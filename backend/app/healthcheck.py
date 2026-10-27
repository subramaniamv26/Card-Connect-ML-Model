"""Container health checks (exit code 0 = healthy).

    python -m app.healthcheck api      # HTTP liveness of the API
    python -m app.healthcheck worker   # worker scheduler heartbeat written within the last 120 s
"""
from __future__ import annotations

import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "api"
    try:
        if target == "api":
            with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=3) as r:
                return 0 if r.status == 200 else 1
        beat = Path(tempfile.gettempdir()) / "worker-heartbeat"
        return 0 if beat.exists() and time.time() - float(beat.read_text()) < 120 else 1
    except Exception:  # noqa: BLE001
        return 1


if __name__ == "__main__":
    sys.exit(main())
