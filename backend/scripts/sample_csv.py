"""Write a consistent customer-sampled subset of the source CSVs (for schemas with a small storage quota).

Every selected customer keeps ALL of their cards, transactions and offer history, so per-customer features and
aggregates are unchanged; merchants are copied in full. Selection is deterministic (hash of customer_id).

    python -m scripts.sample_csv --src <csv dir> --dst <out dir> --customers 10000
"""
from __future__ import annotations

import argparse
import csv
import hashlib
from pathlib import Path

FILES_BY_CUSTOMER = ["customers.csv", "cards.csv", "credit_card_transactions_3m.csv", "customer_offer_candidates.csv",
                     "customer_offer_utilization.csv"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, type=Path)
    ap.add_argument("--dst", required=True, type=Path)
    ap.add_argument("--customers", required=True, type=int)
    a = ap.parse_args()
    a.dst.mkdir(parents=True, exist_ok=True)
    with open(a.src / "customers.csv", newline="", encoding="utf-8") as fh:
        ids = [r["customer_id"] for r in csv.DictReader(fh)]
    ranked = sorted(ids, key=lambda c: hashlib.sha256(c.encode()).hexdigest())
    keep = set(ranked[: a.customers])
    for name in FILES_BY_CUSTOMER:
        n_in = n_out = 0
        with open(a.src / name, newline="", encoding="utf-8") as fin, \
                open(a.dst / name, "w", newline="", encoding="utf-8") as fout:
            reader = csv.reader(fin)
            header = next(reader)
            idx = header.index("customer_id")
            writer = csv.writer(fout)
            writer.writerow(header)
            for row in reader:
                n_in += 1
                if row[idx] in keep:
                    writer.writerow(row)
                    n_out += 1
        print(f"{name}: {n_out:,} of {n_in:,} rows")
    (a.dst / "merchants.csv").write_bytes((a.src / "merchants.csv").read_bytes())
    print("merchants.csv: copied in full")


if __name__ == "__main__":
    main()
