from __future__ import annotations

import json
import re
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse

from .. import db
from ..audit import audit
from ..config import get_settings
from ..ingest.datasets import DATASETS
from ..ingest.importer import create_import
from ..security import Principal, require
from .common import Page, clean, get_conn

router = APIRouter(tags=["imports"])
MAX_UPLOAD_BYTES = 2 * 1024 ** 3
LOAD_ORDER = ["customers", "cards", "merchants", "transactions", "offer_targeting", "offer_outcomes", "offers",
              "eligibility_rules"]


@router.get("/imports/datasets", summary="Importable datasets, required columns and load order")
def datasets(user: Principal = Depends(require("read"))) -> list[dict]:
    return [{"dataset": n, "label": DATASETS[n].label, "load_order": i + 1, "example_file": DATASETS[n].file_hint,
             "required_columns": DATASETS[n].required_columns,
             "optional_columns": [f.name for f in DATASETS[n].fields if not f.required]}
            for i, n in enumerate(LOAD_ORDER)]


@router.get("/imports/templates/{dataset}", response_class=PlainTextResponse, summary="CSV header template")
def template(dataset: str, user: Principal = Depends(require("read"))) -> str:
    if dataset not in DATASETS:
        raise HTTPException(404, "Unknown dataset")
    return ",".join(DATASETS[dataset].field_names) + "\n"


@router.post("/imports", status_code=202, summary="Upload a CSV for validated import (processed by the worker)")
async def upload(request: Request, dataset: str = Form(...), file: UploadFile = File(...),
                 user: Principal = Depends(require("imports:write")), conn=Depends(get_conn)) -> dict:
    if dataset not in DATASETS:
        raise HTTPException(400, f"Unknown dataset; choose one of {LOAD_ORDER}")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", file.filename or "upload.csv")[:200]
    if not name.lower().endswith(".csv"):
        raise HTTPException(400, "Only .csv files are accepted")
    s = get_settings()
    s.import_dir.mkdir(parents=True, exist_ok=True)
    dest = s.import_dir / f"{uuid.uuid4().hex}_{name}"
    size = 0
    with open(dest, "wb") as out:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, "File exceeds the 2 GB upload limit; use the CLI importer")
            out.write(chunk)
    if size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "File is empty")
    import_id = create_import(conn, dataset, name, str(dest), user.username)
    audit(user.username, user.role, "IMPORT_UPLOAD", "import", str(import_id),
          {"dataset": dataset, "file": name, "bytes": size}, request=request, conn=conn)
    conn.commit()
    return {"import_id": import_id, "status": "RECEIVED", "status_url": f"/api/v1/imports/{import_id}"}


@router.get("/imports", summary="Import history")
def list_imports(page: Page = Depends(), dataset: str | None = None, user: Principal = Depends(require("read")),
                 conn=Depends(get_conn)) -> dict:
    where, binds = ("WHERE dataset = :d", {"d": dataset}) if dataset else ("", {})
    total = int(db.scalar(conn, f"SELECT COUNT(*) FROM import_batches {where}", binds))
    rows = db.fetch_all(conn, f"""SELECT import_id, dataset, file_name, status, rows_total, rows_valid, rows_rejected,
                                         rows_inserted, rows_updated, rows_unchanged, uploaded_by, created_at,
                                         started_at, finished_at, error_summary
                                  FROM import_batches {where} ORDER BY import_id DESC
                                  OFFSET :off ROWS FETCH NEXT :n ROWS ONLY""",
                        {**binds, "off": page.offset, "n": page.page_size})
    return page.wrap([_imp(r) for r in rows], total)


def _imp(r: dict) -> dict:
    out = clean(r)
    out["error_summary"] = json.loads(r["error_summary"]) if r.get("error_summary") else None
    return out


@router.get("/imports/{import_id}", summary="Import status and validation summary")
def get_import(import_id: int, user: Principal = Depends(require("read")), conn=Depends(get_conn)) -> dict:
    r = db.fetch_one(conn, """SELECT import_id, dataset, file_name, file_sha256, file_size_bytes, status, rows_total,
                                     rows_valid, rows_rejected, rows_inserted, rows_updated, rows_unchanged,
                                     uploaded_by, created_at, started_at, finished_at, error_summary
                              FROM import_batches WHERE import_id = :i""", {"i": import_id})
    if r is None:
        raise HTTPException(404, "Import not found")
    return _imp(r)


@router.get("/imports/{import_id}/rejections", summary="Rejected rows with validation errors (PII masked)")
def rejections(import_id: int, page: Page = Depends(), user: Principal = Depends(require("read")),
               conn=Depends(get_conn)) -> dict:
    total = int(db.scalar(conn, "SELECT COUNT(*) FROM import_rejections WHERE import_id = :i", {"i": import_id}))
    rows = db.fetch_all(conn, """SELECT row_number, column_name, error_code, error_message, raw_record
                                 FROM import_rejections WHERE import_id = :i ORDER BY row_number, rejection_id
                                 OFFSET :off ROWS FETCH NEXT :n ROWS ONLY""",
                        {"i": import_id, "off": page.offset, "n": page.page_size})
    if not user.can("customer_names"):
        for r in rows:
            r["raw_record"] = None
    return page.wrap(rows, total)
