#!/usr/bin/env python3
"""CampusBite AI v2.0 — Daily Scheduled Four-Day Hybrid Data Retention CLI."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.database import SessionLocal  # noqa: E402
from app.modules.retention_backup import run_four_day_retention_cleanup  # noqa: E402

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=None, help="Override temporary data retention days (default: 4)")
    parser.add_argument("--dry-run", action="store_true", help="Report eligible rows without deleting")
    args = parser.parse_args()

    with SessionLocal() as db:
        result = run_four_day_retention_cleanup(db, retention_days=args.days, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in {"completed", "dry_run"} else 2

if __name__ == "__main__":
    raise SystemExit(main())
