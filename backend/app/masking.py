"""Masking helpers for personal data in logs, exports and API responses."""
from __future__ import annotations

import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(password|passwd|pwd|secret|token|authorization)(\"?\s*[=:]\s*\"?)([^\s\",}]+)"), r"\1\2***"),
    (re.compile(r"\b[\w.+-]+@[\w-]+(\.[\w-]+)+\b"), "***@***"),
    (re.compile(r"\+?94[-\s]?\d{2}[-\s]?\d{7}\b|\b0\d{9}\b"), "+94-**-*******"),
    (re.compile(r"\b(\d{9}[VvXx]|\d{12})\b"), "[NIC]"),
    (re.compile(r"\bDEVICE_\d+\b"), "DEVICE_****"),
]


def mask_text(text: str) -> str:
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


def mask_name(name: str | None) -> str | None:
    """'Dinesh Karunaratne' -> 'D***** K*********'."""
    if not name:
        return name
    return " ".join(p[0] + "*" * (len(p) - 1) if len(p) > 1 else p for p in name.split())


def mask_phone(phone: str | None) -> str | None:
    if not phone:
        return phone
    digits = re.sub(r"\D", "", phone)
    return "*" * max(len(digits) - 3, 0) + digits[-3:]


def mask_device(device_id: str | None) -> str | None:
    if not device_id:
        return device_id
    return device_id[:3] + "*" * max(len(device_id) - 6, 0) + device_id[-3:]


def mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return email
    local, domain = email.split("@", 1)
    return local[:1] + "***@" + domain
