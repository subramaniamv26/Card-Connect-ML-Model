"""Bulk recommendation generation for every customer.

Order of operations per customer:
  eligibility filter (eligibility.evaluate) -> pair features -> relevance (model) -> estimated saving ->
  final score -> diversity-aware selection of up to `depth` candidates -> card + outlet choice ->
  customer-merchant aggregates for the response -> explanation.
The first three candidates are the published recommendations; the rest are reserves the API can use if
a published offer becomes ineligible before the next run.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np
import pandas as pd

from .eligibility import Card, CustomerContext, OfferStats, evaluate, offer_level_failures
from .features import FeatureStore
from .model import RecModel, benefit_score, estimate_saving, explain, final_score, select_diverse
from .windows import Windows

log = logging.getLogger(__name__)
TOP_N = 3


@dataclass
class GenerationResult:
    recommendations: pd.DataFrame
    statuses: pd.DataFrame
    stats: dict


def participating(merchants: pd.DataFrame) -> tuple[set[str], set[str], pd.DataFrame]:
    ok = merchants[(merchants["is_active"] == "Y") & (merchants["merchant_offer_supported"] == "YES")]
    return set(ok["merchant_brand"]), set(ok["merchant_id"]), ok


def customer_merchant_aggregates(spend: pd.DataFrame, windows: Windows) -> dict[tuple[str, str], tuple]:
    """(customer, merchant) -> (cnt_3m, net_amount_3m, last_txn_date, avg_amount, cnt_window)."""
    s = spend[(spend["transaction_datetime"] >= windows.window_start_dt)
              & (spend["transaction_datetime"] < windows.window_end_dt)]
    in3 = s["transaction_datetime"] >= windows.aggregation_start_dt
    s = s.assign(c3=np.where(in3, s["txn_count"], 0), a3=np.where(in3, s["signed_amount"], 0.0),
                 gross=np.where(s["txn_count"] > 0, s["signed_amount"], 0.0),
                 last=s["transaction_datetime"].where(s["txn_count"] > 0))
    g = s.groupby(["customer_id", "merchant_id"], observed=True).agg(
        c3=("c3", "sum"), a3=("a3", "sum"), last=("last", "max"), gross=("gross", "sum"), cnt=("txn_count", "sum"))
    g = g[g["cnt"] > 0]
    out = {}
    for (c, m), r in zip(g.index, g.itertuples(index=False)):
        out[(c, m)] = (int(r.c3), max(float(r.a3), 0.0), r.last.date() if pd.notna(r.last) else None,
                       float(r.gross) / r.cnt, int(r.cnt))
    return out


def generate(fs: FeatureStore, spend: pd.DataFrame, windows: Windows, customers: pd.DataFrame,
             cards: pd.DataFrame, merchants: pd.DataFrame, offers: list[dict], rules: dict[str, list[dict]],
             redemptions: dict, totals: dict, dismissed: set, model: RecModel, today: date,
             depth: int = 6) -> GenerationResult:
    p_brands, p_outlets, p_merch = participating(merchants)
    merch_idx = merchants.set_index("merchant_id")
    brand_cat = merchants.drop_duplicates("merchant_brand").set_index("merchant_brand")["merchant_category"].to_dict()
    brand_type = merchants.drop_duplicates("merchant_brand").set_index("merchant_brand")["merchant_type"].to_dict()

    # ---- offer-level pre-filter (same checks as evaluate(), done once) -------------------------
    live, excluded = [], Counter()
    for o in offers:
        brand = o["merchant_brand"] or merch_idx.at[o["merchant_id"], "merchant_brand"]
        o = {**o, "_brand": brand, "_category": o["merchant_category"]}
        global_fail = offer_level_failures(o, today, OfferStats(totals.get(o["offer_id"], 0)), p_brands, p_outlets,
                                           brand)
        if global_fail:
            excluded[global_fail[0]] += 1
            continue
        if brand not in fs.brand_index:
            excluded["brand not in merchant master"] += 1
            continue
        live.append(o)
    log.info("Live offers: %d of %d (excluded: %s)", len(live), len(offers), dict(excluded))

    cards_by_cust: dict[str, list[Card]] = defaultdict(list)
    for r in cards.itertuples(index=False):
        if r.card_status == "ACTIVE":
            cards_by_cust[r.customer_id].append(Card(r.card_id, r.card_network, r.card_type, r.card_tier, "ACTIVE"))

    # ---- eligibility per customer x offer ------------------------------------------------------
    pair_c, pair_o, pair_cards = [], [], []
    fail_counts: dict[int, Counter] = defaultdict(Counter)
    offer_rules = [rules.get(o["offer_id"], []) for o in live]
    needs_brand = [any(r["rule_type"] in ("NEW_TO_BRAND", "EXISTING_BRAND_CUSTOMER") for r in rl) for rl in offer_rules]
    offer_bidx = [fs.brand_index[o["_brand"]] for o in live]
    cust_df = fs.customers
    spend90 = fs.cust["spend_90"].to_numpy()
    for ci, cid in enumerate(fs.cust_index):
        row = cust_df.loc[cid]
        ctx = CustomerContext(cid, row["customer_segment"], row["customer_home_district"],
                              row["customer_home_province"], int(row["customer_tenure_months"]),
                              cards_by_cust.get(cid, []), float(spend90[ci]))
        for oi, o in enumerate(live):
            if needs_brand[oi]:
                ctx.used_brands = {o["_brand"]} if fs.cnt[ci, offer_bidx[oi]] > 0 else set()
            ctx.redemptions = {o["offer_id"]: redemptions.get((cid, o["offer_id"]), 0)}
            ctx.dismissed = {o["offer_id"]} if (cid, o["offer_id"]) in dismissed else set()
            res = evaluate(o, offer_rules[oi], ctx, today, OfferStats(totals.get(o["offer_id"], 0)),
                           p_brands, p_outlets, o["_brand"])
            if res.eligible:
                pair_c.append(ci)
                pair_o.append(oi)
                pair_cards.append(res.cards)
            else:
                fail_counts[ci][_bucket(res.failures[0])] += 1

    pc = np.asarray(pair_c, dtype=np.int64)
    po = np.asarray(pair_o, dtype=np.int64)
    pb = np.asarray([offer_bidx[o] for o in pair_o], dtype=np.int64)
    log.info("Eligible pairs: %d", len(pc))

    # ---- scoring ----------------------------------------------------------------------------
    if len(pc):
        X = fs.pair_matrix(pc, pb)
        level = fs.cust["history_level"].to_numpy()[pc]
        relevance = model.relevance(X, level)
        ticket, source = _typical_ticket(fs, pc, pb)
        saving = _vector_saving(live, po, ticket)
        score = final_score(relevance, benefit_score(saving), X["proximity"].to_numpy())
    else:
        X, relevance, ticket, source, saving, score = pd.DataFrame(), *(np.array([]),) * 5

    # ---- per-customer selection ----------------------------------------------------------------
    f_cat_aff = X["cat_aff"].to_numpy() if len(pc) else np.array([])
    f_collab = X["collab"].to_numpy() if len(pc) else np.array([])
    f_prox = X["proximity"].to_numpy() if len(pc) else np.array([])
    agg = customer_merchant_aggregates(spend, windows)
    card_brand = {(r.customer_id, r.card_id, r.brand): r.cnt for r in fs.card_brand_cnt.itertuples(index=False)}
    card_cat = {(r.customer_id, r.card_id, r.category): r.cnt for r in fs.card_cat_cnt.itertuples(index=False)}
    card_all = {(r.customer_id, r.card_id): r.cnt for r in fs.card_cnt.itertuples(index=False)}
    outlets_by_brand: dict[str, list[tuple]] = defaultdict(list)
    for r in p_merch.sort_values("merchant_id").itertuples(index=False):
        outlets_by_brand[r.merchant_brand].append((r.merchant_id, r.merchant_district, r.merchant_province,
                                                   r.merchant_type))
    visits_by_cust_brand: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
    for (c, m), v in agg.items():
        if m in p_outlets:
            visits_by_cust_brand[(c, merch_idx.at[m, "merchant_brand"])].append((v[4], m))
    brand_total_cnt = fs.cnt.sum(axis=0)

    rec_rows, status_rows = [], []
    order = np.argsort(pc, kind="stable")
    starts = np.searchsorted(pc[order], np.arange(len(fs.cust_index) + 1)) if len(pc) else None
    cust_ids = list(fs.cust_index)
    live_count = len(live)
    for ci, cid in enumerate(cust_ids):
        idx = order[starts[ci]:starts[ci + 1]] if starts is not None else np.array([], dtype=np.int64)
        c_row = cust_df.loc[cid]
        level = fs.cust.at[cid, "history_level"]
        chosen: list[int] = []
        if len(idx):
            chosen_pos = select_diverse([float(score[i]) for i in idx], [live[po[i]]["_brand"] for i in idx],
                                        [live[po[i]]["_category"] for i in idx], depth)
            chosen = [int(idx[p]) for p in chosen_pos]
        cust_info = {"history_level": level, "district": c_row["customer_home_district"]}
        for rank, i in enumerate(chosen, start=1):
            o = live[po[i]]
            brand = o["_brand"]
            card = _choose_card(cid, pair_cards[i], brand, o["_category"], card_brand, card_cat, card_all)
            mid = o["merchant_id"] or _choose_outlet(cid, brand, c_row, outlets_by_brand, visits_by_cust_brand)
            c3, a3, last, avg, _ = agg.get((cid, mid), (0, 0.0, None, None, 0))
            row_ctx = {"brand": brand, "category": o["_category"], "brand_cnt": float(fs.cnt[ci, pb[i]]),
                       "brand_recency": float(fs.recency[ci, pb[i]]), "cat_aff": float(f_cat_aff[i]),
                       "collab": float(f_collab[i]), "proximity": float(f_prox[i]),
                       "merchant_type": brand_type.get(brand, "PHYSICAL"), "brand_total_cnt": float(brand_total_cnt[pb[i]])}
            reason, codes = explain(row_ctx, cust_info, {"card_network": card.card_network, "card_type": card.card_type})
            sav, assumption = estimate_saving(o["offer_type"], float(o["offer_value"]), float(o["minimum_spend"] or 0),
                                              None if o["maximum_benefit"] is None else float(o["maximum_benefit"]),
                                              float(ticket[i]), source[i].format(brand=brand, category=o["_category"].lower()))
            rec_rows.append({
                "customer_id": cid, "candidate_rank": rank, "offer_id": o["offer_id"], "card_id": card.card_id,
                "merchant_id": mid, "recommendation_score": round(float(score[i]), 6),
                "relevance_score": round(float(relevance[i]), 6), "recommendation_reason": reason[:500],
                "reason_codes": ",".join(codes)[:200], "estimated_customer_saving": sav,
                "saving_assumption": assumption[:400], "merchant_txn_amount_3m": round(a3, 2),
                "merchant_txn_count_3m": c3, "last_txn_date_with_merchant": last,
                "avg_txn_amount_with_merchant": None if avg is None else round(avg, 2),
            })
        n = min(len(chosen), TOP_N)
        status = "FULL" if n == TOP_N else ("PARTIAL" if n else "NONE")
        status_rows.append({"customer_id": cid, "status": status, "history_level": level,
                            "shortfall_reason": None if status == "FULL" else
                            _shortfall(n, live_count, fail_counts.get(ci, Counter()), len(idx))})

    recs = pd.DataFrame(rec_rows)
    statuses = pd.DataFrame(status_rows)
    stats = {"offers_total": len(offers), "offers_live": live_count, "offers_excluded": dict(excluded),
             "eligible_pairs": int(len(pc)), "customers": len(cust_ids),
             "status_counts": statuses["status"].value_counts().to_dict() if len(statuses) else {}}
    return GenerationResult(recs, statuses, stats)


def _bucket(failure: str) -> str:
    for key, label in (("card", "card network/type/tier"), ("location", "location"), ("rule", "eligibility rule"),
                       ("limit", "redemption limit"), ("budget", "campaign budget"), ("dismissed", "dismissed")):
        if key in failure:
            return label
    return "other"


def _shortfall(n: int, live: int, fails: Counter, eligible: int) -> str:
    if live == 0:
        return "No approved offers are currently live in the catalog."
    parts = [f"Only {n} eligible offer{'s' if n != 1 else ''} after eligibility and variety rules"]
    if live < TOP_N:
        parts.append(f"only {live} approved offer{'s are' if live != 1 else ' is'} currently live")
    if fails:
        parts.append("excluded: " + ", ".join(f"{k} ({v})" for k, v in fails.most_common()))
    if eligible > n:
        parts.append("remaining eligible offers repeat a brand or category already recommended")
    return ("; ".join(parts) + ".")[:500]


def _typical_ticket(fs: FeatureStore, pc: np.ndarray, pb: np.ndarray) -> tuple[np.ndarray, list[str]]:
    cnt = fs.cnt[pc, pb]
    spend = fs.spend[pc, pb]
    # Fallback order: the customer's own average at the brand; else their overall average purchase
    # (capped at 3x the brand's population median) if they shop in the category; else the brand median.
    ccnt = fs.cat_cnt[pc, fs.brand_category[pb]]
    brand_avg = np.where(cnt > 0, spend / np.maximum(cnt, 1), np.nan)
    cust_avg = fs.cust["avg_ticket"].to_numpy()[pc]
    pop = fs.brand_median_ticket[pb]
    ticket = np.where(np.isfinite(brand_avg) & (brand_avg > 0), brand_avg,
                      np.where((ccnt > 0) & (cust_avg > 0), np.minimum(cust_avg, pop * 3), pop))
    source = np.where(np.isfinite(brand_avg) & (brand_avg > 0), "your average at {brand}",
                      np.where((ccnt > 0) & (cust_avg > 0), "your typical purchase size",
                               "a typical {brand} purchase"))
    return ticket, source.tolist()


def _vector_saving(live: list[dict], po: np.ndarray, ticket: np.ndarray) -> np.ndarray:
    val = np.array([float(live[o]["offer_value"]) for o in po])
    mins = np.array([float(live[o]["minimum_spend"] or 0) for o in po])
    cap = np.array([np.inf if live[o]["maximum_benefit"] is None else float(live[o]["maximum_benefit"]) for o in po])
    pct = np.array([live[o]["offer_type"].startswith("PERCENT") for o in po])
    basis = np.maximum(ticket, mins)
    return np.minimum(np.where(pct, basis * val / 100.0, val), cap)


def _choose_card(cid, eligible: list[Card], brand, category, card_brand, card_cat, card_all) -> Card:
    return max(eligible, key=lambda c: (card_brand.get((cid, c.card_id, brand), 0),
                                        card_cat.get((cid, c.card_id, category), 0),
                                        card_all.get((cid, c.card_id), 0), c.card_id))


def _choose_outlet(cid, brand, c_row, outlets_by_brand, visits) -> str:
    visited = visits.get((cid, brand))
    if visited:
        return max(visited)[1]
    outlets = outlets_by_brand[brand]
    for match_idx, value in ((1, c_row["customer_home_district"]), (2, c_row["customer_home_province"])):
        for o in outlets:
            if o[match_idx] == value:
                return o[0]
    return outlets[0][0]
