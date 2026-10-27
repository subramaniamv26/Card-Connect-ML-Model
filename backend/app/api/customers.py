"""Dashboard overview, customer search and spending profiles."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import db
from ..audit import audit
from ..masking import mask_device, mask_name, mask_phone
from ..ml.store import current_batch
from ..ml.windows import compute_windows
from ..security import Principal, require
from .common import Page, clean, get_conn

router = APIRouter(tags=["customers & dashboard"])
_OVERVIEW: dict = {"at": 0.0, "data": None}


@router.get("/dashboard/overview", summary="Headline counts and recommendation coverage")
def overview(user: Principal = Depends(require("read")), conn=Depends(get_conn)) -> dict:
    if _OVERVIEW["data"] is None or time.time() - _OVERVIEW["at"] > 30:
        counts = db.fetch_one(conn, """SELECT (SELECT COUNT(*) FROM customers) customers,
                                              (SELECT COUNT(*) FROM cards) cards,
                                              (SELECT COUNT(*) FROM merchants) merchants,
                                              (SELECT COUNT(*) FROM card_transactions) transactions,
                                              (SELECT MAX(transaction_datetime) FROM card_transactions) last_txn,
                                              (SELECT COUNT(*) FROM offers WHERE status = 'APPROVED'
                                                 AND TRUNC(SYSDATE) BETWEEN valid_from AND valid_to) live_offers,
                                              (SELECT COUNT(*) FROM offers WHERE status = 'PENDING_APPROVAL') pending_offers,
                                              (SELECT COUNT(*) FROM offers) catalog_offers
                                       FROM dual""")
        batch = current_batch(conn)
        cov = None
        if batch:
            n = batch["customer_count"] or 0
            cov = {"batch_id": batch["batch_id"], "model_version": batch["model_version"],
                   "generated_at": batch["generated_at"].isoformat() + "Z", "data_as_of": batch["data_as_of"].date().isoformat(),
                   "customers": n, "full": batch["customers_full"], "partial": batch["customers_partial"],
                   "none": batch["customers_none"],
                   "coverage_any": round((batch["customers_full"] + batch["customers_partial"]) / n, 4) if n else 0,
                   "coverage_full": round(batch["customers_full"] / n, 4) if n else 0}
        top = []
        if batch:
            top = db.fetch_all(conn, """SELECT r.offer_id, o.offer_title, o.merchant_category, COUNT(*) customers
                                        FROM customer_recommendations r JOIN offers o ON o.offer_id = r.offer_id
                                        WHERE r.batch_id = :b AND r.candidate_rank <= 3
                                        GROUP BY r.offer_id, o.offer_title, o.merchant_category
                                        ORDER BY customers DESC FETCH FIRST 10 ROWS ONLY""", {"b": batch["batch_id"]})
        cats = db.fetch_all(conn, """SELECT m.merchant_category category, COUNT(*) txns, SUM(t.amount_lkr) spend_lkr
                                     FROM card_transactions t JOIN merchants m ON m.merchant_id = t.merchant_id
                                     WHERE t.transaction_status = 'APPROVED'
                                       AND t.transaction_datetime >= (SELECT MAX(transaction_datetime) - 30
                                                                      FROM card_transactions)
                                     GROUP BY m.merchant_category ORDER BY spend_lkr DESC""")
        _OVERVIEW.update(at=time.time(), data={
            "counts": clean(counts), "recommendations": cov, "top_recommended_offers": top,
            "category_spend_last_30_days": [{**c, "spend_lkr": float(c["spend_lkr"] or 0)} for c in cats]})
    return _OVERVIEW["data"]


@router.get("/customers", summary="Search customers")
def search(page: Page = Depends(), q: str | None = Query(None, max_length=60), district: str | None = None,
           segment: str | None = None, status: str | None = Query(None, pattern="^(FULL|PARTIAL|NONE)$"),
           user: Principal = Depends(require("read")), conn=Depends(get_conn)) -> dict:
    batch = current_batch(conn)
    where, binds = ["1 = 1"], {"b": batch["batch_id"] if batch else -1}
    if q:
        if user.can("customer_names"):
            where.append("(c.customer_id LIKE :q OR UPPER(c.customer_name) LIKE :qn)")
            binds.update(q=f"%{q.upper()}%", qn=f"%{q.upper()}%")
        else:
            where.append("c.customer_id LIKE :q")
            binds["q"] = f"%{q.upper()}%"
    if district:
        where.append("c.customer_home_district = :d"); binds["d"] = district
    if segment:
        where.append("c.customer_segment = :s"); binds["s"] = segment
    if status:
        where.append("s.status = :st"); binds["st"] = status
    w = " AND ".join(where)
    base = f"FROM customers c LEFT JOIN customer_rec_status s ON s.customer_id = c.customer_id AND s.batch_id = :b WHERE {w}"
    total = int(db.scalar(conn, f"SELECT COUNT(*) {base}", binds))
    rows = db.fetch_all(conn, f"""SELECT c.customer_id, c.customer_name, c.customer_segment, c.customer_home_city,
                                         c.customer_home_district, c.customer_home_province, c.customer_tenure_months,
                                         c.number_of_cards, s.status recommendation_status, s.history_level
                                  {base} ORDER BY c.customer_id OFFSET :off ROWS FETCH NEXT :n ROWS ONLY""",
                        {**binds, "off": page.offset, "n": page.page_size})
    if not user.can("customer_names"):
        for r in rows:
            r["customer_name"] = mask_name(r["customer_name"])
    return page.wrap(rows, total)


@router.get("/customers/{customer_id}/profile", summary="Customer spending profile (last 12 months to data cutoff)")
def profile(customer_id: str, request: Request, user: Principal = Depends(require("read")),
            conn=Depends(get_conn)) -> dict:
    c = db.fetch_one(conn, """SELECT customer_id, customer_name, customer_segment, customer_home_city,
                                     customer_home_district, customer_home_province, customer_tenure_months,
                                     number_of_cards, customer_contact_no FROM customers WHERE customer_id = :c""",
                     {"c": customer_id})
    if c is None:
        raise HTTPException(404, "Customer not found")
    cutoff = db.scalar(conn, "SELECT MAX(transaction_datetime) FROM card_transactions")
    if cutoff is None:
        raise HTTPException(409, "No transactions loaded")
    w = compute_windows(cutoff.date())
    b = {"c": customer_id, "s": w.window_start_dt, "e": w.window_end_dt}
    net = "CASE WHEN t.transaction_type = 'REFUND' THEN -t.amount_lkr ELSE t.amount_lkr END"
    counted = "t.transaction_type IN ('PURCHASE','BILL_PAYMENT','RECURRING_PAYMENT')"
    valid = (f"t.customer_id = :c AND t.transaction_status = 'APPROVED' AND t.transaction_type <> 'REVERSAL' "
             f"AND NOT EXISTS (SELECT 1 FROM card_transactions r WHERE r.related_transaction_id = t.transaction_id "
             f"AND r.transaction_type = 'REVERSAL' AND r.transaction_status = 'APPROVED') "
             f"AND t.transaction_datetime >= :s AND t.transaction_datetime < :e")
    periods = db.fetch_one(conn, f"""
        SELECT SUM(CASE WHEN t.transaction_datetime >= :e - 30 AND {counted} THEN 1 ELSE 0 END) cnt_30,
               SUM(CASE WHEN t.transaction_datetime >= :e - 30 THEN {net} ELSE 0 END) spend_30,
               SUM(CASE WHEN t.transaction_datetime >= :e - 90 AND {counted} THEN 1 ELSE 0 END) cnt_90,
               SUM(CASE WHEN t.transaction_datetime >= :e - 90 THEN {net} ELSE 0 END) spend_90,
               SUM(CASE WHEN {counted} THEN 1 ELSE 0 END) cnt_365, SUM({net}) spend_365,
               MAX(t.transaction_datetime) last_txn, MIN(t.transaction_datetime) first_txn
        FROM card_transactions t WHERE {valid}""", b)
    cats = db.fetch_all(conn, f"""SELECT m.merchant_category category, SUM(CASE WHEN {counted} THEN 1 ELSE 0 END) txns,
                                         SUM({net}) spend_lkr
                                  FROM card_transactions t JOIN merchants m ON m.merchant_id = t.merchant_id
                                  WHERE {valid} GROUP BY m.merchant_category ORDER BY spend_lkr DESC""", b)
    brands = db.fetch_all(conn, f"""SELECT m.merchant_brand brand, m.merchant_category category,
                                           SUM(CASE WHEN {counted} THEN 1 ELSE 0 END) txns, SUM({net}) spend_lkr,
                                           MAX(t.transaction_datetime) last_txn
                                    FROM card_transactions t JOIN merchants m ON m.merchant_id = t.merchant_id
                                    WHERE {valid} GROUP BY m.merchant_brand, m.merchant_category
                                    ORDER BY txns DESC, spend_lkr DESC FETCH FIRST 10 ROWS ONLY""", b)
    monthly = db.fetch_all(conn, f"""SELECT TO_CHAR(t.transaction_datetime, 'YYYY-MM') month,
                                            SUM(CASE WHEN {counted} THEN 1 ELSE 0 END) txns, SUM({net}) spend_lkr
                                     FROM card_transactions t WHERE {valid}
                                     GROUP BY TO_CHAR(t.transaction_datetime, 'YYYY-MM') ORDER BY month""", b)
    tod = db.fetch_all(conn, f"""SELECT CASE WHEN TO_NUMBER(TO_CHAR(t.transaction_datetime, 'HH24')) BETWEEN 5 AND 11 THEN 'MORNING'
                                             WHEN TO_NUMBER(TO_CHAR(t.transaction_datetime, 'HH24')) BETWEEN 12 AND 16 THEN 'DAY'
                                             WHEN TO_NUMBER(TO_CHAR(t.transaction_datetime, 'HH24')) BETWEEN 17 AND 20 THEN 'EVENING'
                                             ELSE 'NIGHT' END time_of_day, COUNT(*) txns
                                  FROM card_transactions t WHERE {valid} AND {counted}
                                  GROUP BY CASE WHEN TO_NUMBER(TO_CHAR(t.transaction_datetime, 'HH24')) BETWEEN 5 AND 11 THEN 'MORNING'
                                             WHEN TO_NUMBER(TO_CHAR(t.transaction_datetime, 'HH24')) BETWEEN 12 AND 16 THEN 'DAY'
                                             WHEN TO_NUMBER(TO_CHAR(t.transaction_datetime, 'HH24')) BETWEEN 17 AND 20 THEN 'EVENING'
                                             ELSE 'NIGHT' END""", b)
    dow = db.fetch_all(conn, f"""SELECT TO_CHAR(t.transaction_datetime, 'DY', 'NLS_DATE_LANGUAGE=ENGLISH') day_of_week,
                                        COUNT(*) txns
                                 FROM card_transactions t WHERE {valid} AND {counted}
                                 GROUP BY TO_CHAR(t.transaction_datetime, 'DY', 'NLS_DATE_LANGUAGE=ENGLISH')""", b)
    cards = db.fetch_all(conn, """SELECT k.card_id, k.card_network, k.card_type, k.card_tier, k.card_status,
                                         (SELECT COUNT(*) FROM card_transactions t WHERE t.card_id = k.card_id
                                            AND t.transaction_status = 'APPROVED' AND t.transaction_datetime >= :s
                                            AND t.transaction_datetime < :e) txns
                                  FROM cards k WHERE k.customer_id = :c ORDER BY k.card_id""", b)
    devices = db.fetch_all(conn, """SELECT mobile_device_id, device_type, device_os, is_primary FROM customer_devices
                                    WHERE customer_id = :c ORDER BY is_primary DESC""", {"c": customer_id})
    pii = user.can("pii:read")
    if pii:
        audit(user.username, user.role, "PII_VIEW", "customer", customer_id, request=request, conn=conn)
        conn.commit()
    num = lambda v: float(v or 0)  # noqa: E731
    return {
        "customer": {**{k: v for k, v in c.items() if k not in ("customer_name", "customer_contact_no")},
                     "customer_name": c["customer_name"] if user.can("customer_names") else mask_name(c["customer_name"]),
                     "customer_contact_no": c["customer_contact_no"] if pii else mask_phone(c["customer_contact_no"])},
        "window": {"start": w.window_start.isoformat(), "end_exclusive": w.window_end.isoformat(),
                   "data_cutoff": w.data_cutoff.isoformat(), "currency": "LKR",
                   "basis": "Approved transactions; refunds subtract, reversals cancel the original; USD converted at the "
                            "recorded FX rate."},
        "rfm": {"cnt_30": int(periods["cnt_30"] or 0), "spend_30": num(periods["spend_30"]),
                "cnt_90": int(periods["cnt_90"] or 0), "spend_90": num(periods["spend_90"]),
                "cnt_365": int(periods["cnt_365"] or 0), "spend_365": num(periods["spend_365"]),
                "last_transaction": clean({"x": periods["last_txn"]})["x"],
                "recency_days": (w.window_end_dt - periods["last_txn"]).days if periods["last_txn"] else None,
                "average_ticket": round(num(periods["spend_365"]) / periods["cnt_365"], 2) if periods["cnt_365"] else None},
        "categories": [{**r, "spend_lkr": num(r["spend_lkr"])} for r in cats],
        "top_brands": [{**clean(r), "spend_lkr": num(r["spend_lkr"])} for r in brands],
        "monthly": [{**r, "spend_lkr": num(r["spend_lkr"])} for r in monthly],
        "time_of_day": tod, "day_of_week": dow,
        "cards": cards,
        "devices": [{**d, "mobile_device_id": d["mobile_device_id"] if pii else mask_device(d["mobile_device_id"])}
                    for d in devices],
    }

