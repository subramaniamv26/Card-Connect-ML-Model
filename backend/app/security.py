"""Authentication (JWT bearer tokens, bcrypt password hashes) and role-based access control.

Roles:
  READ_ONLY - dashboards, recommendations and profiles with personal data masked.
  ANALYST   - + bulk export jobs (masked), engagement event recording, customer names unmasked.
  ADMIN     - + imports, offer/merchant/rule management, offer approval, training, users,
              contact/device details and PII exports, consent-aware delivery.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import get_settings

ROLES = ("READ_ONLY", "ANALYST", "ADMIN")
_RANK = {r: i for i, r in enumerate(ROLES)}

PERMISSIONS: dict[str, str] = {
    "read": "READ_ONLY",
    "customer_names": "ANALYST",
    "events:write": "ANALYST",
    "exports:create": "ANALYST",
    "pii:read": "ADMIN",
    "exports:pii": "ADMIN",
    "imports:write": "ADMIN",
    "catalog:write": "ADMIN",
    "offers:approve": "ADMIN",
    "training:run": "ADMIN",
    "users:manage": "ADMIN",
    "delivery:request": "ADMIN",
}

_bearer = HTTPBearer(auto_error=False)
ALGORITHM = "HS256"


@dataclass(frozen=True)
class Principal:
    username: str
    role: str

    def can(self, permission: str) -> bool:
        return _RANK[self.role] >= _RANK[PERMISSIONS[permission]]


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("Password must be at least 10 characters")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))
    except ValueError:
        return False


def create_token(username: str, role: str) -> tuple[str, int]:
    s = get_settings()
    ttl = s.jwt_ttl_minutes * 60
    now = datetime.now(timezone.utc)
    token = jwt.encode({"sub": username, "role": role, "iat": now, "exp": now + timedelta(seconds=ttl),
                        "iss": "card-connect-offers"}, s.jwt_secret.get_secret_value(), algorithm=ALGORITHM)
    return token, ttl


def decode_token(token: str) -> Principal:
    try:
        claims = jwt.decode(token, get_settings().jwt_secret.get_secret_value(), algorithms=[ALGORITHM],
                            issuer="card-connect-offers", options={"require": ["exp", "sub", "role"]})
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired token",
                            headers={"WWW-Authenticate": "Bearer"}) from None
    if claims.get("role") not in ROLES:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token role")
    return Principal(claims["sub"], claims["role"])


def current_user(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Principal:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    p = decode_token(creds.credentials)
    request.state.principal = p
    return p


def require(permission: str) -> Callable[..., Principal]:
    def dep(request: Request, user: Principal = Depends(current_user)) -> Principal:
        if not user.can(permission):
            from .audit import audit_denied
            audit_denied(user, permission, request)
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires permission '{permission}'")
        return user
    return dep
