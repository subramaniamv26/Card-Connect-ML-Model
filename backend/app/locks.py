"""Lease-based mutual exclusion stored in JOB_LOCKS (also safe if two worker processes are started by mistake).

A lock is acquired with a single conditional UPDATE, so two workers can never both succeed. A lease
expires automatically if its holder dies.
"""
from __future__ import annotations

import os
import socket
import uuid
from contextlib import contextmanager
from datetime import timedelta
from typing import Iterator

from . import db

OWNER_PREFIX = f"{socket.gethostname()}:{os.getpid()}"


class LockBusy(RuntimeError):
    pass


def acquire(conn, name: str, lease_seconds: int) -> str | None:
    owner = f"{OWNER_PREFIX}:{uuid.uuid4().hex[:8]}"
    n = db.execute(conn, """
        UPDATE job_locks SET owner_id = :o, acquired_at = SYS_EXTRACT_UTC(SYSTIMESTAMP), expires_at = SYS_EXTRACT_UTC(SYSTIMESTAMP) + :lease
        WHERE lock_name = :n AND (owner_id IS NULL OR expires_at < SYS_EXTRACT_UTC(SYSTIMESTAMP))""",
        {"o": owner, "n": name, "lease": timedelta(seconds=lease_seconds)})
    conn.commit()
    return owner if n == 1 else None


def release(conn, name: str, owner: str) -> None:
    db.execute(conn, """UPDATE job_locks SET owner_id = NULL, acquired_at = NULL, expires_at = NULL
                        WHERE lock_name = :n AND owner_id = :o""", {"n": name, "o": owner})
    conn.commit()


@contextmanager
def held(name: str, lease_seconds: int) -> Iterator[str]:
    with db.connection() as conn:
        owner = acquire(conn, name, lease_seconds)
    if owner is None:
        raise LockBusy(f"{name} is already running")
    try:
        yield owner
    finally:
        with db.connection() as conn:
            release(conn, name, owner)
