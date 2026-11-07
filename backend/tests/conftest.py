"""Pytest session configuration using a clean, disposable SQLite database."""
import os
import sys
from pathlib import Path
import pytest

# Configure paths
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
TEST_DB = BACKEND_DIR / "test_isolated.db"

# Point DATABASE_URL to an isolated test database before importing the application.
if TEST_DB.exists():
    try:
        TEST_DB.unlink()
    except Exception:
        pass

os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB.as_posix()}"
os.environ["ENVIRONMENT"] = "test"
os.environ["APP_ENV"] = "test"
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["AUTO_CREATE_TABLES"] = "true"
os.environ["SEED_DEMO_DATA"] = "true"

# Add backend to sys.path
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture(scope="session", autouse=True)
def isolated_test_database_lifecycle():
    """Create a clean schema without relying on a developer's local database."""
    from app.database import Base, engine

    Base.metadata.create_all(bind=engine)
    yield

    try:
        engine.dispose()
    except Exception:
        pass

    if TEST_DB.exists():
        try:
            TEST_DB.unlink()
        except Exception:
            pass
