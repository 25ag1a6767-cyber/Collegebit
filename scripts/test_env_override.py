import os
import sys
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite:///./backend/test_isolated.db"
sys.path.insert(0, "backend")

from app.config import get_settings
from app.database import SQLALCHEMY_DATABASE_URL, engine

print("Settings DB URL:", get_settings().database_url)
print("SQLAlchemy DB URL:", SQLALCHEMY_DATABASE_URL)
print("Engine URL:", engine.url)
