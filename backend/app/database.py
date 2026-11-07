"""SQLAlchemy engine/session configuration for College PostgreSQL (+ PgBouncer), Neon Free PostgreSQL, and SQLite."""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import get_settings


def normalize_sqlalchemy_url(raw_url: str) -> str:
    url = (raw_url or "").strip()
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


settings = get_settings()
SQLALCHEMY_DATABASE_URL = normalize_sqlalchemy_url(settings.database_url)
IS_SQLITE = SQLALCHEMY_DATABASE_URL.startswith("sqlite")
IS_POSTGRES = SQLALCHEMY_DATABASE_URL.startswith("postgresql")

if IS_SQLITE:
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        pool_pre_ping=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )
elif settings.db_use_pgbouncer_transaction_pool:
    # Behind PgBouncer in transaction mode, disable client-side pooling and psycopg3 prepared statements.
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        poolclass=NullPool,
        connect_args={"prepare_threshold": None},
    )
else:
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL,
        pool_pre_ping=True,
        pool_size=max(2, settings.db_pool_size),
        max_overflow=max(0, settings.db_max_overflow),
        pool_timeout=max(5, settings.db_pool_timeout),
        pool_recycle=max(60, settings.db_pool_recycle_seconds),
    )

if IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _configure_sqlite_connection(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
