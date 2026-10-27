"""Oracle connectivity: python-oracledb thin-mode session pool and small query helpers.

All SQL in this code base uses bind variables. Identifiers are never built from user input; where a
column or table name varies (e.g. import targets), it is chosen from a fixed allow-list in code.
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Sequence

import oracledb

from .config import Settings, get_settings

log = logging.getLogger(__name__)

oracledb.defaults.fetch_lobs = False  # return CLOBs as str

_pool: oracledb.ConnectionPool | None = None
_pool_lock = threading.Lock()


def create_pool(settings: Settings | None = None) -> oracledb.ConnectionPool:
    s = settings or get_settings()
    params = oracledb.PoolParams(
        min=s.oracle_pool_min,
        max=s.oracle_pool_max,
        increment=s.oracle_pool_increment,
        getmode=oracledb.POOL_GETMODE_TIMEDWAIT,
        wait_timeout=30_000,
        ping_interval=60,
        ssl_server_dn_match=s.oracle_ssl_server_dn_match,
    )
    log.info("Creating Oracle pool dsn=%s user=%s min=%s max=%s", s.oracle_dsn, s.oracle_user,
             s.oracle_pool_min, s.oracle_pool_max)
    return oracledb.create_pool(
        user=s.oracle_user,
        password=s.oracle_password.get_secret_value(),
        dsn=s.oracle_dsn,
        params=params,
    )


def get_pool() -> oracledb.ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = create_pool()
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close(force=True)
        _pool = None


@contextmanager
def connection() -> Iterator[oracledb.Connection]:
    """Borrow a pooled connection. Commits are explicit; uncommitted work is rolled back on error."""
    conn = get_pool().acquire()
    conn.call_timeout = get_settings().oracle_call_timeout_ms
    try:
        yield conn
    except Exception:
        try:
            conn.rollback()
        except oracledb.Error:
            pass
        raise
    finally:
        get_pool().release(conn)


def _rows_to_dicts(cur: oracledb.Cursor, rows: Iterable[Sequence[Any]]) -> list[dict[str, Any]]:
    cols = [d[0].lower() for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def fetch_all(conn: oracledb.Connection, sql: str, params: dict | Sequence | None = None,
              arraysize: int = 1000) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.arraysize = arraysize
        cur.execute(sql, params or {})
        return _rows_to_dicts(cur, cur.fetchall())


def fetch_one(conn: oracledb.Connection, sql: str, params: dict | Sequence | None = None) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        row = cur.fetchone()
        return None if row is None else _rows_to_dicts(cur, [row])[0]


def scalar(conn: oracledb.Connection, sql: str, params: dict | Sequence | None = None) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        row = cur.fetchone()
        return None if row is None else row[0]


def execute(conn: oracledb.Connection, sql: str, params: dict | Sequence | None = None) -> int:
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        return cur.rowcount


def executemany(conn: oracledb.Connection, sql: str, rows: list, batch_errors: bool = False) -> list:
    if not rows:
        return []
    with conn.cursor() as cur:
        cur.executemany(sql, rows, batcherrors=batch_errors)
        return cur.getbatcherrors() if batch_errors else []


def insert_returning_id(conn: oracledb.Connection, sql: str, params: dict, id_col: str) -> int:
    """Run an INSERT ... RETURNING <id_col> INTO :new_id and return the generated id."""
    with conn.cursor() as cur:
        new_id = cur.var(oracledb.DB_TYPE_NUMBER)
        cur.execute(f"{sql} RETURNING {id_col} INTO :new_id", {**params, "new_id": new_id})
        return int(new_id.getvalue()[0])
