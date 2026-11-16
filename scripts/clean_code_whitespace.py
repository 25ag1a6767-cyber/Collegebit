"""Clean trailing spaces and excessive blank lines across the entire CollegeBits repository
while keeping code readable, PEP-8 compliant, and student-friendly.
"""
from __future__ import annotations

from pathlib import Path
import re

REPO_ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "backups", "data", ".pytest_cache", "dist", "dist-ios"}
TEXT_EXTS = {".py", ".js", ".ts", ".tsx", ".html", ".css", ".md", ".yml", ".yaml", ".json", ".sh", ".ps1", ".conf"}


def clean_file_content(text: str, ext: str) -> str:
    # 1. Strip trailing spaces/tabs on every line
    lines = [ln.rstrip(" \t\r") for ln in text.splitlines()]
    cleaned = "\n".join(lines)

    # 2. Collapse excessive consecutive blank lines
    if ext == ".py":
        # At most 2 consecutive blank lines at module level (PEP 8), and 1 inside indented blocks
        cleaned = re.sub(r"\n{4,}", "\n\n\n", cleaned)
    else:
        # At most 1 blank line between blocks in HTML/CSS/JS/TS/MD/YAML
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)

    return cleaned.strip() + "\n"


def main() -> None:
    modified_files = 0
    total_scanned = 0
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in TEXT_EXTS:
            continue
        total_scanned += 1
        try:
            raw = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        updated = clean_file_content(raw, path.suffix.lower())
        if updated != raw:
            path.write_text(updated, encoding="utf-8", newline="\n")
            modified_files += 1

    print(f"Scanned {total_scanned} source files; cleaned trailing spaces & extra blank lines in {modified_files} files.")


if __name__ == "__main__":
    main()
