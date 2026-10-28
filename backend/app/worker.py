"""Worker / scheduler process: daily training at 02:00 Asia/Colombo, queued manual training, bulk jobs and
CSV imports. Run it in its own terminal (python -m app.worker); the API only puts work in queue tables.

* Overlap prevention: APScheduler max_instances=1 + coalesce, and a DB lease lock (JOB_LOCKS) shared by all
  worker replicas. A run that cannot get the lock stays QUEUED and is picked up later.
* Retries: a failed training attempt is re-queued with a delay until max_attempts is reached; the previous
  ACTIVE model and published batch remain in service throughout.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import signal
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import db, locks
from .config import get_settings
from .ingest.importer import run_import
from .logging_conf import configure_logging
from .masking import mask_text
from .ml.pipeline import refresh_recommendations, run_training
from .ml.store import current_batch, set_state
from .security import Principal
from .services.recommendations import serve

log = logging.getLogger("worker")
_stop = threading.Event()


# ---- scheduling --------------------------------------------------------------------------------
def enqueue_scheduled_training() -> None:
    with db.connection() as conn:
        busy = db.scalar(conn, "SELECT COUNT(*) FROM model_training_runs WHERE status IN ('QUEUED','RUNNING')")
        if busy:
            log.warning("Scheduled training skipped: a run is already queued or running")
            db.execute(conn, """INSERT INTO model_training_runs (trigger_type, requested_by, status, error_message,
                                finished_at) VALUES ('SCHEDULED', 'scheduler', 'SKIPPED',
                                'Another run was queued or running', SYS_EXTRACT_UTC(SYSTIMESTAMP))""")
        else:
            db.execute(conn, """INSERT INTO model_training_runs (trigger_type, requested_by, max_attempts)
                                VALUES ('SCHEDULED', 'scheduler', :m)""", {"m": get_settings().training_max_attempts})
        conn.commit()


def process_training_queue() -> None:
    s = get_settings()
    with db.connection() as conn:
        run = db.fetch_one(conn, """SELECT run_id, attempt, max_attempts FROM model_training_runs
                                    WHERE status = 'QUEUED' AND (started_at IS NULL OR started_at < SYS_EXTRACT_UTC(SYSTIMESTAMP) - :d)
                                    ORDER BY run_id FETCH FIRST 1 ROWS ONLY""",
                           {"d": timedelta(seconds=s.training_retry_delay_seconds)})
    if run is None:
        return
    try:
        with locks.held("TRAINING", s.lock_lease_seconds):
            attempt = run["attempt"] + 1
            with db.connection() as conn:
                db.execute(conn, "UPDATE model_training_runs SET attempt = :a, error_message = NULL WHERE run_id = :r",
                           {"a": attempt, "r": run["run_id"]})
                conn.commit()
            log.info("Training run %s attempt %s/%s", run["run_id"], attempt, run["max_attempts"])
            try:
                result = run_training(run["run_id"])
                log.info("Training run %s finished: %s", run["run_id"], result.get("status"))
            except Exception as exc:  # noqa: BLE001
                msg = mask_text(f"{type(exc).__name__}: {exc}")[:3900]
                log.exception("Training run %s attempt %s failed", run["run_id"], attempt)
                final = attempt >= run["max_attempts"]
                with db.connection() as conn:
                    db.execute(conn, """UPDATE model_training_runs SET status = :s, error_message = :m,
                                        finished_at = CASE WHEN :s = 'FAILED' THEN SYS_EXTRACT_UTC(SYSTIMESTAMP) END,
                                        duration_seconds = (CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS DATE) - CAST(started_at AS DATE)) * 86400
                                        WHERE run_id = :r""",
                               {"s": "FAILED" if final else "QUEUED", "m": msg, "r": run["run_id"]})
                    conn.commit()
    except locks.LockBusy:
        log.info("Training lock busy; run %s stays queued", run["run_id"])


# ---- imports -----------------------------------------------------------------------------------
def process_import_queue() -> None:
    with db.connection() as conn:
        job = db.fetch_one(conn, """SELECT import_id, dataset, file_path, uploaded_by FROM import_batches
                                    WHERE status = 'RECEIVED' ORDER BY import_id FETCH FIRST 1 ROWS ONLY""")
    if job is None:
        return
    try:
        with locks.held("IMPORTS", get_settings().lock_lease_seconds):
            with db.connection() as conn:
                run_import(conn, job["import_id"], job["dataset"], Path(job["file_path"]), job["uploaded_by"])
    except locks.LockBusy:
        pass
    except Exception:  # run_import already marked the batch FAILED
        log.exception("Import %s failed", job["import_id"])


# ---- bulk jobs ---------------------------------------------------------------------------------
EXPORT_COLUMNS = ["customer_id", "customer_name", "mobile_device_id", "customer_contact_no", "customer_home_city",
                  "customer_home_district", "customer_home_province", "recommendation_status", "shortfall_reason",
                  "rank", "offer_id", "offer_title", "offer_type", "offer_value", "minimum_spend", "maximum_benefit",
                  "estimated_customer_saving", "currency", "valid_from", "valid_to", "recommendation_score",
                  "recommendation_reason", "card_id", "card_network", "card_type", "merchant_id", "merchant_brand",
                  "merchant_city", "merchant_district", "merchant_province", "merchant_type",
                  "merchant_transaction_amount_last_3_months", "merchant_transaction_count_last_3_months",
                  "last_transaction_date_with_merchant", "average_transaction_amount_with_merchant",
                  "aggregation_period_start", "aggregation_period_end", "generated_at", "data_as_of", "model_version"]


def _flatten(resp: dict) -> list[dict]:
    c = resp["customer"]
    base = {k: c.get(k) for k in ("customer_id", "customer_name", "mobile_device_id", "customer_contact_no",
                                  "customer_home_city", "customer_home_district", "customer_home_province")}
    base.update(recommendation_status=resp["recommendation_status"], shortfall_reason=resp["shortfall_reason"],
                generated_at=resp["generated_at"], data_as_of=resp["data_as_of"], model_version=resp["model_version"],
                aggregation_period_start=resp["aggregation_period_start"],
                aggregation_period_end=resp["aggregation_period_end"])
    if not resp["offers"]:
        return [base]
    rows = []
    for o in resp["offers"]:
        r = dict(base)
        r.update({k: o.get(k) for k in ("rank", "offer_id", "offer_title", "offer_type", "offer_value", "minimum_spend",
                                        "maximum_benefit", "estimated_customer_saving", "currency", "valid_from",
                                        "valid_to", "recommendation_score", "recommendation_reason")})
        r.update(card_id=o["card"]["card_id"], card_network=o["card"]["card_network"], card_type=o["card"]["card_type"])
        r.update({k: o["merchant"][k] for k in ("merchant_id", "merchant_brand", "merchant_city", "merchant_district",
                                                "merchant_province", "merchant_type")})
        r.update({k: o["customer_merchant_spend"][k] for k in (
            "merchant_transaction_amount_last_3_months", "merchant_transaction_count_last_3_months",
            "last_transaction_date_with_merchant", "average_transaction_amount_with_merchant")})
        rows.append(r)
    return rows


def run_export(job: dict) -> None:
    s = get_settings()
    params = json.loads(job["parameters"] or "{}")
    filters, fmt = params.get("filters", {}), params.get("format", "csv")
    # Exports run with the requester's role; PII only when requested by an ADMIN.
    role = job["requested_role"] if job["include_pii"] == "Y" else ("ANALYST" if job["requested_role"] != "READ_ONLY"
                                                                     else "READ_ONLY")
    principal = Principal(job["requested_by"], role)
    s.export_dir.mkdir(parents=True, exist_ok=True)
    path = s.export_dir / f"recommendations_job{job['job_id']}.{fmt}"
    with db.connection() as conn:
        batch = current_batch(conn)
        where, binds = ["s.batch_id = :b"], {"b": batch["batch_id"]}
        for col, key in (("s.status", "status"), ("c.customer_home_district", "district"),
                         ("c.customer_home_province", "province"), ("c.customer_segment", "segment")):
            if filters.get(key):
                where.append(f"{col} = :{key}")
                binds[key] = filters[key]
        if filters.get("offer_id"):
            where.append("""EXISTS (SELECT 1 FROM customer_recommendations r WHERE r.batch_id = s.batch_id
                            AND r.customer_id = s.customer_id AND r.offer_id = :o AND r.candidate_rank <= 3)""")
            binds["o"] = filters["offer_id"]
        ids = [r["customer_id"] for r in db.fetch_all(conn, f"""
            SELECT s.customer_id FROM customer_rec_status s JOIN customers c ON c.customer_id = s.customer_id
            WHERE {' AND '.join(where)} ORDER BY s.customer_id""", binds, arraysize=20000)]
        db.execute(conn, "UPDATE bulk_jobs SET batch_id = :b WHERE job_id = :j", {"b": batch["batch_id"], "j": job["job_id"]})
        conn.commit()
        n_rows = 0
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=EXPORT_COLUMNS, extrasaction="ignore") if fmt == "csv" else None
            if writer:
                writer.writeheader()
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                _, out = serve(conn, chunk, principal)
                for cid in chunk:
                    if cid not in out:
                        continue
                    if writer:
                        for row in _flatten(out[cid]):
                            writer.writerow(row)
                            n_rows += 1
                    else:
                        fh.write(json.dumps(out[cid], default=str) + "\n")
                        n_rows += 1
                db.execute(conn, "UPDATE bulk_jobs SET progress_pct = :p WHERE job_id = :j",
                           {"p": round(100 * min(i + 500, len(ids)) / max(len(ids), 1), 1), "j": job["job_id"]})
                conn.commit()
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        db.execute(conn, """UPDATE bulk_jobs SET status = 'SUCCEEDED', progress_pct = 100, row_count = :n,
                            result_path = :p, result_sha256 = :h, finished_at = SYS_EXTRACT_UTC(SYSTIMESTAMP),
                            expires_at = SYS_EXTRACT_UTC(SYSTIMESTAMP) + :ttl WHERE job_id = :j""",
                   {"n": n_rows, "p": str(path), "h": sha, "ttl": timedelta(hours=s.export_ttl_hours), "j": job["job_id"]})
        conn.commit()


def process_job_queue() -> None:
    with db.connection() as conn:
        job = db.fetch_one(conn, "SELECT * FROM bulk_jobs WHERE status = 'QUEUED' ORDER BY job_id FETCH FIRST 1 ROWS ONLY")
    if job is None:
        return
    lock = "TRAINING" if job["job_type"] == "RECOMMENDATION_REFRESH" else "BULK_JOBS"
    try:
        with locks.held(lock, get_settings().lock_lease_seconds):
            with db.connection() as conn:
                db.execute(conn, "UPDATE bulk_jobs SET status = 'RUNNING', started_at = SYS_EXTRACT_UTC(SYSTIMESTAMP) WHERE job_id = :j",
                           {"j": job["job_id"]})
                conn.commit()
            try:
                if job["job_type"] == "RECOMMENDATION_EXPORT":
                    run_export(job)
                else:
                    res = refresh_recommendations()
                    with db.connection() as conn:
                        db.execute(conn, """UPDATE bulk_jobs SET status = 'SUCCEEDED', progress_pct = 100, batch_id = :b,
                                            finished_at = SYS_EXTRACT_UTC(SYSTIMESTAMP) WHERE job_id = :j""",
                                   {"b": res["batch_id"], "j": job["job_id"]})
                        conn.commit()
            except Exception as exc:  # noqa: BLE001
                log.exception("Bulk job %s failed", job["job_id"])
                with db.connection() as conn:
                    db.execute(conn, """UPDATE bulk_jobs SET status = 'FAILED', error_message = :m, finished_at = SYS_EXTRACT_UTC(SYSTIMESTAMP)
                                        WHERE job_id = :j""", {"m": mask_text(str(exc))[:3900], "j": job["job_id"]})
                    conn.commit()
    except locks.LockBusy:
        pass


def expire_exports() -> None:
    with db.connection() as conn:
        for r in db.fetch_all(conn, """SELECT job_id, result_path FROM bulk_jobs WHERE status = 'SUCCEEDED'
                                       AND expires_at < SYS_EXTRACT_UTC(SYSTIMESTAMP)"""):
            if r["result_path"]:
                Path(r["result_path"]).unlink(missing_ok=True)
            db.execute(conn, "UPDATE bulk_jobs SET status = 'EXPIRED', result_path = NULL WHERE job_id = :j",
                       {"j": r["job_id"]})
        conn.commit()


def heartbeat() -> None:
    """Shown on the Training page so you can see the worker is alive."""
    with db.connection() as conn:
        set_state(conn, "WORKER_HEARTBEAT", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        conn.commit()


def _safe(fn):
    def run():
        try:
            fn()
        except Exception:  # noqa: BLE001 - keep the scheduler alive
            log.exception("%s failed", fn.__name__)
    run.__name__ = fn.__name__
    return run


def main() -> None:
    s = get_settings()
    configure_logging(s.log_level)
    tz = ZoneInfo(s.timezone)
    sched = BackgroundScheduler(timezone=tz, job_defaults={"coalesce": True, "max_instances": 1,
                                                           "misfire_grace_time": 3600})
    sched.add_job(_safe(enqueue_scheduled_training), CronTrigger(hour=s.training_cron_hour,
                                                                 minute=s.training_cron_minute, timezone=tz),
                  id="daily-training")
    sched.add_job(_safe(process_training_queue), "interval", seconds=s.worker_poll_seconds, id="training-queue")
    sched.add_job(_safe(process_import_queue), "interval", seconds=s.worker_poll_seconds, id="import-queue")
    sched.add_job(_safe(process_job_queue), "interval", seconds=s.worker_poll_seconds, id="job-queue")
    sched.add_job(_safe(heartbeat), "interval", seconds=30, id="heartbeat")
    sched.add_job(_safe(expire_exports), "interval", minutes=30, id="expire-exports")
    sched.start()
    _safe(heartbeat)()
    log.info("Worker started; daily training at %02d:%02d %s; next run %s", s.training_cron_hour,
             s.training_cron_minute, s.timezone, sched.get_job("daily-training").next_run_time)
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())
    signal.signal(signal.SIGINT, lambda *_: _stop.set())
    while not _stop.is_set():
        time.sleep(1)
    sched.shutdown(wait=True)
    from . import db as _db
    _db.close_pool()


if __name__ == "__main__":
    main()
