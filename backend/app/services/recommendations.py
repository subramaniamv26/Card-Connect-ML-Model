"""Serving stored recommendations with request-time eligibility re-validation.

The stored batch holds up to RESERVE_DEPTH diversity-selected candidates per customer. At request time
each candidate is re-checked with the same eligibility function used at generation (offer status and
validity today, outlet participation, card still active and eligible, limits, recent dismissals, rules).
The first three that still pass are returned, so an offer revoked after the last run is never served and a
reserve takes its place. Personal data is masked according to the caller's role.
"""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..config import get_settings
from ..masking import mask_device, mask_email, mask_name, mask_phone
from ..ml.eligibility import Card, CustomerContext, OfferStats, evaluate
from ..ml.store import current_batch
from ..security import Principal

TOP_N = 3
_MERCHANT_CACHE: dict[str, Any] = {"at": 0.0, "rows": None}


def today_local() -> date:
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


def _in_binds(prefix: str, values: list[str]) -> tuple[str, dict[str, str]]:
    binds = {f"{prefix}{i}": v for i, v in enumerate(values)}
    return ", ".join(":" + k for k in binds) or "NULL", binds


def _merchants(conn) -> dict[str, dict]:
    if _MERCHANT_CACHE["rows"] is None or time.time() - _MERCHANT_CACHE["at"] > 60:
        rows = db.fetch_all(conn, """SELECT merchant_id, merchant_brand, merchant_category, merchant_location,
                                            merchant_city, merchant_district, merchant_province, merchant_email,
                                            merchant_contact_no, merchant_type, TRIM(merchant_offer_supported) supported,
                                            is_active FROM merchants""")
        _MERCHANT_CACHE.update(at=time.time(), rows={r["merchant_id"]: r for r in rows})
    return _MERCHANT_CACHE["rows"]


def invalidate_merchant_cache() -> None:
    _MERCHANT_CACHE["rows"] = None


def _iso(v):
    if v is None:
        return None
    return v.date().isoformat() if isinstance(v, datetime) else v.isoformat()


def serve(conn, customer_ids: list[str], principal: Principal) -> tuple[dict | None, dict[str, dict]]:
    """Return (batch, {customer_id: response}) for the given customers from the current batch."""
    batch = current_batch(conn)
    if batch is None or not customer_ids:
        return batch, {}
    s = get_settings()
    today = today_local()
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # offer_events.occurred_at is stored in UTC
    inlist, binds = _in_binds("c", customer_ids)
    bb = {**binds, "b": batch["batch_id"]}

    customers = {r["customer_id"]: r for r in db.fetch_all(conn, f"""
        SELECT customer_id, customer_name, customer_segment, customer_home_city, customer_home_district,
               customer_home_province, customer_tenure_months, customer_contact_no
        FROM customers WHERE customer_id IN ({inlist})""", binds)}
    devices = defaultdict(list)
    for r in db.fetch_all(conn, f"""SELECT customer_id, mobile_device_id, device_type, device_os, is_primary
                                    FROM customer_devices WHERE customer_id IN ({inlist})
                                    ORDER BY is_primary DESC, last_seen_at DESC""", binds):
        devices[r["customer_id"]].append(r)
    cards = defaultdict(list)
    for r in db.fetch_all(conn, f"""SELECT customer_id, card_id, card_network, card_type, card_tier, card_status
                                    FROM cards WHERE customer_id IN ({inlist})""", binds):
        cards[r["customer_id"]].append(r)
    cands = defaultdict(list)
    for r in db.fetch_all(conn, f"""SELECT * FROM customer_recommendations
                                    WHERE batch_id = :b AND customer_id IN ({inlist})
                                    ORDER BY customer_id, candidate_rank""", bb):
        cands[r["customer_id"]].append(r)
    statuses = {r["customer_id"]: r for r in db.fetch_all(conn, f"""
        SELECT customer_id, status, shortfall_reason, history_level FROM customer_rec_status
        WHERE batch_id = :b AND customer_id IN ({inlist})""", bb)}

    offer_ids = sorted({r["offer_id"] for rows in cands.values() for r in rows})
    offers, rules, totals = {}, defaultdict(list), {}
    if offer_ids:
        oin, obinds = _in_binds("o", offer_ids)
        offers = {r["offer_id"]: r for r in db.fetch_all(conn, f"SELECT * FROM offers WHERE offer_id IN ({oin})", obinds)}
        for r in db.fetch_all(conn, f"""SELECT offer_id, rule_type, operator, rule_value, is_active
                                        FROM offer_eligibility_rules WHERE offer_id IN ({oin}) AND is_active = 'Y'""",
                              obinds):
            rules[r["offer_id"]].append(r)
        totals = {r["offer_id"]: int(r["n"]) for r in db.fetch_all(conn, f"""
            SELECT offer_id, COUNT(*) n FROM offer_events WHERE event_type = 'REDEMPTION' AND offer_id IN ({oin})
            GROUP BY offer_id""", obinds)}
    redemptions = defaultdict(dict)
    dismissed = defaultdict(set)
    for r in db.fetch_all(conn, f"""SELECT customer_id, offer_id, event_type, COUNT(*) n FROM offer_events
                                    WHERE customer_id IN ({inlist})
                                      AND (event_type = 'REDEMPTION' OR (event_type = 'DISMISSAL' AND occurred_at >= :since))
                                    GROUP BY customer_id, offer_id, event_type""",
                          {**binds, "since": now - timedelta(days=s.dismissal_suppression_days)}):
        if r["event_type"] == "REDEMPTION":
            redemptions[r["customer_id"]][r["offer_id"]] = int(r["n"])
        else:
            dismissed[r["customer_id"]].add(r["offer_id"])

    merchants = _merchants(conn)
    p_outlets = {m for m, r in merchants.items() if r["is_active"] == "Y" and r["supported"] == "YES"}
    p_brands = {merchants[m]["merchant_brand"] for m in p_outlets}
    needs_spend = {o for o, rl in rules.items() if any(r["rule_type"] == "MIN_SPEND_LAST_90D_LKR" for r in rl)}
    needs_brand = {o for o, rl in rules.items() if any(r["rule_type"] in ("NEW_TO_BRAND", "EXISTING_BRAND_CUSTOMER")
                                                       for r in rl)}
    spend90, used = _behaviour(conn, customer_ids, batch, bool(needs_spend), bool(needs_brand))

    out = {}
    for cid in customer_ids:
        c = customers.get(cid)
        if c is None:
            continue
        ctx = CustomerContext(cid, c["customer_segment"], c["customer_home_district"], c["customer_home_province"],
                              c["customer_tenure_months"],
                              [Card(k["card_id"], k["card_network"], k["card_type"], k["card_tier"], k["card_status"])
                               for k in cards[cid]],
                              spend90.get(cid, 0.0), used.get(cid, set()), redemptions[cid], dismissed[cid])
        served, dropped = [], []
        for r in cands[cid]:
            if len(served) == TOP_N:
                break
            o = offers.get(r["offer_id"])
            if o is None:
                dropped.append(f"{r['offer_id']}: offer no longer exists")
                continue
            brand = o["merchant_brand"] or merchants[o["merchant_id"]]["merchant_brand"]
            res = evaluate(o, rules.get(o["offer_id"], []), ctx, today, OfferStats(totals.get(o["offer_id"], 0)),
                           p_brands, p_outlets, brand)
            if not res.eligible:
                if r["candidate_rank"] <= TOP_N:
                    dropped.append(f"{o['offer_id']} {res.failures[0]}")
                continue
            card = next((k for k in res.cards if k.card_id == r["card_id"]), res.cards[0])
            served.append((r, o, card))
        st = statuses.get(cid, {})
        n = len(served)
        status = "FULL" if n == TOP_N else ("PARTIAL" if n else "NONE")
        reasons = []
        if n < TOP_N and st.get("shortfall_reason"):
            reasons.append(st["shortfall_reason"])
        if dropped and n < TOP_N:
            reasons.append("Since the last run, became ineligible: " + "; ".join(dropped))
        out[cid] = _response(c, devices[cid], cards[cid], served, merchants, batch, status,
                             " ".join(reasons) or None, st.get("history_level"), principal)
    return batch, out


def _behaviour(conn, customer_ids, batch, need_spend: bool, need_brand: bool):
    spend90, used = {}, defaultdict(set)
    if not (need_spend or need_brand):
        return spend90, used
    inlist, binds = _in_binds("c", customer_ids)
    end = batch["data_as_of"] + timedelta(days=1)
    if need_spend:
        for r in db.fetch_all(conn, f"""
            SELECT customer_id, SUM(CASE WHEN transaction_type = 'REFUND' THEN -amount_lkr ELSE amount_lkr END) s
            FROM card_transactions WHERE customer_id IN ({inlist}) AND transaction_status = 'APPROVED'
              AND transaction_type <> 'REVERSAL' AND transaction_datetime >= :s AND transaction_datetime < :e
            GROUP BY customer_id""", {**binds, "s": end - timedelta(days=90), "e": end}):
            spend90[r["customer_id"]] = float(r["s"] or 0)
    if need_brand:
        for r in db.fetch_all(conn, f"""
            SELECT DISTINCT t.customer_id, m.merchant_brand FROM card_transactions t
            JOIN merchants m ON m.merchant_id = t.merchant_id
            WHERE t.customer_id IN ({inlist}) AND t.transaction_status = 'APPROVED'
              AND t.transaction_type IN ('PURCHASE','BILL_PAYMENT','RECURRING_PAYMENT')
              AND t.transaction_datetime >= ADD_MONTHS(:e, -12) AND t.transaction_datetime < :e""",
                              {**binds, "e": end}):
            used[r["customer_id"]].add(r["merchant_brand"])
    return spend90, used


def _response(c, devices, cards, served, merchants, batch, status, shortfall, level, p: Principal) -> dict:
    pii = p.can("pii:read")
    names = p.can("customer_names")
    primary = devices[0] if devices else None
    customer = {
        "customer_id": c["customer_id"],
        "customer_name": c["customer_name"] if names else mask_name(c["customer_name"]),
        "mobile_device_id": (primary["mobile_device_id"] if pii else mask_device(primary["mobile_device_id"]))
        if primary else None,
        "customer_home_city": c["customer_home_city"],
        "customer_home_district": c["customer_home_district"],
        "customer_home_province": c["customer_home_province"],
        "customer_contact_no": c["customer_contact_no"] if pii else mask_phone(c["customer_contact_no"]),
        "customer_contact_no_available": c["customer_contact_no"] is not None,
        "history_level": level,
        "cards_held": len(cards),
        "active_cards": sum(1 for k in cards if k["card_status"] == "ACTIVE"),
        "devices_registered": len(devices),
    }
    if pii:
        customer["devices"] = [{"mobile_device_id": d["mobile_device_id"], "device_type": d["device_type"],
                                "device_os": d["device_os"], "is_primary": d["is_primary"] == "Y"} for d in devices]
    agg_start, agg_end = _iso(batch["aggregation_start"]), _iso(batch["aggregation_end"])
    offers = []
    for rank, (r, o, card) in enumerate(served, start=1):
        m = merchants[r["merchant_id"]]
        offers.append({
            "rank": rank,
            "offer_id": o["offer_id"], "offer_title": o["offer_title"], "offer_description": o["offer_description"],
            "recommendation_score": float(r["recommendation_score"]),
            "recommendation_reason": r["recommendation_reason"],
            "reason_codes": (r["reason_codes"] or "").split(",") if r["reason_codes"] else [],
            "offer_type": o["offer_type"], "offer_value": float(o["offer_value"]),
            "minimum_spend": float(o["minimum_spend"]),
            "maximum_benefit": None if o["maximum_benefit"] is None else float(o["maximum_benefit"]),
            "estimated_customer_saving": None if r["estimated_customer_saving"] is None
            else float(r["estimated_customer_saving"]),
            "estimated_customer_saving_assumptions": r["saving_assumption"],
            "currency": o["currency"],
            "valid_from": _iso(o["valid_from"]), "valid_to": _iso(o["valid_to"]),
            "terms_and_conditions": o["terms_and_conditions"],
            "is_sample_offer": o["is_sample"] == "Y",
            "card": {"card_id": card.card_id, "card_network": card.card_network, "card_type": card.card_type},
            "merchant": {
                "merchant_id": m["merchant_id"], "merchant_brand": m["merchant_brand"],
                "merchant_location": m["merchant_location"], "merchant_city": m["merchant_city"],
                "merchant_district": m["merchant_district"], "merchant_province": m["merchant_province"],
                "merchant_email": m["merchant_email"] if names else mask_email(m["merchant_email"]),
                "merchant_contact_no": m["merchant_contact_no"] if names else mask_phone(m["merchant_contact_no"]),
                "merchant_type": m["merchant_type"], "merchant_category": m["merchant_category"],
            },
            "customer_merchant_spend": {
                "merchant_transaction_amount_last_3_months": float(r["merchant_txn_amount_3m"]),
                "merchant_transaction_count_last_3_months": int(r["merchant_txn_count_3m"]),
                "last_transaction_date_with_merchant": _iso(r["last_txn_date_with_merchant"]),
                "average_transaction_amount_with_merchant": None if r["avg_txn_amount_with_merchant"] is None
                else float(r["avg_txn_amount_with_merchant"]),
                "aggregation_period_start": agg_start, "aggregation_period_end": agg_end,
                "currency": "LKR",
            },
        })
    return {
        "customer": customer,
        "recommendation_status": status,
        "shortfall_reason": shortfall,
        "offers": offers,
        "generated_at": batch["generated_at"].isoformat() + "Z" if batch["generated_at"] else None,
        "data_as_of": _iso(batch["data_as_of"]),
        "model_version": batch["model_version"],
        "batch_id": batch["batch_id"],
        "aggregation_period_start": agg_start,
        "aggregation_period_end": agg_end,
        "served_at": datetime.now(ZoneInfo(get_settings().timezone)).isoformat(timespec="seconds"),
        "pii_masked": not pii,
    }
