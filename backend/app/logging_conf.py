"""Logging with personal-data masking.

A filter rewrites every record's final message so NICs, phone numbers, e-mail addresses, device ids,
card ids and anything that looks like a password/secret assignment are masked before output.
Code should still avoid logging personal data in the first place; this is a safety net.
"""
from __future__ import annotations

import logging

from .masking import mask_text


class MaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - malformed format args
            return True
        record.msg = mask_text(msg)
        record.args = ()
        if record.exc_info and not record.exc_text:
            record.exc_text = mask_text(logging.Formatter().formatException(record.exc_info))
        return True


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if any(isinstance(f, MaskingFilter) for h in root.handlers for f in h.filters):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(MaskingFilter())
    root.handlers = [handler]
    root.setLevel(level)
    for noisy in ("apscheduler.executors.default", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
