"""Shared API helpers: pagination, connection dependency, small utilities."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from fastapi import Query

from .. import db


def get_conn() -> Iterator:
    with db.connection() as conn:
        yield conn


class Page:
    def __init__(self, page: int = Query(1, ge=1), page_size: int = Query(25, ge=1, le=200)):
        self.page, self.page_size = page, page_size

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    def wrap(self, items: list[Any], total: int) -> dict[str, Any]:
        return {"items": items, "page": self.page, "page_size": self.page_size, "total": total,
                "pages": (total + self.page_size - 1) // self.page_size}


def clean(row: dict) -> dict:
    """Convert datetimes to ISO strings. Every *_at column holds naive UTC and is emitted with a 'Z' suffix."""
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat() + ("Z" if k.endswith("_at") else "")
        elif hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def to_utc_naive(dt: datetime) -> datetime:
    """Aware -> UTC; naive -> interpreted as Asia/Colombo local time -> UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("Asia/Colombo"))
    return dt.astimezone(timezone.utc).replace(tzinfo=None)
