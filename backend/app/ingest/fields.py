"""Typed CSV field parsers. Each returns the parsed value or raises FieldError(code, message).

Identifiers are kept as strings (leading zeros preserved). Lengths are checked in UTF-8 bytes to match
Oracle VARCHAR2 byte semantics. Timestamps without an offset are interpreted as Asia/Colombo local
time; timestamps with an offset are converted to Asia/Colombo.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Colombo")
ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


class FieldError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Field:
    name: str
    kind: str = "str"                    # str | id | int | decimal | date | datetime | enum | yesno | flag01 | list
    required: bool = True
    max_bytes: int | None = None
    choices: frozenset[str] | None = None
    pattern: re.Pattern[str] | None = None
    min_value: Decimal | None = None
    max_value: Decimal | None = None
    scale: int | None = None             # decimal places allowed
    precision: int | None = None         # total digits allowed
    upper: bool = False
    list_choices: frozenset[str] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def parse(self, raw: str | None) -> Any:
        v = None if raw is None else raw.strip()
        if v == "" or v is None:
            if self.required:
                raise FieldError("REQUIRED", f"{self.name} is required")
            return None
        if self.upper:
            v = v.upper()
        parser: Callable[[Field, str], Any] = _PARSERS[self.kind]
        return parser(self, v)


def _check_len(f: Field, v: str) -> str:
    if f.max_bytes is not None and len(v.encode("utf-8")) > f.max_bytes:
        raise FieldError("TOO_LONG", f"{f.name} exceeds {f.max_bytes} bytes")
    return v


def _p_str(f: Field, v: str) -> str:
    _check_len(f, v)
    if f.pattern is not None and not f.pattern.match(v):
        raise FieldError("BAD_FORMAT", f"{f.name} has an invalid format")
    return v


def _p_id(f: Field, v: str) -> str:
    _check_len(f, v)
    if not ID_RE.match(v) or (f.pattern is not None and not f.pattern.match(v)):
        raise FieldError("BAD_IDENTIFIER", f"{f.name} '{v[:30]}' is not a valid identifier")
    return v


def _p_enum(f: Field, v: str) -> str:
    v = v.upper()
    assert f.choices is not None
    if v not in f.choices:
        raise FieldError("BAD_VALUE", f"{f.name} '{v[:30]}' not in {sorted(f.choices)}")
    return v


def _p_yesno(f: Field, v: str) -> str:
    u = v.upper()
    if u in ("YES", "Y", "TRUE", "1"):
        return "YES"
    if u in ("NO", "N", "FALSE", "0"):
        return "NO"
    raise FieldError("BAD_VALUE", f"{f.name} must be YES or NO")


def _p_flag01(f: Field, v: str) -> str:
    if v in ("1", "Y", "YES", "TRUE", "true"):
        return "Y"
    if v in ("0", "N", "NO", "FALSE", "false"):
        return "N"
    raise FieldError("BAD_VALUE", f"{f.name} must be 0/1")


def _p_decimal(f: Field, v: str) -> Decimal:
    try:
        d = Decimal(v.replace(",", ""))
    except InvalidOperation:
        raise FieldError("NOT_A_NUMBER", f"{f.name} '{v[:30]}' is not a number") from None
    if not d.is_finite():
        raise FieldError("NOT_A_NUMBER", f"{f.name} is not finite")
    if f.scale is not None and -d.as_tuple().exponent > f.scale:  # type: ignore[operator]
        raise FieldError("TOO_MANY_DECIMALS", f"{f.name} allows {f.scale} decimal places")
    if f.precision is not None:
        int_digits = len(str(abs(int(d)))) if abs(d) >= 1 else 0
        if int_digits > f.precision - (f.scale or 0):
            raise FieldError("OUT_OF_RANGE", f"{f.name} is too large")
    if f.min_value is not None and d < f.min_value:
        raise FieldError("OUT_OF_RANGE", f"{f.name} must be >= {f.min_value}")
    if f.max_value is not None and d > f.max_value:
        raise FieldError("OUT_OF_RANGE", f"{f.name} must be <= {f.max_value}")
    return d


def _p_int(f: Field, v: str) -> int:
    d = _p_decimal(Field(f.name, "decimal", min_value=f.min_value, max_value=f.max_value), v)
    if d != d.to_integral_value():
        raise FieldError("NOT_AN_INTEGER", f"{f.name} must be a whole number")
    return int(d)


def _p_date(f: Field, v: str) -> date:
    """Accept YYYY-MM-DD (optionally followed by a time, which is ignored) or YYYY/MM/DD."""
    head = v[:10]
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            if len(v) > 10 and v[10] not in (" ", "T"):
                break
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    raise FieldError("BAD_DATE", f"{f.name} '{v[:30]}' is not a date (YYYY-MM-DD)")


def parse_local_datetime(v: str) -> datetime:
    """Parse ISO-like timestamps; return a naive datetime in Asia/Colombo local time."""
    s = v.strip().replace("T", " ").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise FieldError("BAD_DATETIME", f"'{v[:30]}' is not a timestamp (YYYY-MM-DD HH:MM:SS)") from None
    if dt.tzinfo is not None:
        dt = dt.astimezone(LOCAL_TZ).replace(tzinfo=None)
    return dt.replace(microsecond=0)


def _p_datetime(f: Field, v: str) -> datetime:
    try:
        return parse_local_datetime(v)
    except FieldError as e:
        raise FieldError(e.code, f"{f.name} {e.message}") from None


def _p_list(f: Field, v: str) -> str:
    """Normalise a ';' / ',' / '|' separated list to an upper-case ';'-joined string (or ALL)."""
    parts = [p.strip().upper() for p in re.split(r"[;,|]", v) if p.strip()]
    if not parts or parts == ["ALL"]:
        return "ALL"
    if f.list_choices is not None:
        bad = [p for p in parts if p not in f.list_choices]
        if bad:
            raise FieldError("BAD_VALUE", f"{f.name} has invalid entries {bad[:5]}; allowed {sorted(f.list_choices)}")
    out = ";".join(dict.fromkeys(parts))
    return _check_len(f, out)


_PARSERS: dict[str, Callable[[Field, str], Any]] = {
    "str": _p_str, "id": _p_id, "enum": _p_enum, "yesno": _p_yesno, "flag01": _p_flag01,
    "decimal": _p_decimal, "int": _p_int, "date": _p_date, "datetime": _p_datetime, "list": _p_list,
}
