"""Test configuration.

Unit tests need no database. Settings are given harmless placeholder values here so importing the app
never requires real credentials. Integration tests (tests/integration) run only when ORACLE_TEST_DSN,
ORACLE_TEST_USER and ORACLE_TEST_PASSWORD point at a disposable test schema.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if os.environ.get("ORACLE_TEST_DSN"):
    os.environ["ORACLE_DSN"] = os.environ["ORACLE_TEST_DSN"]
    os.environ["ORACLE_USER"] = os.environ["ORACLE_TEST_USER"]
    os.environ["ORACLE_PASSWORD"] = os.environ["ORACLE_TEST_PASSWORD"]
os.environ.setdefault("ORACLE_DSN", "localhost:1/UNITTEST")
os.environ.setdefault("ORACLE_USER", "unit")
os.environ.setdefault("ORACLE_PASSWORD", "unit-test-placeholder")
os.environ.setdefault("JWT_SECRET", "unit-test-secret-that-is-long-enough-0123456789")
