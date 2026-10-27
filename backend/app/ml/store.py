"""Model artifacts, registry and recommendation batch persistence.

Atomic publication: rows are written under a new batch_id while the batch is BUILDING (readers never see
it). Publication is ONE transaction that marks the old batch SUPERSEDED, the new one PUBLISHED and moves
APP_STATE.CURRENT_BATCH_ID. Readers resolve the current batch id first, so they always see a complete batch.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

import joblib
import oracledb
import pandas as pd

from .. import db
from .model import RecModel, baseline_model

log = logging.getLogger(__name__)
KEEP_BATCHES = 2


# ---- app state ---------------------------------------------------------------------------------
def get_state(conn, key: str) -> str | None:
    return db.scalar(conn, "SELECT state_value FROM app_state WHERE state_key = :k", {"k": key})


def set_state(conn, key: str, value: str | None) -> None:
    db.execute(conn, """MERGE INTO app_state a USING (SELECT :k state_key, :v state_value FROM dual) s
                        ON (a.state_key = s.state_key)
                        WHEN MATCHED THEN UPDATE SET a.state_value = s.state_value, a.updated_at = SYS_EXTRACT_UTC(SYSTIMESTAMP)
                        WHEN NOT MATCHED THEN INSERT (state_key, state_value) VALUES (s.state_key, s.state_value)""",
               {"k": key, "v": value})


# ---- artifacts ---------------------------------------------------------------------------------
def save_model(model: RecModel, model_dir: Path) -> tuple[Path, str]:
    path = (model_dir / model.version / "model.joblib").resolve()  # registry stores absolute paths
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    (path.parent / "metadata.json").write_text(json.dumps(
        {"version": model.version, "algorithm": model.algorithm, "sha256": sha, "metrics": model.metrics},
        indent=2, default=str), encoding="utf-8")
    return path, sha


def load_model(path: str, expected_sha: str) -> RecModel:
    """Only artifacts written by this service are loaded, and only if the checksum matches the registry."""
    data = Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha:
        raise RuntimeError(f"Model artifact checksum mismatch for {path}")
    return joblib.load(path)


def active_model(conn) -> tuple[RecModel, dict | None]:
    row = db.fetch_one(conn, """SELECT model_version, artifact_path, artifact_sha256, metrics FROM model_registry
                                WHERE status = 'ACTIVE'""")
    if row is None:
        return baseline_model(), None
    try:
        return load_model(row["artifact_path"], row["artifact_sha256"]), row
    except Exception as exc:  # artifact missing/corrupt -> fall back, never serve nothing
        log.error("Active model %s could not be loaded (%s); falling back to rules baseline",
                  row["model_version"], exc)
        return baseline_model(), None


def register_model(conn, model: RecModel, run_id: int, path: Path, sha: str, cutoff, status: str) -> None:
    db.execute(conn, """INSERT INTO model_registry (model_version, run_id, algorithm, status, artifact_path,
                                                    artifact_sha256, data_cutoff, metrics)
                        VALUES (:v, :r, :a, :s, :p, :h, :c, :m)""",
               {"v": model.version, "r": run_id, "a": model.algorithm[:100], "s": status, "p": str(path),
                "h": sha, "c": cutoff, "m": json.dumps(model.metrics, default=str)})


def promote(conn, version: str) -> None:
    db.execute(conn, "UPDATE model_registry SET status = 'RETIRED', retired_at = SYS_EXTRACT_UTC(SYSTIMESTAMP) WHERE status = 'ACTIVE'")
    db.execute(conn, "UPDATE model_registry SET status = 'ACTIVE', promoted_at = SYS_EXTRACT_UTC(SYSTIMESTAMP) WHERE model_version = :v",
               {"v": version})
    set_state(conn, "ACTIVE_MODEL_VERSION", version)


# ---- batches -----------------------------------------------------------------------------------
def write_batch(conn: oracledb.Connection, run_id: int | None, model_version: str, generated_at: datetime,
                windows, recs: pd.DataFrame, statuses: pd.DataFrame, chunk: int = 20_000) -> int:
    batch_id = db.insert_returning_id(conn, """
        INSERT INTO recommendation_batches (run_id, model_version, status, generated_at, data_as_of,
                                            aggregation_start, aggregation_end)
        VALUES (:r, :m, 'BUILDING', :g, :d, :s, :e)""",
        {"r": run_id, "m": model_version, "g": generated_at, "d": windows.data_cutoff,
         "s": windows.aggregation_start, "e": windows.aggregation_end}, "batch_id")
    conn.commit()
    try:
        cols = ["customer_id", "candidate_rank", "offer_id", "card_id", "merchant_id", "recommendation_score",
                "relevance_score", "recommendation_reason", "reason_codes", "estimated_customer_saving",
                "saving_assumption", "merchant_txn_amount_3m", "merchant_txn_count_3m",
                "last_txn_date_with_merchant", "avg_txn_amount_with_merchant"]
        sql = (f"INSERT INTO customer_recommendations (batch_id, {', '.join(cols)}) "
               f"VALUES (:1, {', '.join(':' + str(i + 2) for i in range(len(cols)))})")
        rows = [[batch_id, *[None if pd.isna(v) else v for v in r]] for r in recs[cols].itertuples(index=False)] \
            if len(recs) else []
        for i in range(0, len(rows), chunk):
            db.executemany(conn, sql, rows[i:i + chunk])
            conn.commit()
        srows = [[batch_id, r.customer_id, r.status, r.shortfall_reason, r.history_level]
                 for r in statuses.itertuples(index=False)]
        for i in range(0, len(srows), chunk):
            db.executemany(conn, """INSERT INTO customer_rec_status (batch_id, customer_id, status, shortfall_reason,
                                    history_level) VALUES (:1, :2, :3, :4, :5)""", srows[i:i + chunk])
            conn.commit()
        counts = statuses["status"].value_counts().to_dict() if len(statuses) else {}
        db.execute(conn, """UPDATE recommendation_batches SET customer_count = :c, recommendation_rows = :r,
                            customers_full = :f, customers_partial = :p, customers_none = :n WHERE batch_id = :b""",
                   {"c": len(statuses), "r": len(rows), "f": counts.get("FULL", 0), "p": counts.get("PARTIAL", 0),
                    "n": counts.get("NONE", 0), "b": batch_id})
        conn.commit()
    except Exception:
        conn.rollback()
        db.execute(conn, "UPDATE recommendation_batches SET status = 'FAILED' WHERE batch_id = :b", {"b": batch_id})
        conn.commit()
        raise
    return batch_id


def publish_batch(conn: oracledb.Connection, batch_id: int) -> None:
    """Single transaction: old batch superseded, new batch published, pointer moved."""
    db.execute(conn, "UPDATE recommendation_batches SET status = 'SUPERSEDED' WHERE status = 'PUBLISHED'")
    n = db.execute(conn, """UPDATE recommendation_batches SET status = 'PUBLISHED', published_at = SYS_EXTRACT_UTC(SYSTIMESTAMP)
                            WHERE batch_id = :b AND status = 'BUILDING'""", {"b": batch_id})
    if n != 1:
        conn.rollback()
        raise RuntimeError(f"Batch {batch_id} is not in BUILDING state")
    set_state(conn, "CURRENT_BATCH_ID", str(batch_id))
    conn.commit()
    _purge_old_batches(conn)


def _purge_old_batches(conn) -> None:
    old = db.fetch_all(conn, """SELECT batch_id FROM recommendation_batches
                                WHERE status IN ('SUPERSEDED', 'FAILED') ORDER BY batch_id DESC""")
    for r in old[KEEP_BATCHES - 1:]:
        db.execute(conn, "DELETE FROM customer_recommendations WHERE batch_id = :b", {"b": r["batch_id"]})
        db.execute(conn, "DELETE FROM customer_rec_status WHERE batch_id = :b", {"b": r["batch_id"]})
        conn.commit()


def current_batch(conn) -> dict | None:
    bid = get_state(conn, "CURRENT_BATCH_ID")
    if bid is None:
        return None
    return db.fetch_one(conn, "SELECT * FROM recommendation_batches WHERE batch_id = :b", {"b": int(bid)})
