"""Authentication and role-based authorisation for every protected route (no database needed).

The DB connection dependency is replaced by a stub that raises, so a request that passes authorisation
ends in 500 (handler reached) while a denied request must end in 401/403 before any DB access.
"""
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient

from app import audit as audit_mod
from app.api.common import get_conn
from app.config import get_settings
from app.main import app
from app.masking import mask_device, mask_name, mask_phone, mask_text
from app.security import create_token, decode_token


class _Unreachable(Exception):
    pass


class _FakeConn:
    """Any use of the connection proves the request got past authentication and authorisation."""
    def __getattr__(self, name):
        raise _Unreachable("handler reached the database")


def _no_db():
    yield _FakeConn()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    app.dependency_overrides[get_conn] = _no_db
    monkeypatch.setattr(audit_mod, "audit", lambda *a, **k: None)
    monkeypatch.setattr(audit_mod, "audit_denied", lambda *a, **k: None)
    yield
    app.dependency_overrides.clear()


client = TestClient(app, raise_server_exceptions=False)


def hdr(role):
    return {"Authorization": f"Bearer {create_token('u_' + role.lower(), role)[0]}"}


OFFER = {"offer_id": "X1", "offer_title": "t", "offer_description": "d", "merchant_brand": "B", "merchant_category": "GROCERY",
         "offer_type": "PERCENT_DISCOUNT", "offer_value": 10, "valid_from": "2026-10-01", "valid_to": "2027-01-01",
         "terms_and_conditions": "terms apply here"}
EVENT = {"events": [{"event_type": "CLICK", "customer_id": "C1", "offer_id": "O1", "occurred_at": "2026-10-26T10:00:00",
                     "idempotency_key": "abcdefgh1"}]}

# (method, path, json, minimum role)
ROUTES = [
    ("GET", "/api/v1/customers/C1/recommendations", None, "READ_ONLY"),
    ("GET", "/api/v1/recommendations", None, "READ_ONLY"),
    ("GET", "/api/v1/dashboard/overview", None, "READ_ONLY"),
    ("GET", "/api/v1/customers", None, "READ_ONLY"),
    ("GET", "/api/v1/customers/C1/profile", None, "READ_ONLY"),
    ("GET", "/api/v1/offers", None, "READ_ONLY"),
    ("GET", "/api/v1/merchants", None, "READ_ONLY"),
    ("GET", "/api/v1/imports", None, "READ_ONLY"),
    ("GET", "/api/v1/training/status", None, "READ_ONLY"),
    ("GET", "/api/v1/training/runs", None, "READ_ONLY"),
    ("GET", "/api/v1/reports/engagement", None, "READ_ONLY"),
    ("GET", "/api/v1/recommendation-jobs", None, "READ_ONLY"),
    ("POST", "/api/v1/events", EVENT, "ANALYST"),
    ("POST", "/api/v1/recommendation-jobs", {"job_type": "RECOMMENDATION_EXPORT"}, "ANALYST"),
    ("GET", "/api/v1/recommendation-jobs/1/download", None, "ANALYST"),
    ("POST", "/api/v1/offers", OFFER, "ADMIN"),
    ("PUT", "/api/v1/offers/X1", {**OFFER, "version": 1}, "ADMIN"),
    ("POST", "/api/v1/offers/X1/actions/approve", None, "ADMIN"),
    ("POST", "/api/v1/offers/X1/rules", {"rule_type": "NEW_TO_BRAND", "operator": "IS_TRUE"}, "ADMIN"),
    ("DELETE", "/api/v1/offers/X1/rules/1", None, "ADMIN"),
    ("PATCH", "/api/v1/merchants/M1", {"is_active": False}, "ADMIN"),
    ("POST", "/api/v1/training/runs", None, "ADMIN"),
    ("GET", "/api/v1/users", None, "ADMIN"),
    ("POST", "/api/v1/users", {"username": "newuser", "password": "x" * 12, "role": "ANALYST"}, "ADMIN"),
    ("PUT", "/api/v1/customers/C1/consents/SMS", {"consent_status": "GRANTED", "source": "app"}, "ADMIN"),
    ("POST", "/api/v1/deliveries", {"customer_id": "C1", "offer_id": "O1", "channel": "SMS"}, "ADMIN"),
    ("GET", "/api/v1/deliveries", None, "ADMIN"),
]
RANK = {"READ_ONLY": 0, "ANALYST": 1, "ADMIN": 2}


@pytest.mark.parametrize("method,path,body,_", ROUTES)
def test_unauthenticated_is_401(method, path, body, _):
    assert client.request(method, path, json=body).status_code == 401


@pytest.mark.parametrize("method,path,body,minimum", ROUTES)
@pytest.mark.parametrize("role", ["READ_ONLY", "ANALYST", "ADMIN"])
def test_role_matrix(method, path, body, minimum, role):
    status = client.request(method, path, json=body, headers=hdr(role)).status_code
    if RANK[role] < RANK[minimum]:
        assert status == 403, f"{role} should be denied {method} {path}"
    else:
        assert status not in (401, 403), f"{role} should be allowed {method} {path} (got {status})"


def test_import_upload_requires_admin():
    files = {"file": ("customers.csv", b"customer_id\nC1\n", "text/csv")}
    for role in ("READ_ONLY", "ANALYST"):
        assert client.post("/api/v1/imports", data={"dataset": "customers"}, files=files,
                           headers=hdr(role)).status_code == 403


def test_pii_export_requires_admin():
    r = client.post("/api/v1/recommendation-jobs", json={"include_pii": True}, headers=hdr("ANALYST"))
    assert r.status_code == 403


def test_tampered_and_expired_tokens_rejected():
    good = create_token("a", "ADMIN")[0]
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {good}x"}).status_code == 401
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    expired = jwt.encode({"sub": "a", "role": "ADMIN", "iat": past, "exp": past + timedelta(minutes=5),
                          "iss": "card-connect-offers"}, get_settings().jwt_secret.get_secret_value(), algorithm="HS256")
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
    forged_role = jwt.encode({"sub": "a", "role": "SUPERUSER", "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
                              "iss": "card-connect-offers"}, get_settings().jwt_secret.get_secret_value(), algorithm="HS256")
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {forged_role}"}).status_code == 401
    assert decode_token(good).role == "ADMIN"


def test_validation_errors_are_422_not_500():
    r = client.post("/api/v1/offers", json={**OFFER, "offer_type": "FREE_MONEY"}, headers=hdr("ADMIN"))
    assert r.status_code == 422


def test_health_is_public():
    assert client.get("/health").json() == {"status": "ok"}


def test_masking_helpers():
    assert mask_name("Dinesh Karunaratne") == "D***** K**********"
    assert mask_phone("+94-11-0121614") == "********614"
    assert mask_device("DEVICE_000123") == "DEV*******123"
    text = mask_text("nic 971654398V phone +94-11-0121614 mail a.b@x.example password=hunter2 DEVICE_000001")
    for secret in ("971654398V", "0121614", "a.b@x.example", "hunter2", "DEVICE_000001"):
        assert secret not in text


def test_log_records_are_masked(caplog):
    import logging
    from app.logging_conf import MaskingFilter
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "customer %s nic %s", ("C1", "200222206807"), None)
    MaskingFilter().filter(rec)
    assert "200222206807" not in rec.getMessage()
