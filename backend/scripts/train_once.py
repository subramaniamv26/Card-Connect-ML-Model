"""Run one training + recommendation run directly in this terminal (no worker process required).

    python -m scripts.train_once

It records the run in MODEL_TRAINING_RUNS exactly like the worker does, so it shows on the Training page.
"""
from __future__ import annotations

import json
import sys

from app import db
from app.logging_conf import configure_logging
from app.ml.pipeline import run_training


def main() -> int:
    configure_logging()
    with db.connection() as conn:
        busy = db.scalar(conn, "SELECT COUNT(*) FROM model_training_runs WHERE status IN ('QUEUED', 'RUNNING')")
        if busy:
            print("Another training run is queued or running; wait for it to finish.")
            return 1
        run_id = db.insert_returning_id(conn, """INSERT INTO model_training_runs (trigger_type, requested_by)
                                                 VALUES ('MANUAL', 'cli')""", {}, "run_id")
        conn.commit()
    try:
        result = run_training(run_id)
    except Exception as exc:
        with db.connection() as conn:
            db.execute(conn, """UPDATE model_training_runs SET status = 'FAILED', error_message = :m,
                                finished_at = SYS_EXTRACT_UTC(SYSTIMESTAMP) WHERE run_id = :r""",
                       {"m": f"{type(exc).__name__}: {exc}"[:3900], "r": run_id})
            conn.commit()
        raise
    holdout = result["metrics"]["holdout"]["methods"]
    print(f"\nRun {run_id}: {result['status']} - {result['decision']}")
    print("NDCG@3:", json.dumps({k: v["ndcg_at_3"] for k, v in holdout.items()}))
    print("Published batch:", result["batch_id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
