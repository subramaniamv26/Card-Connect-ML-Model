"""Engagement feedback (impressions, clicks, redemptions, dismissals) and reporting."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import oracledb
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import db
from ..audit import audit
from ..ml.store import get_state
from ..security import Principal, require
from .common import get_conn, to_utc_naive

router = APIRouter(tags=["engagement"])


class EventIn(BaseModel):
    event_type: str = Field(pattern="^(IMPRESSION|CLICK|REDEMPTION|DISMISSAL)$")
    customer_id: str = Field(max_length=20)
    offer_id: str = Field(max_length=30)
    occurred_at: datetime = Field(description="ISO timestamp; without an offset it is taken as Asia/Colombo time")
    idempotency_key: str = Field(min_length=8, max_length=80, description="Unique per event; retries are ignored")
    batch_id: int | None = None
    card_id: str | None = Field(None, max_length=20)
    transaction_id: str | None = Field(None, max_length=20)
    channel: str | None = Field(None, max_length=20)
    benefit_amount: Decimal | None = Field(None, ge=0, description="Benefit granted (REDEMPTION only, LKR)")


class EventsIn(BaseModel):
    events: list[EventIn] = Field(min_length=1, max_length=1000)


@router.post("/events", summary="Record offer engagement events (idempotent)")
def record(body: EventsIn, request: Request, user: Principal = Depends(require("events:write")),
           conn=Depends(get_conn)) -> dict:
    accepted, duplicates, errors = 0, 0, []
    for i, e in enumerate(body.events):
        if e.benefit_amount is not None and e.event_type != "REDEMPTION":
            errors.append({"index": i, "error": "benefit_amount is only valid for REDEMPTION"})
            continue
        if e.card_id and not db.scalar(conn, "SELECT COUNT(*) FROM cards WHERE card_id = :c AND customer_id = :cu",
                                       {"c": e.card_id, "cu": e.customer_id}):
            errors.append({"index": i, "error": "card_id does not belong to customer_id"})
            continue
        try:
            db.execute(conn, """INSERT INTO offer_events (event_type, customer_id, offer_id, batch_id, card_id,
                                       transaction_id, channel, benefit_amount, occurred_at, recorded_by, idempotency_key)
                                VALUES (:t, :c, :o, :b, :card, :tx, :ch, :amt, :at, :u, :k)""",
                       {"t": e.event_type, "c": e.customer_id, "o": e.offer_id, "b": e.batch_id, "card": e.card_id,
                        "tx": e.transaction_id, "ch": e.channel, "amt": e.benefit_amount,
                        "at": to_utc_naive(e.occurred_at), "u": user.username, "k": e.idempotency_key})
            if e.event_type == "REDEMPTION" and e.benefit_amount:
                db.execute(conn, "UPDATE offers SET budget_consumed = budget_consumed + :a WHERE offer_id = :o",
                           {"a": e.benefit_amount, "o": e.offer_id})
            conn.commit()
            accepted += 1
        except oracledb.IntegrityError as exc:
            conn.rollback()
            msg = str(exc)
            if "UQ_OFFER_EVENTS_IDEM" in msg.upper():
                duplicates += 1
            elif "FK_EVENT_CUSTOMER" in msg.upper():
                errors.append({"index": i, "error": "unknown customer_id"})
            elif "FK_EVENT_OFFER" in msg.upper():
                errors.append({"index": i, "error": "unknown offer_id"})
            else:
                errors.append({"index": i, "error": "rejected by database constraint"})
    audit(user.username, user.role, "EVENTS_RECORD", "offer_events", None,
          {"accepted": accepted, "duplicates": duplicates, "errors": len(errors)}, request=request, conn=conn)
    conn.commit()
    return {"accepted": accepted, "duplicates_ignored": duplicates, "errors": errors}


@router.get("/reports/engagement", summary="Offer engagement and redemption report")
def engagement(user: Principal = Depends(require("read")), conn=Depends(get_conn)) -> dict:
    rows = db.fetch_all(conn, """
        SELECT o.offer_id, o.offer_title, o.merchant_brand, o.merchant_category, o.status, o.is_sample,
               o.campaign_budget, o.budget_consumed,
               SUM(CASE WHEN e.event_type = 'IMPRESSION' THEN 1 ELSE 0 END) impressions,
               SUM(CASE WHEN e.event_type = 'CLICK' THEN 1 ELSE 0 END) clicks,
               SUM(CASE WHEN e.event_type = 'REDEMPTION' THEN 1 ELSE 0 END) redemptions,
               SUM(CASE WHEN e.event_type = 'DISMISSAL' THEN 1 ELSE 0 END) dismissals
        FROM offers o LEFT JOIN offer_events e ON e.offer_id = o.offer_id
        GROUP BY o.offer_id, o.offer_title, o.merchant_brand, o.merchant_category, o.status, o.is_sample,
                 o.campaign_budget, o.budget_consumed
        ORDER BY redemptions DESC, impressions DESC, o.offer_id""")
    bid = get_state(conn, "CURRENT_BATCH_ID")
    recs = {}
    if bid:
        recs = {r["offer_id"]: int(r["n"]) for r in db.fetch_all(conn, """
            SELECT offer_id, COUNT(*) n FROM customer_recommendations WHERE batch_id = :b AND candidate_rank <= 3
            GROUP BY offer_id""", {"b": int(bid)})}
    for r in rows:
        r["recommended_customers"] = recs.get(r["offer_id"], 0)
        imp = r["impressions"] or 0
        r["click_through_rate"] = round(r["clicks"] / imp, 4) if imp else None
        r["redemption_rate_per_impression"] = round(r["redemptions"] / imp, 4) if imp else None
        for k in ("campaign_budget", "budget_consumed"):
            r[k] = None if r[k] is None else float(r[k])
        r["is_sample"] = r["is_sample"] == "Y"
    hist = db.fetch_one(conn, """SELECT COUNT(*) delivered, SUM(CASE WHEN offer_utilized = 'YES' THEN 1 ELSE 0 END) redeemed
                                 FROM historical_offer_targeting WHERE offer_received = 'YES'""")
    by_type = db.fetch_all(conn, """SELECT targeting_type, COUNT(*) delivered,
                                           SUM(CASE WHEN offer_utilized = 'YES' THEN 1 ELSE 0 END) redeemed
                                    FROM historical_offer_targeting WHERE offer_received = 'YES'
                                    GROUP BY targeting_type ORDER BY targeting_type""")
    return {"catalog_offers": rows,
            "live_event_totals": {k: sum(r[k] or 0 for r in rows) for k in ("impressions", "clicks", "redemptions",
                                                                              "dismissals")},
            "historical_campaigns": {"delivered": int(hist["delivered"] or 0), "redeemed": int(hist["redeemed"] or 0),
                                     "by_targeting_type": by_type,
                                     "note": "Source-system campaigns (OFFER_xxxxxxxx) without financial terms; "
                                             "used as training labels, not part of the approved catalog."}}
