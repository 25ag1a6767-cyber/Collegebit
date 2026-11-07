"""
Package CampusBite AI v3.0 into a clean distribution ZIP archive.
Ensures zero real database files, zero secret environment files, zero logs,
and zero temporary files are bundled in releases.
"""
import os
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".coverage",
    "htmlcov",
    "backups",
    "playwright-report",
    "test-results",
}

EXCLUDE_EXTS = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".tmp",
    ".db",
    ".db-wal",
    ".db-shm",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".zip",
}

def should_exclude(rel_path: Path) -> bool:
    parts = rel_path.parts
    for part in parts:
        if part in EXCLUDE_DIRS:
            return True
    if rel_path.suffix.lower() in EXCLUDE_EXTS:
        return True
    name = rel_path.name.lower()
    # Exclude secret env files, keep example templates
    if name.startswith(".env") and not name.endswith(".example"):
        return True
    return False

def package():
    zip_filename = "CampusBite-AI-v3.0.zip"
    target_zip = ROOT / zip_filename
    print(f"Creating clean production distribution archive: {target_zip}")

    count = 0
    with zipfile.ZipFile(target_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]

            for file in files:
                abs_file = Path(root) / file
                rel_file = abs_file.relative_to(ROOT)
                if should_exclude(rel_file):
                    continue
                zf.write(abs_file, str(rel_file))
                count += 1

    size_mb = target_zip.stat().st_size / (1024 * 1024)
    print(f"Successfully packaged {count} clean files into {target_zip.name} ({size_mb:.2f} MB)")

    # Test ZIP integrity
    with zipfile.ZipFile(target_zip, "r") as zf:
        corrupted = zf.testzip()
        if corrupted:
            raise RuntimeError(f"Corrupted file in ZIP: {corrupted}")
        # Verify no database or secret files leaked into the ZIP
        names = zf.namelist()
        leaked_db = [n for n in names if n.endswith((".db", ".sqlite", ".sqlite3"))]
        leaked_env = [n for n in names if n.split("/")[-1].startswith(".env") and not n.endswith(".example")]
        if leaked_db:
            raise RuntimeError(f"Database leak detected in ZIP: {leaked_db}")
        if leaked_env:
            raise RuntimeError(f"Environment secret leak detected in ZIP: {leaked_env}")
        print("ZIP integrity and security verification: 100% CLEAN (no DB, no secrets).")

    destinations = [
        Path.home() / "Desktop" / zip_filename,
        Path.home() / "Downloads" / zip_filename,
        Path("C:/Users/HP/.gemini/antigravity/brain/46d5dd4e-034f-4a7c-a76a-a8c0c74b6f91") / zip_filename,
        # Keep v2 backward compatibility alias
        Path.home() / "Desktop" / "CampusBite-AI-v2.0.zip",
        Path.home() / "Downloads" / "CampusBite-AI-v2.0.zip",
        Path("C:/Users/HP/.gemini/antigravity/brain/46d5dd4e-034f-4a7c-a76a-a8c0c74b6f91") / "CampusBite-AI-v2.0.zip",
    ]

    for dest in destinations:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target_zip, dest)
            print(f"Copied clean package to: {dest}")
        except Exception as e:
            print(f"Notice: Could not copy to {dest}: {e}")

if __name__ == "__main__":
    package()
