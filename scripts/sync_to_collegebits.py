"""
Sync updated codebase and database from clg to collegebits.
"""
import shutil
from pathlib import Path

SRC = Path("C:/Users/HP/Desktop/clg")
DST = Path("C:/Users/HP/Desktop/collegebits")

if DST.exists():

    files_to_sync = [
        "frontend/admin.html",
        "frontend/staff.html",
        "frontend/index.html",
        "frontend/js/admin-portal.js",
        "frontend/js/staff-portal.js",
        "frontend/js/app.js",
        "frontend/css/style.css",
        "frontend/css/admin-portal.css",
        "frontend/css/staff-portal.css",
        "backend/app/main.py",
        "backend/app/modules/auth_profile.py",
        "backend/app/modules/admin_portal.py",
        "backend/ace_canteen.db",
        "START_LOCAL_DEV.bat",
        "scripts/cleanup_counters_and_discounts.py",
    ]

    for rel in files_to_sync:
        s = SRC / rel
        d = DST / rel
        if s.exists():
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
            print(f"Synced: {rel}")
    print("All files successfully synced to collegebits!")
else:
    print("collegebits folder not found, skipping sync.")
