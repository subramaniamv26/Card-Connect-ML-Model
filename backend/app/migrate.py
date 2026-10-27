"""Versioned schema migrations.

Files in backend/migrations named V<NNN>__<description>.sql are applied in order and recorded in
SCHEMA_MIGRATIONS with a SHA-256 checksum. An applied file whose checksum later changes is an error
(write a new migration instead of editing an old one).

    python -m app.migrate            # apply pending migrations
    python -m app.migrate --status   # list applied / pending
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
from pathlib import Path

import oracledb

from . import db
from .logging_conf import configure_logging

log = logging.getLogger(__name__)
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
FILE_RE = re.compile(r"^V(\d{3})__([A-Za-z0-9_]+)\.sql$")


def split_statements(sql: str) -> list[str]:
    """Split a migration into statements terminated by ';' at end of line. Comment lines are dropped."""
    lines = [ln for ln in sql.splitlines() if not ln.lstrip().startswith("--")]
    stmts, buf = [], []
    for ln in lines:
        buf.append(ln)
        if ln.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip().rstrip(";").strip()
            if stmt:
                stmts.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


def discover(directory: Path = MIGRATIONS_DIR) -> list[tuple[str, str, Path, str]]:
    found = []
    for p in sorted(directory.glob("V*.sql")):
        m = FILE_RE.match(p.name)
        if not m:
            raise ValueError(f"Bad migration file name: {p.name}")
        content = p.read_text(encoding="utf-8")
        found.append((m.group(1), m.group(2), p, hashlib.sha256(content.encode()).hexdigest()))
    return found


def ensure_table(conn: oracledb.Connection) -> None:
    exists = db.scalar(conn, "SELECT COUNT(*) FROM user_tables WHERE table_name = 'SCHEMA_MIGRATIONS'")
    if not exists:
        db.execute(conn, """
            CREATE TABLE schema_migrations (
                version      VARCHAR2(3)   PRIMARY KEY,
                description  VARCHAR2(200) NOT NULL,
                checksum     VARCHAR2(64)  NOT NULL,
                applied_at   TIMESTAMP     DEFAULT SYS_EXTRACT_UTC(SYSTIMESTAMP) NOT NULL
            )""")


def applied(conn: oracledb.Connection) -> dict[str, str]:
    return {r["version"]: r["checksum"] for r in db.fetch_all(conn, "SELECT version, checksum FROM schema_migrations")}


def migrate(conn: oracledb.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    ensure_table(conn)
    done = applied(conn)
    newly = []
    for version, desc, path, checksum in discover(directory):
        if version in done:
            if done[version] != checksum:
                raise RuntimeError(f"Migration V{version} was modified after being applied")
            continue
        log.info("Applying V%s %s", version, desc)
        for stmt in split_statements(path.read_text(encoding="utf-8")):
            try:
                db.execute(conn, stmt)
            except oracledb.Error as exc:
                conn.rollback()
                raise RuntimeError(f"V{version} failed on statement:\n{stmt[:300]}\n{exc}") from exc
        db.execute(conn, "INSERT INTO schema_migrations (version, description, checksum) VALUES (:1, :2, :3)",
                   [version, desc, checksum])
        conn.commit()
        newly.append(version)
    return newly


def main() -> int:
    configure_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    with db.connection() as conn:
        if a.status:
            ensure_table(conn)
            done = applied(conn)
            for version, desc, _, _ in discover():
                print(f"V{version} {desc}: {'applied' if version in done else 'PENDING'}")
            return 0
        newly = migrate(conn)
        print(f"Applied: {', '.join('V' + v for v in newly) or 'nothing (up to date)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
