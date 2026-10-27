from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import db
from ..audit import audit
from ..config import get_settings
from ..ml.store import get_state
from ..security import Principal, require
from .common import Page, clean, get_conn

router = APIRouter(tags=["training"])


def next_scheduled_run(now: datetime | None = None) -> datetime:
    s = get_settings()
    tz = ZoneInfo(s.timezone)
    now = now or datetime.now(tz)
    run = now.replace(hour=s.training_cron_hour, minute=s.training_cron_minute, second=0, microsecond=0)
    return run if run > now else run + timedelta(days=1)


def _run_out(r: dict, full: bool = False) -> dict:
    out = clean(r)
    for k in ("metrics", "baseline_metrics", "active_model_metrics"):
        if k in out:
            out[k] = json.loads(r[k]) if r.get(k) and (full or k != "metrics") else (None if not r.get(k) else out[k])
    if not full and r.get("metrics"):
        m = json.loads(r["metrics"])
        out["metrics"] = {"candidate": m.get("holdout", {}).get("methods", {}).get("candidate"),
                          "batch_quality": {k: v for k, v in m.get("batch_quality", {}).items()
                                            if k not in ("generation", "compliance_examples")},
                          "ranker_validation": m.get("ranker", {}).get("validation"),
                          "ranker_decision": m.get("ranker", {}).get("decision"),
                          "serving_model": m.get("serving_model")}
    return out


@router.get("/training/status", summary="Last run, last success, next scheduled run, active model, worker health")
def status(user: Principal = Depends(require("read")), conn=Depends(get_conn)) -> dict:
    last = db.fetch_one(conn, "SELECT * FROM model_training_runs ORDER BY run_id DESC FETCH FIRST 1 ROWS ONLY")
    ok = db.fetch_one(conn, """SELECT * FROM model_training_runs WHERE status IN ('SUCCEEDED', 'REJECTED')
                               ORDER BY run_id DESC FETCH FIRST 1 ROWS ONLY""")
    active = db.fetch_one(conn, """SELECT model_version, algorithm, data_cutoff, promoted_at, run_id
                                   FROM model_registry WHERE status = 'ACTIVE'""")
    hb = get_state(conn, "WORKER_HEARTBEAT")
    s = get_settings()
    return {
        "last_run": _run_out(last) if last else None,
        "last_successful_run": _run_out(ok) if ok else None,
        "active_model": clean(active) if active else {"model_version": "baseline-rules-v1",
                                                     "algorithm": "rules baseline (no trained model promoted yet)"},
        "next_scheduled_run": next_scheduled_run().isoformat(timespec="minutes"),
        "schedule": f"daily {s.training_cron_hour:02d}:{s.training_cron_minute:02d} {s.timezone}",
        "worker_heartbeat": hb,
        "queued_or_running": int(db.scalar(conn, "SELECT COUNT(*) FROM model_training_runs "
                                                 "WHERE status IN ('QUEUED', 'RUNNING')")),
    }


@router.get("/training/runs", summary="Training history")
def runs(page: Page = Depends(), user: Principal = Depends(require("read")), conn=Depends(get_conn)) -> dict:
    total = int(db.scalar(conn, "SELECT COUNT(*) FROM model_training_runs"))
    rows = db.fetch_all(conn, """SELECT * FROM model_training_runs ORDER BY run_id DESC
                                 OFFSET :off ROWS FETCH NEXT :n ROWS ONLY""", {"off": page.offset, "n": page.page_size})
    return page.wrap([_run_out(r) for r in rows], total)


@router.get("/training/runs/{run_id}", summary="One training run with full metrics")
def run(run_id: int, user: Principal = Depends(require("read")), conn=Depends(get_conn)) -> dict:
    r = db.fetch_one(conn, "SELECT * FROM model_training_runs WHERE run_id = :r", {"r": run_id})
    if r is None:
        raise HTTPException(404, "Run not found")
    return _run_out(r, full=True)


@router.post("/training/runs", status_code=202, summary="Train Now (queued for the worker; ADMIN)")
def train_now(request: Request, user: Principal = Depends(require("training:run")), conn=Depends(get_conn)) -> dict:
    busy = db.fetch_one(conn, """SELECT run_id, status FROM model_training_runs WHERE status IN ('QUEUED', 'RUNNING')
                                 FETCH FIRST 1 ROWS ONLY""")
    if busy:
        raise HTTPException(409, f"Run {busy['run_id']} is already {busy['status']}")
    run_id = db.insert_returning_id(conn, """INSERT INTO model_training_runs (trigger_type, requested_by, max_attempts)
                                             VALUES ('MANUAL', :u, :m)""",
                                    {"u": user.username, "m": get_settings().training_max_attempts}, "run_id")
    audit(user.username, user.role, "TRAIN_NOW", "training_run", str(run_id), request=request, conn=conn)
    conn.commit()
    return {"run_id": run_id, "status": "QUEUED"}


@router.get("/models", summary="Model registry")
def models(user: Principal = Depends(require("read")), conn=Depends(get_conn)) -> list[dict]:
    return [clean(r) for r in db.fetch_all(conn, """SELECT model_version, run_id, algorithm, status, data_cutoff,
                                                           created_at, promoted_at, retired_at
                                                    FROM model_registry ORDER BY created_at DESC
                                                    FETCH FIRST 50 ROWS ONLY""")]
