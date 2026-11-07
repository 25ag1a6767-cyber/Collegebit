"""
Pytest session configuration for CampusBite AI v3.0.
Ensures tests run on an isolated, disposable database copy and NEVER touch the developer's
real database (backend/ace_canteen.db).
"""
import os
import sys
import uuid
from pathlib import Path
from tempfile import gettempdir
import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
TEST_DB = Path(gettempdir()) / f"collegebits-test-{uuid.uuid4().hex}.db"

os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB.as_posix()}"
os.environ["ENVIRONMENT"] = "test"
os.environ["APP_ENV"] = "test"
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["AUTO_CREATE_TABLES"] = "true"
os.environ["SEED_DEMO_DATA"] = "true"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture(scope="session", autouse=True)
def isolated_test_database_lifecycle():
    from app.database import Base, SessionLocal, engine
    import app.models
    from app.seed import seed_demo_data

    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_demo_data(db)
    try:
        yield
    finally:
        engine.dispose()
        if TEST_DB.exists():
            TEST_DB.unlink()
