"""Run reproducible local release checks for ACE Canteen.

This script never contacts Razorpay or a production database. Use --browser only
when Node dependencies and the Playwright browser are installed.
"""
from __future__ import annotations
import argparse
import shutil
import subprocess
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def run(label: str, command: list[str], *, required: bool = True) -> bool:
    print(f"\n== {label} ==", flush=True)
    resolved = shutil.which(command[0])
    if resolved is None:
        print(f"SKIP: executable not found: {command[0]}")
        return not required
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "backend") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    result = subprocess.run([resolved, *command[1:]], cwd=ROOT, env=env, check=False)
    print(f"{'PASS' if result.returncode == 0 else 'FAIL'}: {label} (exit {result.returncode})")
    return result.returncode == 0

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", action="store_true", help="Run Playwright tests; requires npm install and browser installation")
    args = parser.parse_args()
    py = sys.executable
    results = []
    results.append(run("Backend regression suite (isolated DB)", [py, "-m", "pytest", "-q", "backend/tests"]))
    results.append(run("Python compilation", [py, "-m", "compileall", "-q", "backend/app", "backend/tests", "scripts"]))
    results.append(run("Frontend JavaScript syntax (config.js)", ["node", "--check", "frontend/config.js"], required=True))
    results.append(run("Frontend JavaScript syntax (app.js)", ["node", "--check", "frontend/js/app.js"], required=True))
    results.append(run("Frontend JavaScript syntax (v1-platform.js)", ["node", "--check", "frontend/js/v1-platform.js"], required=True))
    results.append(run("Frontend JavaScript syntax (charts-v2.js)", ["node", "--check", "frontend/js/charts-v2.js"], required=True))
    results.append(run("Frontend JavaScript syntax (staff-portal.js)", ["node", "--check", "frontend/js/staff-portal.js"], required=True))
    results.append(run("Vendor QR decoder syntax (jsQR.js)", ["node", "--check", "frontend/js/vendor/jsQR.js"], required=True))
    results.append(run("Frontend JavaScript syntax (admin-portal.js)", ["node", "--check", "frontend/js/admin-portal.js"], required=True))
    results.append(run("Service worker syntax (sw.js)", ["node", "--check", "frontend/sw.js"], required=True))
    if args.browser:
        pw_cli = ROOT / "node_modules" / "@playwright" / "test" / "cli.js"
        pw_cmd = ["node", str(pw_cli), "test"] if pw_cli.is_file() else ["npx", "playwright", "test"]
        results.append(run("Playwright browser acceptance", pw_cmd, required=True))
    print("\nRelease gate: " + ("PASS" if all(results) else "FAIL"))
    if not args.browser:
        print("Browser acceptance was not run. Use --browser after installing npm dependencies and Playwright browsers.")
    print("Live payment-provider, cloud infrastructure, load, and restore-drill checks are outside this local gate.")
    return 0 if all(results) else 1

if __name__ == "__main__":
    raise SystemExit(main())
