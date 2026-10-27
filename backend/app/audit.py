"""Audit logging of security-relevant actions into AUDIT_LOG (details are masked JSON)."""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import Request

from . import db
from .masking import mask_text

log = logging.getLogger(__name__)


def _ip(request: Request | None) -> str | None:
    if request is None or request.client is None:
        return None
    return request.client.host[:45]


def audit(username: str, role: str | None, action: str, entity_type: str | None = None,
          entity_id: str | None = None, details: dict[str, Any] | None = None, outcome: str = "SUCCESS",
          request: Request | None = None, conn=None) -> None:
    payload = mask_text(json.dumps(details, default=str))[:30000] if details else None
    sql = """INSERT INTO audit_log (username, role, action, entity_type, entity_id, outcome, client_ip, details)
             VALUES (:u, :r, :a, :t, :i, :o, :ip, :d)"""
    binds = {"u": username[:60], "r": role, "a": action[:60], "t": entity_type, "i": None if entity_id is None
             else str(entity_id)[:100], "o": outcome, "ip": _ip(request), "d": payload}
    try:
        if conn is not None:
            db.execute(conn, sql, binds)
        else:
            with db.connection() as c:
                db.execute(c, sql, binds)
                c.commit()
    except Exception:  # auditing must never break the request, but failures are visible in logs
        log.exception("Failed to write audit record for action %s", action)


def audit_denied(user, permission: str, request: Request) -> None:
    audit(user.username, user.role, "ACCESS_DENIED", "permission", permission,
          {"path": request.url.path, "method": request.method}, outcome="DENIED", request=request)
