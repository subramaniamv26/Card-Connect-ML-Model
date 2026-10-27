"""Validated, repeatable CSV import into Oracle.

Flow per file: header check -> stream rows -> field parsing + cross-field/reference validation ->
within-file duplicate-key check -> valid rows staged in a session-private GTT in chunks -> counts of new /
changed / unchanged rows -> MERGE into the target -> post-merge steps -> commit per chunk.

Re-importing the same file is idempotent: every row is counted as unchanged and nothing is duplicated.
A chunk is committed only after its MERGE succeeds, so a failure leaves earlier chunks loaded and the
import marked FAILED; re-running the import resumes safely because MERGE is idempotent.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

import oracledb

from .. import db
from ..masking import mask_device, mask_name, mask_phone, mask_text
from .datasets import DATASETS, DatasetSpec, ReferenceData, RowError
from .fields import FieldError

log = logging.getLogger(__name__)

CHUNK_ROWS = 20_000
MAX_STORED_REJECTIONS = 20_000
PII_COLUMNS = {"customer_name": mask_name, "nic_no": lambda v: "[NIC]" if v else v, "mobile_device_id": mask_device,
               "customer_contact_no": mask_phone, "merchant_contact_no": mask_phone}


class ImportFailed(Exception):
    pass


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ------------------------------------------------------------------------------------------------
# Reference data
# ------------------------------------------------------------------------------------------------
def load_reference(conn: oracledb.Connection, needs: frozenset[str]) -> ReferenceData:
    ref = ReferenceData()
    if "customers" in needs:
        for r in db.fetch_all(conn, "SELECT customer_id, customer_name, nic_no FROM customers", arraysize=20000):
            ref.customers[r["customer_id"]] = (r["customer_name"], r["nic_no"])
            ref.nic_owner[r["nic_no"]] = r["customer_id"]
    if "devices" in needs:
        for r in db.fetch_all(conn, "SELECT mobile_device_id, customer_id FROM customer_devices", arraysize=20000):
            ref.device_owner[r["mobile_device_id"]] = r["customer_id"]
    if "cards" in needs:
        for r in db.fetch_all(conn, "SELECT card_id, customer_id, card_network, card_type, card_tier FROM cards",
                              arraysize=20000):
            ref.cards[r["card_id"]] = (r["customer_id"], r["card_network"], r["card_type"], r["card_tier"])
    if needs & {"merchants", "geo"}:
        for r in db.fetch_all(conn, """SELECT merchant_id, merchant_brand, merchant_category, merchant_city,
                                              merchant_offer_supported, merchant_district, merchant_province
                                       FROM merchants"""):
            ref.merchants[r["merchant_id"]] = (r["merchant_brand"], r["merchant_category"], r["merchant_city"],
                                               r["merchant_offer_supported"].strip())
            ref.brand_categories.setdefault(r["merchant_brand"], set()).add(r["merchant_category"])
            if r["merchant_city"] != "INTERNATIONAL":
                ref.districts.add(r["merchant_district"])
                ref.provinces.add(r["merchant_province"])
    if "geo" in needs:
        for r in db.fetch_all(conn, "SELECT DISTINCT customer_home_district d, customer_home_province p FROM customers"):
            ref.districts.add(r["d"])
            ref.provinces.add(r["p"])
    if needs & {"fx", "merchants", "geo"}:
        for r in db.fetch_all(conn, "SELECT currency, valid_from, valid_to, rate_to_lkr FROM fx_rates ORDER BY valid_from"):
            ref.fx.setdefault(r["currency"], []).append(
                (r["valid_from"].date(), r["valid_to"].date() if r["valid_to"] else None, Decimal(str(r["rate_to_lkr"]))))
    if "targeting" in needs:
        for r in db.fetch_all(conn, "SELECT offer_id, customer_id FROM historical_offer_targeting", arraysize=20000):
            ref.targeting_offers.add(r["offer_id"])
            ref.targeting_customer[r["offer_id"]] = r["customer_id"]
    if "catalog" in needs:
        ref.catalog_offers = {r["offer_id"] for r in db.fetch_all(conn, "SELECT offer_id FROM offers")}
    return ref


# ------------------------------------------------------------------------------------------------
# SQL builders (identifiers come only from the static DatasetSpec definitions)
# ------------------------------------------------------------------------------------------------
def _key_join(spec: DatasetSpec) -> str:
    return " AND ".join(f"t.{k} = s.{k}" for k in spec.key_columns)


def _update_cols(spec: DatasetSpec) -> list[str]:
    if spec.update_columns is not None:
        return spec.update_columns
    return [c for c in spec.stage_columns if c not in spec.key_columns]


def _changed_predicate(spec: DatasetSpec) -> str:
    if spec.change_mode == "hash":
        return "(t.row_hash IS NULL OR t.row_hash <> s.row_hash)"
    cols = [c for c in _update_cols(spec)]
    return "(" + " OR ".join(f"DECODE(t.{c}, s.{c}, 0, 1) = 1" for c in cols) + ")"


def merge_sql(spec: DatasetSpec) -> str:
    upd = ", ".join(f"t.{c} = s.{c}" for c in _update_cols(spec))
    has_updated_at = spec.target_table in ("customers", "cards", "merchants", "offers")
    upd_meta = ", t.import_id = :import_id" + (", t.updated_at = SYS_EXTRACT_UTC(SYSTIMESTAMP)" if has_updated_at else "")
    sql = (f"MERGE INTO {spec.target_table} t USING (SELECT * FROM {spec.stage_table}) s ON ({_key_join(spec)}) "
           f"WHEN MATCHED THEN UPDATE SET {upd}{upd_meta}{spec.update_extra} WHERE {_changed_predicate(spec)}")
    if spec.merge_insert:
        ins = spec.insert_columns or spec.stage_columns
        sql += (f" WHEN NOT MATCHED THEN INSERT ({', '.join(ins)}, import_id{spec.insert_extra_cols}) "
                f"VALUES ({', '.join('s.' + c for c in ins)}, :import_id{spec.insert_extra_vals})")
    return sql


def count_sql(spec: DatasetSpec) -> tuple[str, str]:
    new = (f"SELECT COUNT(*) FROM {spec.stage_table} s WHERE NOT EXISTS "
           f"(SELECT 1 FROM {spec.target_table} t WHERE {_key_join(spec)})")
    changed = (f"SELECT COUNT(*) FROM {spec.stage_table} s JOIN {spec.target_table} t ON ({_key_join(spec)}) "
               f"WHERE {_changed_predicate(spec)}")
    return new, changed


def _bind_subset(sql: str, binds: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in binds.items() if f":{k}" in sql}


# ------------------------------------------------------------------------------------------------
# Row handling
# ------------------------------------------------------------------------------------------------
def _masked_record(raw: dict[str, str]) -> str:
    safe = {k: (PII_COLUMNS[k](v) if k in PII_COLUMNS else v) for k, v in raw.items()}
    return mask_text(json.dumps(safe, ensure_ascii=False))[:3900]


def read_rows(path: Path) -> Iterator[tuple[int, dict[str, str]]]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for i, row in enumerate(reader, start=2):  # line 1 is the header
            if None in row:  # more values than headers
                row = {k: v for k, v in row.items() if k is not None}
                row["__extra__"] = "1"
            yield i, row


def read_header(path: Path) -> list[str]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        header = next(csv.reader(fh), None)
    if not header:
        raise ImportFailed("File is empty or has no header row")
    return [h.strip() for h in header]


def validate_row(spec: DatasetSpec, raw: dict[str, str], present: set[str], ref: ReferenceData):
    """Return (stage_tuple, None) or (None, [(code, message, column), ...])."""
    if raw.get("__extra__"):
        return None, [("EXTRA_VALUES", "row has more values than the header", None)]
    values: dict[str, Any] = {}
    errors: list[tuple[str, str, str | None]] = []
    for f in spec.fields:
        try:
            values[f.name] = f.parse(raw.get(f.name)) if f.name in present else f.parse(None)
        except FieldError as e:
            errors.append((e.code, e.message, f.name))
    if errors:
        return None, errors
    try:
        return spec.validator(values, raw, ref), None
    except RowError as e:
        return None, [(e.code, e.message, e.column)]


def _update_reference(spec: DatasetSpec, t: tuple, ref: ReferenceData) -> None:
    """Make rows accepted earlier in the same file visible to later rows' identity checks."""
    if spec.name == "customers":
        ref.nic_owner[t[2]] = t[0]
        ref.device_owner[t[10]] = t[0]
        ref.customers[t[0]] = (t[1], t[2])
    elif spec.name == "cards":
        ref.cards[t[0]] = (t[1], t[2], t[3], t[4])


# ------------------------------------------------------------------------------------------------
# Import driver
# ------------------------------------------------------------------------------------------------
def _set_status(conn, import_id: int, status: str, **cols) -> None:
    allowed = {"rows_total", "rows_valid", "rows_rejected", "rows_inserted", "rows_updated", "rows_unchanged",
               "error_summary", "file_sha256", "file_size_bytes"}
    sets = ["status = :status"] + [f"{k} = :{k}" for k in cols if k in allowed]
    if status in ("VALIDATING",):
        sets.append("started_at = NVL(started_at, SYS_EXTRACT_UTC(SYSTIMESTAMP))")
    if status in ("COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"):
        sets.append("finished_at = SYS_EXTRACT_UTC(SYSTIMESTAMP)")
    db.execute(conn, f"UPDATE import_batches SET {', '.join(sets)} WHERE import_id = :import_id",
               {"status": status, "import_id": import_id, **{k: v for k, v in cols.items() if k in allowed}})
    conn.commit()


def create_import(conn, dataset: str, file_name: str, file_path: str | None, username: str) -> int:
    if dataset not in DATASETS:
        raise ValueError(f"Unknown dataset {dataset}")
    import_id = db.insert_returning_id(
        conn, "INSERT INTO import_batches (dataset, file_name, file_path, uploaded_by) "
              "VALUES (:dataset, :file_name, :file_path, :username)",
        {"dataset": dataset, "file_name": file_name, "file_path": file_path, "username": username}, "import_id")
    conn.commit()
    return import_id


def run_import(conn: oracledb.Connection, import_id: int, dataset: str, path: Path, username: str,
               chunk_rows: int = CHUNK_ROWS) -> dict[str, Any]:
    spec = DATASETS[dataset]
    counts = Counter()
    error_codes: Counter[str] = Counter()
    summary: dict[str, Any] = {}
    try:
        _set_status(conn, import_id, "VALIDATING", file_sha256=file_sha256(path),
                    file_size_bytes=path.stat().st_size)
        header = read_header(path)
        present = set(header)
        missing = [c for c in spec.required_columns if c not in present]
        if missing:
            raise ImportFailed(f"Missing required columns: {', '.join(missing)}")
        dupes = [h for h, n in Counter(header).items() if n > 1]
        if dupes:
            raise ImportFailed(f"Duplicate column names in header: {', '.join(dupes)}")
        summary["ignored_columns"] = [c for c in header if c not in spec.field_names]
        summary["absent_optional_columns"] = [f.name for f in spec.fields if not f.required and f.name not in present]

        ref = load_reference(conn, spec.needs)
        seen_keys: set = set()
        key_idx = [spec.stage_columns.index(k) for k in spec.key_columns]
        chunk: list[tuple] = []
        rejections: list[list] = []
        stored_rejections = 0
        binds = {"import_id": import_id, "username": username}
        msql, (new_sql, changed_sql) = merge_sql(spec), count_sql(spec)
        insert_stage = (f"INSERT INTO {spec.stage_table} ({', '.join(spec.stage_columns)}) "
                        f"VALUES ({', '.join(':' + str(i + 1) for i in range(len(spec.stage_columns)))})")

        def flush_rejections() -> None:
            nonlocal rejections
            db.executemany(conn, "INSERT INTO import_rejections (import_id, row_number, column_name, error_code, "
                                 "error_message, raw_record) VALUES (:1, :2, :3, :4, :5, :6)", rejections)
            rejections = []

        def flush_chunk() -> None:
            nonlocal chunk
            if not chunk:
                return
            _set_status(conn, import_id, "LOADING", rows_total=counts["total"], rows_valid=counts["valid"],
                        rows_rejected=counts["rejected"], rows_inserted=counts["inserted"],
                        rows_updated=counts["updated"], rows_unchanged=counts["unchanged"])
            db.execute(conn, f"DELETE FROM {spec.stage_table}")
            db.executemany(conn, insert_stage, chunk)
            new = db.scalar(conn, new_sql) if spec.merge_insert else 0
            changed = db.scalar(conn, changed_sql)
            db.execute(conn, msql, _bind_subset(msql, binds))
            for step in spec.post_merge:
                db.execute(conn, step, _bind_subset(step, binds))
            db.execute(conn, f"DELETE FROM {spec.stage_table}")
            flush_rejections()
            conn.commit()
            counts["inserted"] += new
            counts["updated"] += changed
            counts["unchanged"] += len(chunk) - new - changed
            chunk = []

        for line_no, raw in read_rows(path):
            counts["total"] += 1
            stage_row, errs = validate_row(spec, raw, present, ref)
            if stage_row is not None:
                key = tuple(stage_row[i] for i in key_idx)
                if key in seen_keys:
                    stage_row, errs = None, [("DUPLICATE_KEY", f"duplicate {'/'.join(spec.key_columns)} in file "
                                                               "(first occurrence kept)", spec.key_columns[0])]
                else:
                    seen_keys.add(key)
            if stage_row is None:
                counts["rejected"] += 1
                for code, msg, col in errs:
                    error_codes[code] += 1
                if stored_rejections < MAX_STORED_REJECTIONS:
                    masked = _masked_record(raw)
                    for code, msg, col in errs:
                        rejections.append([import_id, line_no, col, code, mask_text(msg)[:500], masked])
                    stored_rejections += 1
                continue
            counts["valid"] += 1
            _update_reference(spec, stage_row, ref)
            chunk.append(stage_row)
            if len(chunk) >= chunk_rows:
                flush_chunk()
        flush_chunk()
        flush_rejections()
        conn.commit()

        summary["error_counts"] = dict(error_codes.most_common())
        summary["stored_rejections"] = stored_rejections
        if counts["total"] == 0:
            status = "FAILED"
            summary["message"] = "File contains a header but no data rows"
        elif counts["valid"] == 0:
            status = "FAILED"
            summary["message"] = "Every row was rejected"
        else:
            status = "COMPLETED_WITH_ERRORS" if counts["rejected"] else "COMPLETED"
        _set_status(conn, import_id, status, rows_total=counts["total"], rows_valid=counts["valid"],
                    rows_rejected=counts["rejected"], rows_inserted=counts["inserted"],
                    rows_updated=counts["updated"], rows_unchanged=counts["unchanged"],
                    error_summary=json.dumps(summary))
        log.info("Import %s (%s) %s: %s", import_id, dataset, status, dict(counts))
        return {"import_id": import_id, "status": status, **counts, "summary": summary}
    except Exception as exc:
        try:
            conn.rollback()
        except oracledb.Error:
            pass
        summary["message"] = mask_text(str(exc))[:2000]
        summary["error_counts"] = dict(error_codes)
        _set_status(conn, import_id, "FAILED", rows_total=counts["total"], rows_valid=counts["valid"],
                    rows_rejected=counts["rejected"], rows_inserted=counts["inserted"],
                    rows_updated=counts["updated"], rows_unchanged=counts["unchanged"],
                    error_summary=json.dumps(summary))
        log.error("Import %s (%s) failed: %s", import_id, dataset, summary["message"])
        if isinstance(exc, ImportFailed):
            return {"import_id": import_id, "status": "FAILED", **counts, "summary": summary}
        raise
