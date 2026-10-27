"""Command-line import using the same validation/merge pipeline as the API.

    python -m scripts.import_csv --dataset customers --file /path/customers.csv [--user admin]

Datasets: customers, cards, merchants, transactions, offer_targeting, offer_outcomes, offers,
eligibility_rules. Load in that order (later datasets reference earlier ones).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app import db
from app.ingest.datasets import DATASETS
from app.ingest.importer import create_import, run_import
from app.logging_conf import configure_logging


def main() -> int:
    configure_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True, choices=sorted(DATASETS))
    ap.add_argument("--file", required=True, type=Path)
    ap.add_argument("--user", default="cli")
    a = ap.parse_args()
    if not a.file.is_file():
        print(f"File not found: {a.file}", file=sys.stderr)
        return 2
    with db.connection() as conn:
        import_id = create_import(conn, a.dataset, a.file.name, str(a.file.resolve()), a.user)
        result = run_import(conn, import_id, a.dataset, a.file, a.user)
    print(json.dumps({k: v for k, v in result.items()}, indent=2, default=str))
    return 0 if result["status"] in ("COMPLETED", "COMPLETED_WITH_ERRORS") else 1


if __name__ == "__main__":
    sys.exit(main())
