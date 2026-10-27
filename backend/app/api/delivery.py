"""Consent-aware delivery integration point.

Recommendation generation NEVER sends messages. Delivery happens only when an authorised caller requests
it here, and only if the customer has GRANTED consent for that channel and the offer is still eligible
for them right now. Accepted requests are queued in DELIVERY_REQUESTS for an external, authorised channel
adapter (SMS gateway / push service) to pick up; no adapter is bundled with this project.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import db
from ..audit import audit
from ..security import Principal, require
from ..services.recommendations import serve
from .common import Page, clean, get_conn

router = APIRouter(tags=["delivery"])


class ConsentIn(BaseModel):
    consent_status: str = Field(pattern="^(GRANTED|WITHDRAWN)$")
    source: str = Field(min_length=3, max_length=100, description="Where consent was captured, e.g. 'mobile app v4.2'")


class DeliveryIn(BaseModel):
    customer_id: str = Field(max_length=20)
    offer_id: str = Field(max_length=30)
    channel: str = Field(pattern="^(SMS|PUSH|EMAIL)$")


@router.put("/customers/{customer_id}/consents/{channel}", summary="Record channel consent (ADMIN)")
def set_consent(customer_id: str, channel: str, body: ConsentIn, request: Request,
                user: Principal = Depends(require("delivery:request")), conn=Depends(get_conn)) -> dict:
    if channel not in ("SMS", "PUSH", "EMAIL"):
        raise HTTPException(400, "channel must be SMS, PUSH or EMAIL")
    if not db.scalar(conn, "SELECT COUNT(*) FROM customers WHERE customer_id = :c", {"c": customer_id}):
        raise HTTPException(404, "Customer not found")
    db.execute(conn, """MERGE INTO customer_channel_consents t
                        USING (SELECT :c customer_id, :ch channel FROM dual) s
                        ON (t.customer_id = s.customer_id AND t.channel = s.channel)
                        WHEN MATCHED THEN UPDATE SET consent_status = :st, source = :src, updated_by = :u,
                             updated_at = SYS_EXTRACT_UTC(SYSTIMESTAMP)
                        WHEN NOT MATCHED THEN INSERT (customer_id, channel, consent_status, source, updated_by)
                             VALUES (:c, :ch, :st, :src, :u)""",
               {"c": customer_id, "ch": channel, "st": body.consent_status, "src": body.source, "u": user.username})
    audit(user.username, user.role, "CONSENT_SET", "customer", customer_id,
          {"channel": channel, "status": body.consent_status}, request=request, conn=conn)
    conn.commit()
    return {"customer_id": customer_id, "channel": channel, "consent_status": body.consent_status}


@router.post("/deliveries", status_code=202, summary="Request delivery of a recommended offer (consent-checked)")
def request_delivery(body: DeliveryIn, request: Request, user: Principal = Depends(require("delivery:request")),
                     conn=Depends(get_conn)) -> dict:
    consent = db.scalar(conn, """SELECT consent_status FROM customer_channel_consents
                                 WHERE customer_id = :c AND channel = :ch""", {"c": body.customer_id, "ch": body.channel})
    status, reason = "QUEUED", "Queued for the authorised channel adapter"
    batch_id = None
    if consent != "GRANTED":
        status, reason = "BLOCKED_NO_CONSENT", f"No granted {body.channel} consent on record"
    else:
        batch, out = serve(conn, [body.customer_id], user)
        batch_id = batch["batch_id"] if batch else None
        offered = {o["offer_id"] for o in out.get(body.customer_id, {}).get("offers", [])}
        if body.offer_id not in offered:
            status, reason = "BLOCKED_INELIGIBLE", "Offer is not currently among the customer's eligible recommendations"
    did = db.insert_returning_id(conn, """INSERT INTO delivery_requests (customer_id, offer_id, batch_id, channel, status,
                                          status_reason, requested_by) VALUES (:c, :o, :b, :ch, :s, :r, :u)""",
                                 {"c": body.customer_id, "o": body.offer_id, "b": batch_id, "ch": body.channel,
                                  "s": status, "r": reason, "u": user.username}, "delivery_id")
    audit(user.username, user.role, "DELIVERY_REQUEST", "delivery", str(did),
          {"customer_id": body.customer_id, "offer_id": body.offer_id, "channel": body.channel, "status": status},
          request=request, conn=conn)
    conn.commit()
    return {"delivery_id": did, "status": status, "reason": reason}


@router.get("/deliveries", summary="Delivery requests (ADMIN)")
def list_deliveries(page: Page = Depends(), user: Principal = Depends(require("delivery:request")),
                    conn=Depends(get_conn)) -> dict:
    total = int(db.scalar(conn, "SELECT COUNT(*) FROM delivery_requests"))
    rows = db.fetch_all(conn, """SELECT * FROM delivery_requests ORDER BY delivery_id DESC
                                 OFFSET :off ROWS FETCH NEXT :n ROWS ONLY""", {"off": page.offset, "n": page.page_size})
    return page.wrap([clean(r) for r in rows], total)
