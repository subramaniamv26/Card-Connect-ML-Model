"""Integration tests against a real Oracle schema (migrated, with data loaded and one published batch).

Run:  ORACLE_TEST_DSN=... ORACLE_TEST_USER=... ORACLE_TEST_PASSWORD=... pytest tests/integration
Only rows created by these tests (ids prefixed TSTI_) are modified, and they are removed afterwards.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("ORACLE_TEST_DSN"), reason="ORACLE_TEST_DSN not set")


@pytest.fixture(scope="module")
def conn():
    from app import db
    with db.connection() as c:
        yield c
    db.close_pool()


def _cleanup(conn):
    from app import db
    for sql in ("DELETE FROM offer_events WHERE idempotency_key LIKE 'TSTI_%'",
                "DELETE FROM customer_devices WHERE customer_id LIKE 'TSTI_%'",
                "DELETE FROM customers WHERE customer_id LIKE 'TSTI_%'",
                "DELETE FROM import_batches WHERE file_name LIKE 'tsti_%'",
                "DELETE FROM app_users WHERE username LIKE 'tsti_%'"):
        db.execute(conn, sql)
    conn.commit()


HEADER = ("customer_id,customer_name,nic_no,mobile_device_id,device_type,device_os,customer_segment,customer_home_city,"
          "customer_home_district,customer_home_province,customer_tenure_months,number_of_cards\n")
ROWS = [
    "TSTI_000001,Test One,111111111V,TSTI_DEV_1,MOBILE_PHONE,ANDROID,SALARIED,Colombo,Colombo,Western,12,1",
    "TSTI_000002,Test Two,222222222222,TSTI_DEV_2,TABLET,IOS,STUDENT,Kandy,Kandy,Central,3,1",
    "TSTI_000003,Test Three,BADNIC,TSTI_DEV_3,MOBILE_PHONE,ANDROID,SALARIED,Galle,Galle,Southern,5,1",
    "TSTI_000001,Test One Again,111111111V,TSTI_DEV_1,MOBILE_PHONE,ANDROID,SALARIED,Colombo,Colombo,Western,12,1",
]


def _write(tmp_path: Path, name: str, rows: list[str]) -> Path:
    p = tmp_path / name
    p.write_text(HEADER + "\n".join(rows) + "\n", encoding="utf-8")
    return p


def test_import_is_validated_and_idempotent(conn, tmp_path):
    from app import db
    from app.ingest.importer import create_import, run_import
    _cleanup(conn)
    try:
        f = _write(tmp_path, "tsti_customers.csv", ROWS)
        r1 = run_import(conn, create_import(conn, "customers", f.name, str(f), "pytest"), "customers", f, "pytest")
        assert (r1["status"], r1["inserted"], r1["rejected"]) == ("COMPLETED_WITH_ERRORS", 2, 2)
        codes = {r["error_code"] for r in db.fetch_all(conn, "SELECT error_code, raw_record FROM import_rejections "
                                                             "WHERE import_id = :i", {"i": r1["import_id"]})}
        assert codes == {"BAD_FORMAT", "DUPLICATE_KEY"}
        raw = db.scalar(conn, "SELECT MAX(raw_record) FROM import_rejections WHERE import_id = :i", {"i": r1["import_id"]})
        assert "111111111V" not in raw and "Test One Again" not in raw      # PII masked in stored rejections

        r2 = run_import(conn, create_import(conn, "customers", f.name, str(f), "pytest"), "customers", f, "pytest")
        assert (r2["inserted"], r2["updated"], r2["unchanged"]) == (0, 0, 2)

        f3 = _write(tmp_path, "tsti_customers_v2.csv", [ROWS[0].replace("Colombo,Colombo", "Dehiwala,Colombo"), ROWS[1]])
        r3 = run_import(conn, create_import(conn, "customers", f3.name, str(f3), "pytest"), "customers", f3, "pytest")
        assert (r3["inserted"], r3["updated"], r3["unchanged"]) == (0, 1, 1)
        assert db.scalar(conn, "SELECT COUNT(*) FROM customers WHERE customer_id LIKE 'TSTI_%'") == 2
        assert db.scalar(conn, "SELECT customer_home_city FROM customers WHERE customer_id = 'TSTI_000001'") == "Dehiwala"

        # a new primary device keeps the old one as history, flagged non-primary
        f4 = _write(tmp_path, "tsti_customers_v3.csv", [ROWS[1].replace("TSTI_DEV_2", "TSTI_DEV_2B")])
        run_import(conn, create_import(conn, "customers", f4.name, str(f4), "pytest"), "customers", f4, "pytest")
        devs = dict((r["mobile_device_id"], r["is_primary"]) for r in db.fetch_all(
            conn, "SELECT mobile_device_id, is_primary FROM customer_devices WHERE customer_id = 'TSTI_000002'"))
        assert devs == {"TSTI_DEV_2": "N", "TSTI_DEV_2B": "Y"}
    finally:
        _cleanup(conn)


def test_missing_required_column_fails_cleanly(conn, tmp_path):
    from app.ingest.importer import create_import, run_import
    p = tmp_path / "tsti_bad.csv"
    p.write_text("customer_id,customer_name\nX,Y\n", encoding="utf-8")
    try:
        r = run_import(conn, create_import(conn, "customers", p.name, str(p), "pytest"), "customers", p, "pytest")
        assert r["status"] == "FAILED" and "Missing required columns" in r["summary"]["message"]
    finally:
        _cleanup(conn)


def test_lease_lock_prevents_overlap(conn):
    from app import locks
    owner = locks.acquire(conn, "BULK_JOBS", 60)
    try:
        assert owner is not None
        assert locks.acquire(conn, "BULK_JOBS", 60) is None
    finally:
        locks.release(conn, "BULK_JOBS", owner)
    second = locks.acquire(conn, "BULK_JOBS", 60)
    assert second is not None
    locks.release(conn, "BULK_JOBS", second)


def test_building_batch_is_invisible_to_readers(conn):
    from app import db
    from app.ml.store import current_batch
    before = current_batch(conn)
    if before is None:
        pytest.skip("no published batch")
    bid = db.insert_returning_id(conn, """INSERT INTO recommendation_batches (model_version, status, generated_at, data_as_of,
                                          aggregation_start, aggregation_end)
                                          VALUES ('tsti', 'BUILDING', SYSTIMESTAMP, SYSDATE, SYSDATE, SYSDATE)""", {}, "batch_id")
    conn.commit()
    try:
        assert current_batch(conn)["batch_id"] == before["batch_id"]
    finally:
        db.execute(conn, "DELETE FROM recommendation_batches WHERE batch_id = :b", {"b": bid})
        conn.commit()


@pytest.fixture(scope="module")
def api(conn):
    from fastapi.testclient import TestClient
    from app import db
    from app.main import app
    from app.security import hash_password
    pw = secrets.token_urlsafe(16)
    db.execute(conn, "INSERT INTO app_users (username, password_hash, role) VALUES ('tsti_admin', :h, 'ADMIN')",
               {"h": hash_password(pw)})
    conn.commit()
    client = TestClient(app)
    token = client.post("/api/v1/auth/login", json={"username": "tsti_admin", "password": pw}).json()["access_token"]
    client.headers["Authorization"] = f"Bearer {token}"
    yield client
    _cleanup(conn)


def test_suspended_offer_is_not_served_and_reserve_fills_slot(api, conn):
    from app import db
    from app.ml.store import current_batch
    batch = current_batch(conn)
    if batch is None:
        pytest.skip("no published batch")
    cid = db.scalar(conn, """SELECT customer_id FROM customer_recommendations WHERE batch_id = :b AND candidate_rank = 4
                             FETCH FIRST 1 ROWS ONLY""", {"b": batch["batch_id"]})
    before = api.get(f"/api/v1/customers/{cid}/recommendations").json()
    top = before["offers"][0]["offer_id"]
    assert api.post(f"/api/v1/offers/{top}/actions/suspend", json={"reason": "integration test"}).status_code == 200
    try:
        after = api.get(f"/api/v1/customers/{cid}/recommendations").json()
        ids = [o["offer_id"] for o in after["offers"]]
        assert top not in ids and len(ids) == 3
        assert [o["rank"] for o in after["offers"]] == [1, 2, 3]
        assert ids[:2] == [o["offer_id"] for o in before["offers"][1:3]]
        listing = api.get("/api/v1/recommendations", params={"offer_id": top, "page_size": 5}).json()
        assert all(top not in [o["offer_id"] for o in item["offers"]] for item in listing["items"])
    finally:
        assert api.post(f"/api/v1/offers/{top}/actions/resume").status_code == 200
    restored = api.get(f"/api/v1/customers/{cid}/recommendations").json()
    assert restored["offers"][0]["offer_id"] == top


def test_admin_sees_pii_and_response_fields(api, conn):
    from app import db
    cid = db.scalar(conn, "SELECT MIN(customer_id) FROM customer_rec_status WHERE status = 'FULL'")
    body = api.get(f"/api/v1/customers/{cid}/recommendations").json()
    assert body["pii_masked"] is False and body["customer"]["mobile_device_id"].startswith("DEVICE_")
    for key in ("customer_id", "customer_name", "mobile_device_id", "customer_home_city", "customer_home_district",
                "customer_home_province", "customer_contact_no"):
        assert key in body["customer"]
    o = body["offers"][0]
    for key in ("offer_id", "offer_title", "offer_description", "rank", "recommendation_score", "recommendation_reason",
                "offer_type", "offer_value", "minimum_spend", "maximum_benefit", "estimated_customer_saving", "currency",
                "valid_from", "valid_to", "terms_and_conditions"):
        assert key in o
    assert set(o["card"]) == {"card_id", "card_network", "card_type"}
    assert {"merchant_id", "merchant_brand", "merchant_location", "merchant_city", "merchant_district",
            "merchant_province", "merchant_email", "merchant_contact_no", "merchant_type"} <= set(o["merchant"])
    assert {"merchant_transaction_amount_last_3_months", "merchant_transaction_count_last_3_months",
            "last_transaction_date_with_merchant", "average_transaction_amount_with_merchant",
            "aggregation_period_start", "aggregation_period_end"} <= set(o["customer_merchant_spend"])
    for key in ("generated_at", "data_as_of", "model_version", "recommendation_status", "shortfall_reason"):
        assert key in body


def test_events_are_idempotent(api, conn):
    from app import db
    cid, oid = db.fetch_one(conn, """SELECT customer_id, offer_id FROM customer_recommendations
                                     WHERE candidate_rank = 1 FETCH FIRST 1 ROWS ONLY""").values()
    ev = {"event_type": "IMPRESSION", "customer_id": cid, "offer_id": oid, "occurred_at": "2026-10-26T09:00:00",
          "idempotency_key": "TSTI_" + secrets.token_hex(6)}
    r1 = api.post("/api/v1/events", json={"events": [ev, ev]}).json()
    assert (r1["accepted"], r1["duplicates_ignored"]) == (1, 1)
    bad = api.post("/api/v1/events", json={"events": [{**ev, "idempotency_key": "TSTI_" + secrets.token_hex(6),
                                                       "customer_id": "NOPE_1"}]}).json()
    assert bad["errors"][0]["error"] == "unknown customer_id"
