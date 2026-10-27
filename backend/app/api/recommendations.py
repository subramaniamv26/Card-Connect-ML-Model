from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from .. import db
from ..audit import audit
from ..ml.store import current_batch
from ..security import Principal, require
from ..services.recommendations import serve
from .common import Page, get_conn

router = APIRouter(tags=["recommendations"])


@router.get("/customers/{customer_id}/recommendations",
            summary="Top three eligible offers for one customer",
            description="Served from the current published batch and re-validated against current offer "
                        "eligibility. Contact and device details are masked unless the caller has pii:read.")
def customer_recommendations(customer_id: str, request: Request, user: Principal = Depends(require("read")),
                             conn=Depends(get_conn)) -> dict:
    if not db.scalar(conn, "SELECT COUNT(*) FROM customers WHERE customer_id = :c", {"c": customer_id}):
        raise HTTPException(404, "Customer not found")
    batch, out = serve(conn, [customer_id], user)
    if batch is None:
        raise HTTPException(503, "No recommendation batch has been published yet; run training first")
    if user.can("pii:read"):
        audit(user.username, user.role, "PII_VIEW", "customer", customer_id, request=request, conn=conn)
        conn.commit()
    if customer_id not in out:
        return {"customer": {"customer_id": customer_id}, "recommendation_status": "NONE",
                "shortfall_reason": "Customer was not present when the current batch was generated", "offers": [],
                "batch_id": batch["batch_id"], "model_version": batch["model_version"]}
    return out[customer_id]


@router.get("/recommendations", summary="Recommendations for all customers (paginated, filterable)")
def list_recommendations(page: Page = Depends(), status: str | None = Query(None, pattern="^(FULL|PARTIAL|NONE)$"),
                         district: str | None = None, province: str | None = None, segment: str | None = None,
                         history_level: str | None = Query(None, pattern="^(NONE|LIMITED|ESTABLISHED)$"),
                         offer_id: str | None = None, customer_id_prefix: str | None = Query(None, max_length=20),
                         user: Principal = Depends(require("read")), conn=Depends(get_conn)) -> dict:
    batch = current_batch(conn)
    if batch is None:
        raise HTTPException(503, "No recommendation batch has been published yet")
    where, binds = ["s.batch_id = :b"], {"b": batch["batch_id"]}
    for col, val, key in (("s.status", status, "st"), ("c.customer_home_district", district, "d"),
                          ("c.customer_home_province", province, "p"), ("c.customer_segment", segment, "sg"),
                          ("s.history_level", history_level, "h")):
        if val:
            where.append(f"{col} = :{key}")
            binds[key] = val
    if customer_id_prefix:
        where.append("s.customer_id LIKE :cp ESCAPE '\\'")
        binds["cp"] = customer_id_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    if offer_id:
        where.append("""EXISTS (SELECT 1 FROM customer_recommendations r WHERE r.batch_id = s.batch_id
                        AND r.customer_id = s.customer_id AND r.offer_id = :o AND r.candidate_rank <= 3)""")
        binds["o"] = offer_id
    base = f"FROM customer_rec_status s JOIN customers c ON c.customer_id = s.customer_id WHERE {' AND '.join(where)}"
    total = int(db.scalar(conn, f"SELECT COUNT(*) {base}", binds))
    ids = [r["customer_id"] for r in db.fetch_all(conn, f"""SELECT s.customer_id {base} ORDER BY s.customer_id
                                                            OFFSET :off ROWS FETCH NEXT :n ROWS ONLY""",
                                                    {**binds, "off": page.offset, "n": page.page_size})]
    _, out = serve(conn, ids, user)
    result = page.wrap([out[i] for i in ids if i in out], total)
    result.update({"batch_id": batch["batch_id"], "model_version": batch["model_version"],
                   "data_as_of": batch["data_as_of"].date().isoformat(),
                   "generated_at": batch["generated_at"].isoformat() + "Z"})
    return result
