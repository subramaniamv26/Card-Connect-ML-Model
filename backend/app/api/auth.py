from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from .. import db
from ..audit import audit
from ..security import Principal, create_token, current_user, hash_password, require, verify_password
from .common import clean, get_conn

router = APIRouter(tags=["auth"])
_FAILED: dict[str, list[float]] = {}
MAX_FAILURES, WINDOW = 5, 300


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=1, max_length=200)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    username: str
    role: str


@router.post("/auth/login", response_model=TokenOut, summary="Exchange username/password for a bearer token")
def login(body: LoginIn, request: Request, conn=Depends(get_conn)) -> TokenOut:
    key = body.username.lower()
    recent = [t for t in _FAILED.get(key, []) if time.time() - t < WINDOW]
    if len(recent) >= MAX_FAILURES:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed attempts; try again later")
    row = db.fetch_one(conn, "SELECT username, password_hash, role, is_active FROM app_users WHERE LOWER(username) = :u",
                       {"u": key})
    if row is None or row["is_active"] != "Y" or not verify_password(body.password, row["password_hash"]):
        _FAILED[key] = recent + [time.time()]
        audit(body.username, None, "LOGIN", "user", body.username, outcome="DENIED", request=request)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid username or password")
    _FAILED.pop(key, None)
    db.execute(conn, "UPDATE app_users SET last_login_at = SYS_EXTRACT_UTC(SYSTIMESTAMP) WHERE username = :u", {"u": row["username"]})
    conn.commit()
    token, ttl = create_token(row["username"], row["role"])
    audit(row["username"], row["role"], "LOGIN", "user", row["username"], request=request, conn=conn)
    conn.commit()
    return TokenOut(access_token=token, expires_in=ttl, username=row["username"], role=row["role"])


@router.get("/auth/me", summary="Current user and permissions")
def me(user: Principal = Depends(current_user)) -> dict:
    from ..security import PERMISSIONS
    return {"username": user.username, "role": user.role,
            "permissions": sorted(p for p in PERMISSIONS if user.can(p))}


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=60, pattern=r"^[A-Za-z0-9_.\-]+$")
    password: str = Field(min_length=10, max_length=200)
    role: str = Field(pattern="^(ADMIN|ANALYST|READ_ONLY)$")


class UserPatch(BaseModel):
    role: str | None = Field(default=None, pattern="^(ADMIN|ANALYST|READ_ONLY)$")
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=10, max_length=200)


@router.get("/users", summary="List users (admin)")
def list_users(user: Principal = Depends(require("users:manage")), conn=Depends(get_conn)) -> list[dict]:
    return [clean(r) for r in db.fetch_all(conn, """SELECT username, role, is_active, created_at, last_login_at
                                                    FROM app_users ORDER BY username""")]


@router.post("/users", status_code=201, summary="Create a user (admin)")
def create_user(body: UserIn, request: Request, user: Principal = Depends(require("users:manage")),
                conn=Depends(get_conn)) -> dict:
    if db.scalar(conn, "SELECT COUNT(*) FROM app_users WHERE LOWER(username) = :u", {"u": body.username.lower()}):
        raise HTTPException(409, "Username already exists")
    db.execute(conn, "INSERT INTO app_users (username, password_hash, role) VALUES (:u, :h, :r)",
               {"u": body.username, "h": hash_password(body.password), "r": body.role})
    audit(user.username, user.role, "USER_CREATE", "user", body.username, {"role": body.role}, request=request, conn=conn)
    conn.commit()
    return {"username": body.username, "role": body.role}


@router.patch("/users/{username}", summary="Change role, activation or password (admin)")
def patch_user(username: str, body: UserPatch, request: Request, user: Principal = Depends(require("users:manage")),
               conn=Depends(get_conn)) -> dict:
    if username == user.username and (body.role not in (None, "ADMIN") or body.is_active is False):
        raise HTTPException(400, "You cannot demote or deactivate yourself")
    sets, binds = [], {"u": username}
    if body.role:
        sets.append("role = :r"); binds["r"] = body.role
    if body.is_active is not None:
        sets.append("is_active = :a"); binds["a"] = "Y" if body.is_active else "N"
    if body.password:
        sets.append("password_hash = :h"); binds["h"] = hash_password(body.password)
    if not sets:
        raise HTTPException(400, "Nothing to update")
    if db.execute(conn, f"UPDATE app_users SET {', '.join(sets)} WHERE username = :u", binds) != 1:
        raise HTTPException(404, "User not found")
    audit(user.username, user.role, "USER_UPDATE", "user", username,
          {"role": body.role, "is_active": body.is_active, "password_changed": bool(body.password)},
          request=request, conn=conn)
    conn.commit()
    return {"username": username, "updated": [s.split(" ")[0] for s in sets]}

