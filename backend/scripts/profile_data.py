"""Stage 1 data inspection: profile the supplied CSVs and write a data-quality report.

Usage:
    python -m scripts.profile_data --data-dir <dir with CSVs> --dictionary <data_dictionary.csv> --out ../docs

Reads every file as strings (preserving leading zeros), checks headers against the data
dictionary, primary-key uniqueness, referential integrity, domains, and the consistency of
denormalised customer/merchant attributes copied into each transaction.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import pandas as pd

MASTER_FILES = {
    "customers.csv": "customer_id",
    "cards.csv": "card_id",
    "merchants.csv": "merchant_id",
    "customer_offer_candidates.csv": "offer_id",
    "customer_offer_utilization.csv": "offer_id",
}
TXN_FILE = "credit_card_transactions_3m.csv"


def read(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])


def header_check(df_cols: list[str], dictionary: pd.DataFrame, csv_file: str) -> dict:
    expected = dictionary.loc[dictionary.csv_file == csv_file, "column_name"].tolist()
    return {
        "in_dictionary_not_in_file": [c for c in expected if c not in df_cols],
        "in_file_not_in_dictionary": [c for c in df_cols if c not in expected],
    }


def null_counts(df: pd.DataFrame) -> dict:
    n = df.isna().sum()
    return {k: int(v) for k, v in n.items() if v}


def profile(data_dir: Path, dictionary_path: Path) -> dict:
    dictionary = pd.read_csv(dictionary_path, dtype=str, keep_default_na=False)
    report: dict = {"data_dir": str(data_dir), "files": {}, "issues": []}

    frames: dict[str, pd.DataFrame] = {}
    for name, pk in MASTER_FILES.items():
        df = read(data_dir / name)
        frames[name] = df
        report["files"][name] = {
            "rows": len(df),
            "columns": list(df.columns),
            "header_vs_dictionary": header_check(list(df.columns), dictionary, name),
            "nulls": null_counts(df),
            "duplicate_primary_keys": int(df[pk].duplicated().sum()),
            "exact_duplicate_rows": int(df.duplicated().sum()),
        }

    cust, cards, merch = frames["customers.csv"], frames["cards.csv"], frames["merchants.csv"]
    cand, util = frames["customer_offer_candidates.csv"], frames["customer_offer_utilization.csv"]
    cust_ids, card_ids, merch_ids = set(cust.customer_id), set(cards.card_id), set(merch.merchant_id)

    # Customers
    r = report["files"]["customers.csv"]
    r["duplicate_nic"] = int(cust.nic_no.duplicated().sum())
    r["duplicate_device"] = int(cust.mobile_device_id.duplicated().sum())
    r["segments"] = cust.customer_segment.value_counts().to_dict()
    r["districts"] = int(cust.customer_home_district.nunique())
    r["provinces"] = int(cust.customer_home_province.nunique())
    r["has_customer_contact_no"] = "customer_contact_no" in cust.columns

    # Cards
    r = report["files"]["cards.csv"]
    r["orphan_customer"] = int((~cards.customer_id.isin(cust_ids)).sum())
    r["network_type"] = (cards.card_network + "/" + cards.card_type).value_counts().to_dict()
    r["status"] = cards.card_status.value_counts().to_dict()
    per_cust = cards.groupby("customer_id").size()
    declared = cust.set_index("customer_id").number_of_cards.astype(int)
    r["number_of_cards_mismatch"] = int((per_cust.reindex(declared.index, fill_value=0) != declared).sum())
    r["customers_without_active_card"] = int(
        len(cust_ids - set(cards.loc[cards.card_status == "ACTIVE", "customer_id"]))
    )
    nic_map = cust.set_index("customer_id").nic_no
    r["nic_mismatch_with_customer"] = int((cards.nic_no.values != nic_map.reindex(cards.customer_id).values).sum())

    # Merchants
    r = report["files"]["merchants.csv"]
    r["categories"] = merch.merchant_category.value_counts().to_dict()
    r["types"] = merch.merchant_type.value_counts().to_dict()
    r["offer_supported"] = merch.merchant_offer_supported.value_counts().to_dict()
    r["brands"] = int(merch.merchant_brand.nunique())
    r["international_outlets"] = int((merch.merchant_city == "INTERNATIONAL").sum())
    r["location_value_ONLINE"] = int((merch.merchant_location == "ONLINE").sum())

    # Offer candidates
    r = report["files"]["customer_offer_candidates.csv"]
    r["status"] = cand.offer_status.value_counts().to_dict()
    r["offer_type"] = cand.offer_type.value_counts().to_dict()
    r["utilized"] = cand.offer_utilized.value_counts().to_dict()
    r["orphan_customer"] = int((~cand.customer_id.isin(cust_ids)).sum())
    r["brands_not_in_merchant_master"] = sorted(set(cand.recommended_merchant_brand) - set(merch.merchant_brand))
    r["has_discount_or_value_fields"] = any(
        c in cand.columns for c in ("offer_value", "discount", "cashback", "minimum_spend", "maximum_benefit")
    )

    # Utilisation
    r = report["files"]["customer_offer_utilization.csv"]
    r["offer_start_min"], r["offer_start_max"] = util.offer_start_date.min(), util.offer_start_date.max()
    r["offer_end_min"], r["offer_end_max"] = util.offer_end_date.min(), util.offer_end_date.max()
    r["received"] = util.offer_received.value_counts().to_dict()
    r["utilized"] = util.offer_utilized.value_counts().to_dict()
    r["redeemed_eq_utilized"] = bool((util.offer_redeemed == util.offer_utilized).all())
    r["offers_not_in_candidates"] = int((~util.offer_id.isin(set(cand.offer_id))).sum())
    r["candidates_not_in_utilization"] = int((~cand.offer_id.isin(set(util.offer_id))).sum())
    r["orphan_merchant"] = int((~util.merchant_id.isin(merch_ids)).sum())
    waves = util.groupby(["offer_start_date", "offer_end_date"]).agg(
        offers=("offer_id", "size"), utilized=("offer_utilized", lambda s: int((s == "YES").sum()))
    )
    r["waves"] = [
        {"start": a, "end": b, "offers": int(v.offers), "utilized": int(v.utilized)} for (a, b), v in waves.iterrows()
    ]
    r["utilization_rate_by_type"] = (
        util.assign(u=(util.offer_utilized == "YES").astype(int)).groupby("offer_type").u.mean().round(4).to_dict()
    )

    # Transactions (chunked)
    report["files"][TXN_FILE] = profile_transactions(data_dir / TXN_FILE, dictionary, cust, cards, merch, util)
    report["issues"] = derive_issues(report)
    return report


def profile_transactions(path, dictionary, cust, cards, merch, util) -> dict:
    cust_attr = cust.set_index("customer_id")[["customer_name", "nic_no", "mobile_device_id", "customer_home_district"]]
    card_attr = cards.set_index("card_id")[["customer_id", "card_network", "card_type", "card_tier", "card_status"]]
    merch_attr = merch.set_index("merchant_id")[["merchant_brand", "merchant_category", "merchant_city"]]

    out = {
        "rows": 0, "duplicate_transaction_id": 0, "duplicate_card_timestamp": 0,
        "nulls": Counter(), "status": Counter(), "type": Counter(), "channel": Counter(), "currency": Counter(),
        "currency_international": Counter(), "month": Counter(), "orphan_customer": 0, "orphan_card": 0,
        "orphan_merchant": 0, "card_customer_mismatch": 0, "customer_attr_mismatch": 0,
        "card_attr_mismatch": 0, "merchant_attr_mismatch": 0, "non_active_card_txn": 0,
        "amount_non_positive": 0, "amount_unparseable": 0, "datetime_unparseable": 0,
        "hour_mismatch": 0, "offer_id_present": 0, "offer_utilized_yes": 0, "offer_flag_inconsistent": 0,
        "amount_lkr_min": None, "amount_lkr_max": None, "amount_lkr_sum_approved": 0.0,
        "approved_by_currency_sum": Counter(),
    }
    seen_ids: set[str] = set()
    seen_card_ts: set[tuple] = set()
    dt_min, dt_max = None, None
    header = None
    for chunk in pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""], chunksize=500_000):
        if header is None:
            header = list(chunk.columns)
        out["rows"] += len(chunk)
        for k, v in chunk.isna().sum().items():
            if v:
                out["nulls"][k] += int(v)
        ids = chunk.transaction_id.tolist()
        before = len(seen_ids)
        seen_ids.update(ids)
        out["duplicate_transaction_id"] += len(ids) - (len(seen_ids) - before)
        keys = list(zip(chunk.card_id, chunk.transaction_datetime))
        before = len(seen_card_ts)
        seen_card_ts.update(keys)
        out["duplicate_card_timestamp"] += len(keys) - (len(seen_card_ts) - before)

        for col, key in (("transaction_status", "status"), ("transaction_type", "type"),
                         ("transaction_channel", "channel"), ("transaction_currency", "currency"), ("month", "month")):
            out[key].update(chunk[col].value_counts().to_dict())
        out["currency_international"].update(
            (chunk.transaction_currency + "/intl=" + chunk.is_international).value_counts().to_dict()
        )

        dt = pd.to_datetime(chunk.transaction_datetime, format="%Y-%m-%d %H:%M:%S", errors="coerce")
        out["datetime_unparseable"] += int(dt.isna().sum())
        out["hour_mismatch"] += int((dt.dt.hour.astype("Int64").astype(str) != chunk.transaction_hour).sum())
        cmin, cmax = dt.min(), dt.max()
        dt_min = cmin if dt_min is None or cmin < dt_min else dt_min
        dt_max = cmax if dt_max is None or cmax > dt_max else dt_max

        amt = pd.to_numeric(chunk.transaction_amount, errors="coerce")
        out["amount_unparseable"] += int(amt.isna().sum())
        out["amount_non_positive"] += int((amt <= 0).sum())
        lkr = amt.where(chunk.transaction_currency == "LKR", amt * 300)
        lo, hi = float(lkr.min()), float(lkr.max())
        out["amount_lkr_min"] = lo if out["amount_lkr_min"] is None else min(out["amount_lkr_min"], lo)
        out["amount_lkr_max"] = hi if out["amount_lkr_max"] is None else max(out["amount_lkr_max"], hi)
        approved = chunk.transaction_status == "APPROVED"
        out["amount_lkr_sum_approved"] += float(lkr[approved].sum())
        for cur, s in amt[approved].groupby(chunk.transaction_currency[approved]).sum().items():
            out["approved_by_currency_sum"][cur] += float(s)

        out["orphan_customer"] += int((~chunk.customer_id.isin(cust_attr.index)).sum())
        out["orphan_card"] += int((~chunk.card_id.isin(card_attr.index)).sum())
        out["orphan_merchant"] += int((~chunk.merchant_id.isin(merch_attr.index)).sum())

        ca = card_attr.reindex(chunk.card_id)
        out["card_customer_mismatch"] += int((ca.customer_id.values != chunk.customer_id.values).sum())
        out["card_attr_mismatch"] += int(
            ((ca.card_network.values != chunk.card_network.values) | (ca.card_type.values != chunk.card_type.values)
             | (ca.card_tier.values != chunk.card_tier.values)).sum()
        )
        out["non_active_card_txn"] += int((ca.card_status.values != "ACTIVE").sum())
        cu = cust_attr.reindex(chunk.customer_id)
        out["customer_attr_mismatch"] += int(
            ((cu.customer_name.values != chunk.customer_name.values) | (cu.nic_no.values != chunk.nic_no.values)
             | (cu.mobile_device_id.values != chunk.mobile_device_id.values)).sum()
        )
        ma = merch_attr.reindex(chunk.merchant_id)
        out["merchant_attr_mismatch"] += int(
            ((ma.merchant_brand.values != chunk.merchant_brand.values)
             | (ma.merchant_category.values != chunk.merchant_category.values)
             | (ma.merchant_city.values != chunk.merchant_city.values)).sum()
        )
        has_offer = chunk.offer_id.notna()
        out["offer_id_present"] += int(has_offer.sum())
        out["offer_utilized_yes"] += int((chunk.offer_utilized == "YES").sum())
        out["offer_flag_inconsistent"] += int((has_offer != (chunk.offer_utilized == "YES")).sum())

    for k in ("nulls", "status", "type", "channel", "currency", "currency_international", "month",
              "approved_by_currency_sum"):
        out[k] = dict(sorted(out[k].items()))
    out["datetime_min"], out["datetime_max"] = str(dt_min), str(dt_max)
    out["span_days"] = (dt_max - dt_min).days if dt_min is not None else None
    out["header_vs_dictionary"] = header_check(header or [], dictionary, TXN_FILE)
    out["util_txn_ids_missing_in_txn"] = int(
        (~util.utilization_transaction_id.dropna().isin(seen_ids)).sum()
    )
    return out


def derive_issues(rep: dict) -> list[dict]:
    issues = []
    f = rep["files"]

    def add(sev, area, text):
        issues.append({"severity": sev, "area": area, "finding": text})

    for name, r in f.items():
        hv = r.get("header_vs_dictionary", {})
        if hv.get("in_dictionary_not_in_file"):
            add("HIGH", name, f"Columns in data dictionary but missing from file: {hv['in_dictionary_not_in_file']}")
        if hv.get("in_file_not_in_dictionary"):
            add("LOW", name, f"Columns in file not described by dictionary: {hv['in_file_not_in_dictionary']}")
        for k in ("duplicate_primary_keys", "exact_duplicate_rows", "duplicate_transaction_id", "orphan_customer",
                  "orphan_card", "orphan_merchant", "card_customer_mismatch", "customer_attr_mismatch",
                  "card_attr_mismatch", "merchant_attr_mismatch", "amount_non_positive", "amount_unparseable",
                  "datetime_unparseable", "offer_flag_inconsistent", "nic_mismatch_with_customer"):
            if r.get(k):
                add("HIGH", name, f"{k} = {r[k]}")
    t = f[TXN_FILE]
    if t.get("span_days") is not None and t["span_days"] < 360:
        add("HIGH", TXN_FILE, f"History covers {t['span_days']} days ({t['datetime_min']} .. {t['datetime_max']}); "
            "a rolling 12-month window will contain only this period")
    if set(t["status"]) <= {"APPROVED", "DECLINED"}:
        add("MEDIUM", TXN_FILE, "No REFUND / REVERSAL / CANCELLED records exist; only APPROVED and DECLINED")
    if not f["customer_offer_candidates.csv"]["has_discount_or_value_fields"]:
        add("HIGH", "offers", "No approved offer catalog: candidate offers carry no value, minimum spend, maximum "
            "benefit, validity, card eligibility or T&Cs")
    if not f["customers.csv"]["has_customer_contact_no"]:
        add("MEDIUM", "customers.csv", "customer_contact_no is required by the API but absent from the CSV")
    if t.get("non_active_card_txn"):
        add("INFO", TXN_FILE, f"{t['non_active_card_txn']} transactions on cards whose CURRENT status is not ACTIVE "
            "(status is a snapshot, not history)")
    if t.get("duplicate_card_timestamp"):
        add("LOW", TXN_FILE, f"duplicate (card_id, timestamp) pairs = {t['duplicate_card_timestamp']}")
    return issues


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", required=True, type=Path)
    p.add_argument("--dictionary", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    a = p.parse_args()
    rep = profile(a.data_dir, a.dictionary)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "data_profile.json").write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    for i in rep["issues"]:
        print(f"[{i['severity']}] {i['area']}: {i['finding']}")


if __name__ == "__main__":
    main()
