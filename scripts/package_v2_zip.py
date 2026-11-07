"""
Package CampusBite AI v2.0 into a clean distribution ZIP archive.
Copies the ZIP to:
1. clg project root
2. User's Desktop
3. User's Downloads
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
}

EXCLUDE_EXTS = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".tmp",
}

def should_exclude(rel_path: Path) -> bool:
    parts = rel_path.parts
    for part in parts:
        if part in EXCLUDE_DIRS:
            return True
    if rel_path.suffix.lower() in EXCLUDE_EXTS:
        return True
    # Exclude zip files themselves
    if rel_path.suffix.lower() == ".zip":
        return True
    return False

def package():
    zip_filename = "CampusBite-AI-v2.0.zip"
    target_zip = ROOT / zip_filename
    print(f"Creating distribution archive: {target_zip}")

    count = 0
    with zipfile.ZipFile(target_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, dirs, files in os.walk(ROOT):
            # Prune excluded directories in-place
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]

            for file in files:
                abs_file = Path(root) / file
                rel_file = abs_file.relative_to(ROOT)
                if should_exclude(rel_file):
                    continue
                zf.write(abs_file, str(rel_file))
                count += 1

    size_mb = target_zip.stat().st_size / (1024 * 1024)
    print(f"Successfully packaged {count} files into {target_zip.name} ({size_mb:.2f} MB)")

    # Copy to Desktop and Downloads
    destinations = [
        Path.home() / "Desktop" / zip_filename,
        Path.home() / "Downloads" / zip_filename,
    ]

    for dest in destinations:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target_zip, dest)
            print(f"Copied to: {dest}")
        except Exception as e:
            print(f"Notice: Could not copy to {dest}: {e}")

if __name__ == "__main__":
    package()
