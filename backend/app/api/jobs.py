from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import db
from ..audit import audit
from ..config import get_settings
from ..security import Principal, require
from .common import Page, clean, get_conn

router = APIRouter(tags=["bulk jobs"])


class JobFilters(BaseModel):
    status: str | None = Field(None, pattern="^(FULL|PARTIAL|NONE)$")
    district: str | None = Field(None, max_length=40)
    province: str | None = Field(None, max_length=40)
    segment: str | None = Field(None, max_length=30)
    offer_id: str | None = Field(None, max_length=30)


class JobIn(BaseModel):
    job_type: str = Field("RECOMMENDATION_EXPORT", pattern="^(RECOMMENDATION_EXPORT|RECOMMENDATION_REFRESH)$",
                          description="EXPORT writes the current batch to a downloadable file; REFRESH regenerates "
                                      "and atomically republishes recommendations with the active model.")
    format: str = Field("csv", pattern="^(csv|jsonl)$")
    include_pii: bool = Field(False, description="Unmasked contact/device details (ADMIN only, audited)")
    filters: JobFilters = JobFilters()


@router.post("/recommendation-jobs", status_code=202, summary="Start an asynchronous bulk job")
def create_job(body: JobIn, request: Request, user: Principal = Depends(require("exports:create")),
               conn=Depends(get_conn)) -> dict:
    if body.include_pii and not user.can("exports:pii"):
        raise HTTPException(403, "include_pii requires the ADMIN role")
    if body.job_type == "RECOMMENDATION_REFRESH" and not user.can("training:run"):
        raise HTTPException(403, "Refreshing recommendations requires the ADMIN role")
    if body.job_type == "RECOMMENDATION_EXPORT" and not db.scalar(
            conn, "SELECT COUNT(*) FROM app_state WHERE state_key = 'CURRENT_BATCH_ID'"):
        raise HTTPException(409, "No recommendation batch has been published yet")
    job_id = db.insert_returning_id(conn, """
        INSERT INTO bulk_jobs (job_type, parameters, requested_by, requested_role, include_pii)
        VALUES (:t, :p, :u, :r, :pii)""",
        {"t": body.job_type, "p": json.dumps({"format": body.format, "filters": body.filters.model_dump()}),
         "u": user.username, "r": user.role, "pii": "Y" if body.include_pii else "N"}, "job_id")
    audit(user.username, user.role, "BULK_JOB_CREATE", "bulk_job", str(job_id),
          {"job_type": body.job_type, "include_pii": body.include_pii}, request=request, conn=conn)
    conn.commit()
    return {"job_id": job_id, "status": "QUEUED", "status_url": f"/api/v1/recommendation-jobs/{job_id}"}


def _visible(job: dict, user: Principal) -> bool:
    return user.role == "ADMIN" or job["requested_by"] == user.username


@router.get("/recommendation-jobs", summary="List bulk jobs (own jobs; ADMIN sees all)")
def list_jobs(page: Page = Depends(), user: Principal = Depends(require("read")), conn=Depends(get_conn)) -> dict:
    where, binds = ("", {}) if user.role == "ADMIN" else ("WHERE requested_by = :u", {"u": user.username})
    total = int(db.scalar(conn, f"SELECT COUNT(*) FROM bulk_jobs {where}", binds))
    rows = db.fetch_all(conn, f"""SELECT job_id, job_type, status, parameters, requested_by, include_pii, batch_id,
                                         progress_pct, row_count, error_message, created_at, started_at, finished_at,
                                         expires_at
                                  FROM bulk_jobs {where} ORDER BY job_id DESC
                                  OFFSET :off ROWS FETCH NEXT :n ROWS ONLY""",
                        {**binds, "off": page.offset, "n": page.page_size})
    return page.wrap([_job_out(r) for r in rows], total)


def _job_out(r: dict) -> dict:
    out = clean(r)
    out["parameters"] = json.loads(r["parameters"]) if r.get("parameters") else None
    out["download_url"] = (f"/api/v1/recommendation-jobs/{r['job_id']}/download"
                           if r["status"] == "SUCCEEDED" and r["job_type"] == "RECOMMENDATION_EXPORT" else None)
    return out


@router.get("/recommendation-jobs/{job_id}", summary="Job status")
def get_job(job_id: int, user: Principal = Depends(require("read")), conn=Depends(get_conn)) -> dict:
    r = db.fetch_one(conn, "SELECT * FROM bulk_jobs WHERE job_id = :j", {"j": job_id})
    if r is None or not _visible(r, user):
        raise HTTPException(404, "Job not found")
    r.pop("result_path", None)
    return _job_out(r)


@router.get("/recommendation-jobs/{job_id}/download", summary="Download an export (requester or ADMIN)")
def download(job_id: int, request: Request, user: Principal = Depends(require("exports:create")),
             conn=Depends(get_conn)):
    r = db.fetch_one(conn, "SELECT * FROM bulk_jobs WHERE job_id = :j", {"j": job_id})
    if r is None or not _visible(r, user):
        raise HTTPException(404, "Job not found")
    if r["status"] != "SUCCEEDED" or not r["result_path"]:
        raise HTTPException(409, f"Job is {r['status']}")
    if db.scalar(conn, "SELECT COUNT(*) FROM bulk_jobs WHERE job_id = :j AND expires_at < SYS_EXTRACT_UTC(SYSTIMESTAMP)",
                 {"j": job_id}):
        raise HTTPException(410, "Export has expired")
    path = Path(r["result_path"]).resolve()
    if get_settings().export_dir.resolve() not in path.parents or not path.is_file():
        raise HTTPException(410, "Export file is no longer available")
    audit(user.username, user.role, "EXPORT_DOWNLOAD", "bulk_job", str(job_id),
          {"include_pii": r["include_pii"] == "Y", "rows": r["row_count"]}, request=request, conn=conn)
    conn.commit()
    return FileResponse(path, filename=path.name,
                        media_type="text/csv" if path.suffix == ".csv" else "application/x-ndjson")
