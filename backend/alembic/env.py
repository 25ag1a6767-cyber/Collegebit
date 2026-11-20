from logging.config import fileConfig
from alembic import context
from sqlalchemy import engine_from_config, inspect, pool, text
from app.config import get_settings
from app.database import Base, normalize_sqlalchemy_url
from app import models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
config.set_main_option("sqlalchemy.url", normalize_sqlalchemy_url(get_settings().database_url).replace("%", "%%"))
target_metadata = Base.metadata


def _ensure_wide_alembic_version(connection) -> None:
    """Serialize Postgres migrations per-tx and widen alembic_version.version_num past 32 chars."""
    if connection.dialect.name != "postgresql":
        return
    connection.execute(text("SELECT pg_advisory_xact_lock(748291045)"))
    inspector = inspect(connection)
    if not inspector.has_table("alembic_version"):
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version ("
                "version_num VARCHAR(128) NOT NULL, "
                "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)"
                ")"
            )
        )
        return
    cols = {col["name"]: col for col in inspector.get_columns("alembic_version")}
    version_col = cols.get("version_num")
    length = getattr(version_col.get("type"), "length", None) if version_col else None
    if length is not None and length < 128:
        connection.execute(text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(128)"))


def run_migrations_offline():
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"}, compare_type=True)
    with context.begin_transaction(): context.run_migrations()

def run_migrations_online():
    provided_connection = config.attributes.get("connection")
    if provided_connection is not None:
        context.configure(connection=provided_connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            _ensure_wide_alembic_version(provided_connection)
            context.run_migrations()
        return
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool, pool_pre_ping=True)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            _ensure_wide_alembic_version(connection)
            context.run_migrations()

if context.is_offline_mode(): run_migrations_offline()
else: run_migrations_online()
