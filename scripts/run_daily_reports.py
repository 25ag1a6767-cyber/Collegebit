#!/usr/bin/env python3
"""CLI runner for automated daily CampusBite AI v2.0 executive reports & email dispatch."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.database import SessionLocal  # noqa: E402
from app.modules.reporting_engine import generate_daily_reports_job  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, default=None, help="Target YYYY-MM-DD (defaults to today)")
    parser.add_argument("--emails", type=str, default=None, help="Comma-separated recipient override")
    args = parser.parse_args()

    recipients = [e.strip() for e in args.emails.split(",") if e.strip()] if args.emails else None
    with SessionLocal() as db:
        out = generate_daily_reports_job(db, target_date=args.date, email_recipients=recipients)
        print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
