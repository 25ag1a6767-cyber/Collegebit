"""
Pytest session configuration for CampusBite AI v3.0.
Ensures tests run on an isolated, disposable database copy and NEVER touch the developer's
real database (backend/ace_canteen.db).
"""
import os
import shutil
import sys
from pathlib import Path
import pytest

# Configure paths
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
REAL_DB = BACKEND_DIR / "ace_canteen.db"
TEST_DB = BACKEND_DIR / "test_isolated.db"

# 1. Point DATABASE_URL to isolated test database BEFORE any application imports
if TEST_DB.exists():
    try:
        TEST_DB.unlink()
    except Exception:
        pass

if REAL_DB.exists():
    shutil.copy2(REAL_DB, TEST_DB)

os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB.as_posix()}"
os.environ["ENVIRONMENT"] = "test"
os.environ["APP_ENV"] = "test"
os.environ["RATE_LIMIT_ENABLED"] = "false"

# Add backend to sys.path
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture(scope="session", autouse=True)
def isolated_test_database_lifecycle():
    """Session fixture ensuring complete test isolation."""
    yield

    # Teardown: close any open engine connections and remove test_isolated.db
    try:
        from app.database import engine
        engine.dispose()
    except Exception:
        pass

    if TEST_DB.exists():
        try:
            TEST_DB.unlink()
        except Exception:
            pass
