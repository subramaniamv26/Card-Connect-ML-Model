"""Create or reset an application user. The password is read from the environment variable named by
--password-env (default APP_USER_PASSWORD) or prompted for; it is never accepted on the command line.

    APP_USER_PASSWORD=... python -m scripts.create_user --username admin --role ADMIN
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

from app import db
from app.logging_conf import configure_logging
from app.security import ROLES, hash_password


def main() -> int:
    configure_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--username", required=True)
    ap.add_argument("--role", required=True, choices=ROLES)
    ap.add_argument("--password-env", default="APP_USER_PASSWORD")
    a = ap.parse_args()
    pw = os.environ.get(a.password_env) or getpass.getpass(f"Password for {a.username}: ")
    hashed = hash_password(pw)
    with db.connection() as conn:
        db.execute(conn, """MERGE INTO app_users u USING (SELECT :u username FROM dual) s ON (u.username = s.username)
                            WHEN MATCHED THEN UPDATE SET password_hash = :h, role = :r, is_active = 'Y'
                            WHEN NOT MATCHED THEN INSERT (username, password_hash, role) VALUES (:u, :h, :r)""",
                   {"u": a.username, "h": hashed, "r": a.role})
        conn.commit()
    print(f"User {a.username} ({a.role}) saved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
