"""Merchant, offer catalog and eligibility-rule management.

Offer lifecycle: DRAFT -> PENDING_APPROVAL -> APPROVED <-> SUSPENDED; any state -> REVOKED (terminal);
PENDING_APPROVAL -> DRAFT (rejected). Only APPROVED offers within their validity are ever recommended.
Changing the terms or eligibility rules of an APPROVED offer sends it back to PENDING_APPROVAL.
Create/update requests are validated with the same field parsers and checks as the CSV importer.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .. import db
from ..audit import audit
from ..config import get_settings
from ..ingest.datasets import DATASETS, OFFER_COLS, RowError, validate_rule_value
from ..ingest.fields import FieldError
from ..ingest.importer import load_reference
from ..security import Principal, require
from ..services.recommendations import invalidate_merchant_cache, today_local
from .common import Page, clean, get_conn

router = APIRouter(tags=["catalog"])


def _validate_with_spec(dataset: str, payload: dict[str, Any], conn) -> tuple:
    spec = DATASETS[dataset]
    raw = {k: (None if v is None else (v.isoformat() if isinstance(v, date) else str(v))) for k, v in payload.items()}
    values, errors = {}, []
    for f in spec.fields:
        try:
            values[f.name] = f.parse(raw.get(f.name))
        except FieldError as e:
            errors.append({"field": f.name, "code": e.code, "message": e.message})
    if errors:
        raise HTTPException(422, {"message": "Validation failed", "errors": errors})
    try:
        return spec.validator(values, raw, load_reference(conn, spec.needs))
    except RowError as e:
        raise HTTPException(422, {"message": "Validation failed",
                                  "errors": [{"field": e.column, "code": e.code, "message": e.message}]}) from None


# ================================================================================================
# Merchants
# ================================================================================================
class MerchantIn(BaseModel):
    merchant_id: str = Field(max_length=20)
    merchant_brand: str = Field(max_length=100)
    merchant_category: str
    merchant_location: str = Field(max_length=200)
    merchant_city: str = Field(max_length=60)
    merchant_district: str = Field(max_length=40)
    merchant_province: str = Field(max_length=40)
    merchant_email: str | None = Field(None, max_length=120)
    merchant_contact_no: str | None = Field(None, max_length=20)
    merchant_type: str
    merchant_offer_supported: str = "NO"


class MerchantPatch(BaseModel):
    is_active: bool | None = None
    merchant_offer_supported: bool | None = None
    merchant_email: str | None = Field(None, max_length=120)
    merchant_contact_no: str | None = Field(None, max_length=20)
    merchant_location: str | None = Field(None, max_length=200)


@router.get("/merchants", summary="Search merchants")
def list_merchants(page: Page = Depends(), q: str | None = Query(None, max_length=100), category: str | None = None,
                   district: str | None = None, offer_supported: bool | None = None, active: bool | None = None,
                   user: Principal = Depends(require("read")), conn=Depends(get_conn)) -> dict:
    where, binds = ["1 = 1"], {}
    if q:
        where.append("(UPPER(merchant_brand) LIKE :q OR merchant_id LIKE :q)")
        binds["q"] = f"%{q.upper()}%"
    if category:
        where.append("merchant_category = :cat"); binds["cat"] = category
    if district:
        where.append("merchant_district = :d"); binds["d"] = district
    if offer_supported is not None:
        where.append("merchant_offer_supported = :os"); binds["os"] = "YES" if offer_supported else "NO"
    if active is not None:
        where.append("is_active = :a"); binds["a"] = "Y" if active else "N"
    w = " AND ".join(where)
    total = int(db.scalar(conn, f"SELECT COUNT(*) FROM merchants WHERE {w}", binds))
    rows = db.fetch_all(conn, f"""SELECT merchant_id, merchant_brand, merchant_category, merchant_location, merchant_city,
                                         merchant_district, merchant_province, merchant_email, merchant_contact_no,
                                         merchant_type, TRIM(merchant_offer_supported) merchant_offer_supported,
                                         is_active, updated_at
                                  FROM merchants WHERE {w} ORDER BY merchant_brand, merchant_id
                                  OFFSET :off ROWS FETCH NEXT :n ROWS ONLY""",
                        {**binds, "off": page.offset, "n": page.page_size})
    return page.wrap([clean(r) for r in rows], total)


@router.post("/merchants", status_code=201, summary="Create a merchant outlet")
def create_merchant(body: MerchantIn, request: Request, user: Principal = Depends(require("catalog:write")),
                    conn=Depends(get_conn)) -> dict:
    if db.scalar(conn, "SELECT COUNT(*) FROM merchants WHERE merchant_id = :m", {"m": body.merchant_id}):
        raise HTTPException(409, "merchant_id already exists")
    row = _validate_with_spec("merchants", body.model_dump(), conn)
    cols = DATASETS["merchants"].stage_columns
    db.execute(conn, f"INSERT INTO merchants ({', '.join(cols)}) VALUES ({', '.join(':' + str(i + 1) for i in range(len(cols)))})",
               list(row))
    audit(user.username, user.role, "MERCHANT_CREATE", "merchant", body.merchant_id, request=request, conn=conn)
    conn.commit()
    invalidate_merchant_cache()
    return {"merchant_id": body.merchant_id}


@router.patch("/merchants/{merchant_id}", summary="Activate/deactivate, offer participation, contact details")
def patch_merchant(merchant_id: str, body: MerchantPatch, request: Request,
                   user: Principal = Depends(require("catalog:write")), conn=Depends(get_conn)) -> dict:
    sets, binds = [], {"m": merchant_id}
    if body.is_active is not None:
        sets.append("is_active = :a"); binds["a"] = "Y" if body.is_active else "N"
    if body.merchant_offer_supported is not None:
        sets.append("merchant_offer_supported = :os"); binds["os"] = "YES" if body.merchant_offer_supported else "NO"
    for f in ("merchant_email", "merchant_contact_no", "merchant_location"):
        v = getattr(body, f)
        if v is not None:
            sets.append(f"{f} = :{f}"); binds[f] = v
    if not sets:
        raise HTTPException(400, "Nothing to update")
    sets.append("updated_at = SYS_EXTRACT_UTC(SYSTIMESTAMP)")
    if db.execute(conn, f"UPDATE merchants SET {', '.join(sets)} WHERE merchant_id = :m", binds) != 1:
        raise HTTPException(404, "Merchant not found")
    audit(user.username, user.role, "MERCHANT_UPDATE", "merchant", merchant_id,
          body.model_dump(exclude_none=True), request=request, conn=conn)
    conn.commit()
    invalidate_merchant_cache()
    return {"merchant_id": merchant_id, "updated": True}


@router.get("/merchant-brands", summary="Brands with category and participating outlet counts")
def brands(user: Principal = Depends(require("read")), conn=Depends(get_conn)) -> list[dict]:
    return db.fetch_all(conn, """SELECT merchant_brand, merchant_category, COUNT(*) outlets,
                                        SUM(CASE WHEN merchant_offer_supported = 'YES' AND is_active = 'Y' THEN 1 ELSE 0 END)
                                            participating_outlets
                                 FROM merchants GROUP BY merchant_brand, merchant_category ORDER BY merchant_brand""")


# ================================================================================================
# Offers
# ================================================================================================
class OfferIn(BaseModel):
    offer_id: str = Field(max_length=30, pattern=r"^[A-Za-z0-9_\-]+$")
    offer_title: str = Field(max_length=150)
    offer_description: str = Field(max_length=1000)
    merchant_brand: str | None = Field(None, max_length=100)
    merchant_id: str | None = Field(None, max_length=20)
    merchant_category: str
    offer_type: str = Field(pattern="^(PERCENT_DISCOUNT|PERCENT_CASHBACK|FIXED_DISCOUNT|FIXED_CASHBACK)$")
    offer_value: Decimal = Field(gt=0)
    currency: str = "LKR"
    minimum_spend: Decimal = Field(Decimal(0), ge=0)
    maximum_benefit: Decimal | None = Field(None, gt=0)
    valid_from: date
    valid_to: date
    eligible_card_networks: str = "ALL"
    eligible_card_types: str = "ALL"
    eligible_card_tiers: str = "ALL"
    eligible_locations: str = "ALL"
    redemption_limit_per_customer: int | None = Field(None, ge=1)
    total_redemption_limit: int | None = Field(None, ge=1)
    campaign_budget: Decimal | None = Field(None, gt=0)
    terms_and_conditions: str = Field(min_length=10, max_length=32000)
    is_sample: bool = False


class OfferUpdate(OfferIn):
    version: int = Field(description="Current version (optimistic locking)")


class Reason(BaseModel):
    reason: str = Field(min_length=3, max_length=400)


OFFER_SELECT = """SELECT offer_id, offer_title, offer_description, merchant_brand, merchant_id, merchant_category,
       offer_type, offer_value, currency, minimum_spend, maximum_benefit, valid_from, valid_to, eligible_card_networks,
       eligible_card_types, eligible_card_tiers, eligible_locations, redemption_limit_per_customer,
       total_redemption_limit, campaign_budget, budget_consumed, terms_and_conditions, status, is_sample,
       revoked_reason, version, created_by, created_at, updated_by, updated_at, approved_by, approved_at
       FROM offers"""


@router.get("/offers", summary="List offers in the catalog")
def list_offers(page: Page = Depends(), status: str | None = None, category: str | None = None,
                q: str | None = Query(None, max_length=100), live_only: bool = False,
                user: Principal = Depends(require("read")), conn=Depends(get_conn)) -> dict:
    where, binds = ["1 = 1"], {}
    if status:
        where.append("status = :s"); binds["s"] = status
    if category:
        where.append("merchant_category = :c"); binds["c"] = category
    if q:
        where.append("(UPPER(offer_title) LIKE :q OR UPPER(offer_id) LIKE :q OR UPPER(merchant_brand) LIKE :q)")
        binds["q"] = f"%{q.upper()}%"
    if live_only:
        where.append("status = 'APPROVED' AND TRUNC(SYSDATE) BETWEEN valid_from AND valid_to")
    w = " AND ".join(where)
    total = int(db.scalar(conn, f"SELECT COUNT(*) FROM offers WHERE {w}", binds))
    rows = db.fetch_all(conn, f"{OFFER_SELECT} WHERE {w} ORDER BY offer_id OFFSET :off ROWS FETCH NEXT :n ROWS ONLY",
                        {**binds, "off": page.offset, "n": page.page_size})
    return page.wrap([_offer_out(r) for r in rows], total)


def _offer_out(r: dict) -> dict:
    out = clean(r)
    for k in ("offer_value", "minimum_spend", "maximum_benefit", "campaign_budget", "budget_consumed"):
        if out.get(k) is not None:
            out[k] = float(out[k])
    out["is_sample"] = r["is_sample"] == "Y"
    return out


def _get_offer(conn, offer_id: str) -> dict:
    r = db.fetch_one(conn, f"{OFFER_SELECT} WHERE offer_id = :o", {"o": offer_id})
    if r is None:
        raise HTTPException(404, "Offer not found")
    return r


@router.get("/offers/{offer_id}", summary="Offer details with eligibility rules and engagement")
def get_offer(offer_id: str, user: Principal = Depends(require("read")), conn=Depends(get_conn)) -> dict:
    out = _offer_out(_get_offer(conn, offer_id))
    out["rules"] = [clean(r) for r in db.fetch_all(conn, """SELECT rule_id, rule_type, operator, rule_value, description,
                                                                   is_active, created_by, created_at
                                                            FROM offer_eligibility_rules WHERE offer_id = :o
                                                            ORDER BY rule_id""", {"o": offer_id})]
    out["engagement"] = {r["event_type"]: int(r["n"]) for r in db.fetch_all(
        conn, "SELECT event_type, COUNT(*) n FROM offer_events WHERE offer_id = :o GROUP BY event_type", {"o": offer_id})}
    return out


def _offer_binds(row: tuple) -> dict:
    return dict(zip(OFFER_COLS + ["row_hash"], row))


@router.post("/offers", status_code=201, summary="Create an offer (lands as PENDING_APPROVAL)")
def create_offer(body: OfferIn, request: Request, user: Principal = Depends(require("catalog:write")),
                 conn=Depends(get_conn)) -> dict:
    if db.scalar(conn, "SELECT COUNT(*) FROM offers WHERE offer_id = :o", {"o": body.offer_id}):
        raise HTTPException(409, "offer_id already exists")
    payload = body.model_dump()
    payload["is_sample"] = "YES" if body.is_sample else "NO"
    b = _offer_binds(_validate_with_spec("offers", payload, conn))
    cols = list(b)
    db.execute(conn, f"""INSERT INTO offers ({', '.join(cols)}, status, created_by, updated_by)
                         VALUES ({', '.join(':' + c for c in cols)}, 'PENDING_APPROVAL', :u, :u)""", {**b, "u": user.username})
    audit(user.username, user.role, "OFFER_CREATE", "offer", body.offer_id, {"title": body.offer_title},
          request=request, conn=conn)
    conn.commit()
    return _offer_out(_get_offer(conn, body.offer_id))


@router.put("/offers/{offer_id}", summary="Update an offer's terms (re-enters approval if it was approved)")
def update_offer(offer_id: str, body: OfferUpdate, request: Request, user: Principal = Depends(require("catalog:write")),
                 conn=Depends(get_conn)) -> dict:
    if body.offer_id != offer_id:
        raise HTTPException(400, "offer_id in body must match the URL")
    cur = _get_offer(conn, offer_id)
    if cur["status"] == "REVOKED":
        raise HTTPException(409, "Revoked offers cannot be changed")
    payload = body.model_dump(exclude={"version"})
    payload["is_sample"] = "YES" if body.is_sample else "NO"
    b = _offer_binds(_validate_with_spec("offers", payload, conn))
    sets = ", ".join(f"{c} = :{c}" for c in b if c != "offer_id")
    n = db.execute(conn, f"""UPDATE offers SET {sets}, status = CASE WHEN status = 'DRAFT' THEN 'DRAFT'
                                                                  ELSE 'PENDING_APPROVAL' END,
                                 approved_by = NULL, approved_at = NULL, version = version + 1, updated_by = :u,
                                 updated_at = SYS_EXTRACT_UTC(SYSTIMESTAMP)
                             WHERE offer_id = :offer_id AND version = :ver""", {**b, "u": user.username, "ver": body.version})
    if n != 1:
        raise HTTPException(409, "Offer was modified by someone else; reload and retry")
    audit(user.username, user.role, "OFFER_UPDATE", "offer", offer_id, {"previous_status": cur["status"]},
          request=request, conn=conn)
    conn.commit()
    return _offer_out(_get_offer(conn, offer_id))


TRANSITIONS = {
    "submit": ({"DRAFT"}, "PENDING_APPROVAL"),
    "approve": ({"PENDING_APPROVAL"}, "APPROVED"),
    "reject": ({"PENDING_APPROVAL"}, "DRAFT"),
    "suspend": ({"APPROVED"}, "SUSPENDED"),
    "resume": ({"SUSPENDED"}, "APPROVED"),
    "revoke": ({"DRAFT", "PENDING_APPROVAL", "APPROVED", "SUSPENDED", "EXPIRED"}, "REVOKED"),
}


@router.post("/offers/{offer_id}/actions/{action}", summary="Workflow: submit | approve | reject | suspend | resume | revoke")
def transition(offer_id: str, action: str, request: Request, body: Reason | None = None,
               user: Principal = Depends(require("catalog:write")), conn=Depends(get_conn)) -> dict:
    if action not in TRANSITIONS:
        raise HTTPException(404, "Unknown action")
    if action in ("approve", "resume") and not user.can("offers:approve"):
        raise HTTPException(403, "Approval requires offers:approve")
    if action in ("reject", "revoke", "suspend") and body is None:
        raise HTTPException(422, "A reason is required")
    cur = _get_offer(conn, offer_id)
    allowed, target = TRANSITIONS[action]
    if cur["status"] not in allowed:
        raise HTTPException(409, f"Cannot {action} an offer in status {cur['status']}")
    if action == "approve":
        if get_settings().require_four_eyes_approval and user.username in (cur["created_by"], cur["updated_by"]):
            raise HTTPException(403, "Four-eyes approval: the approver must differ from the author")
        if cur["valid_to"].date() < today_local():
            raise HTTPException(409, "Offer validity has already ended")
    extra = ""
    binds = {"o": offer_id, "t": target, "u": user.username, "v": cur["version"]}
    if target == "APPROVED":
        extra = ", approved_by = :u, approved_at = SYS_EXTRACT_UTC(SYSTIMESTAMP)"
    if action == "revoke":
        extra = ", revoked_reason = :r"
        binds["r"] = body.reason
    n = db.execute(conn, f"""UPDATE offers SET status = :t, updated_by = :u, updated_at = SYS_EXTRACT_UTC(SYSTIMESTAMP){extra}
                             WHERE offer_id = :o AND version = :v""", binds)
    if n != 1:
        raise HTTPException(409, "Offer was modified concurrently; retry")
    audit(user.username, user.role, f"OFFER_{action.upper()}", "offer", offer_id,
          {"from": cur["status"], "to": target, "reason": body.reason if body else None}, request=request, conn=conn)
    conn.commit()
    return _offer_out(_get_offer(conn, offer_id))


# ================================================================================================
# Eligibility rules
# ================================================================================================
class RuleIn(BaseModel):
    rule_type: str
    operator: str
    rule_value: str | None = Field(None, max_length=400)
    description: str | None = Field(None, max_length=400)
    is_active: bool = True


def _check_rule(body: RuleIn) -> str | None:
    from ..ingest.datasets import RULE_OPERATORS
    if body.rule_type not in RULE_OPERATORS:
        raise HTTPException(422, f"rule_type must be one of {sorted(RULE_OPERATORS)}")
    try:
        return validate_rule_value(body.rule_type, body.operator, body.rule_value)
    except RowError as e:
        raise HTTPException(422, {"field": e.column, "code": e.code, "message": e.message}) from None


def _rules_changed(conn, offer_id: str, user: Principal) -> None:
    db.execute(conn, """UPDATE offers SET status = 'PENDING_APPROVAL', approved_by = NULL, approved_at = NULL,
                               version = version + 1, updated_by = :u, updated_at = SYS_EXTRACT_UTC(SYSTIMESTAMP)
                        WHERE offer_id = :o AND status IN ('APPROVED', 'SUSPENDED')""", {"o": offer_id, "u": user.username})


@router.post("/offers/{offer_id}/rules", status_code=201, summary="Add an eligibility rule (one per rule type)")
def add_rule(offer_id: str, body: RuleIn, request: Request, user: Principal = Depends(require("catalog:write")),
             conn=Depends(get_conn)) -> dict:
    cur = _get_offer(conn, offer_id)
    if cur["status"] == "REVOKED":
        raise HTTPException(409, "Revoked offers cannot be changed")
    value = _check_rule(body)
    if db.scalar(conn, "SELECT COUNT(*) FROM offer_eligibility_rules WHERE offer_id = :o AND rule_type = :t",
                 {"o": offer_id, "t": body.rule_type}):
        raise HTTPException(409, "A rule of this type already exists for the offer; update it instead")
    rid = db.insert_returning_id(conn, """INSERT INTO offer_eligibility_rules (offer_id, rule_type, operator, rule_value,
                                          description, is_active, created_by)
                                          VALUES (:o, :t, :op, :v, :d, :a, :u)""",
                                 {"o": offer_id, "t": body.rule_type, "op": body.operator, "v": value,
                                  "d": body.description, "a": "Y" if body.is_active else "N", "u": user.username},
                                 "rule_id")
    _rules_changed(conn, offer_id, user)
    audit(user.username, user.role, "RULE_CREATE", "offer", offer_id, {"rule_id": rid, "rule_type": body.rule_type},
          request=request, conn=conn)
    conn.commit()
    return {"rule_id": rid, "offer_status": _get_offer(conn, offer_id)["status"]}


@router.put("/offers/{offer_id}/rules/{rule_id}", summary="Update an eligibility rule")
def update_rule(offer_id: str, rule_id: int, body: RuleIn, request: Request,
                user: Principal = Depends(require("catalog:write")), conn=Depends(get_conn)) -> dict:
    value = _check_rule(body)
    n = db.execute(conn, """UPDATE offer_eligibility_rules SET rule_type = :t, operator = :op, rule_value = :v,
                                   description = :d, is_active = :a
                            WHERE rule_id = :r AND offer_id = :o""",
                   {"t": body.rule_type, "op": body.operator, "v": value, "d": body.description,
                    "a": "Y" if body.is_active else "N", "r": rule_id, "o": offer_id})
    if n != 1:
        raise HTTPException(404, "Rule not found")
    _rules_changed(conn, offer_id, user)
    audit(user.username, user.role, "RULE_UPDATE", "offer", offer_id, {"rule_id": rule_id}, request=request, conn=conn)
    conn.commit()
    return {"rule_id": rule_id, "offer_status": _get_offer(conn, offer_id)["status"]}


@router.delete("/offers/{offer_id}/rules/{rule_id}", summary="Remove an eligibility rule")
def delete_rule(offer_id: str, rule_id: int, request: Request, user: Principal = Depends(require("catalog:write")),
                conn=Depends(get_conn)) -> dict:
    if db.execute(conn, "DELETE FROM offer_eligibility_rules WHERE rule_id = :r AND offer_id = :o",
                  {"r": rule_id, "o": offer_id}) != 1:
        raise HTTPException(404, "Rule not found")
    _rules_changed(conn, offer_id, user)
    audit(user.username, user.role, "RULE_DELETE", "offer", offer_id, {"rule_id": rule_id}, request=request, conn=conn)
    conn.commit()
    return {"deleted": rule_id, "offer_status": _get_offer(conn, offer_id)["status"]}
