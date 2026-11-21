"""Database connections. PostgreSQL schema is managed by Alembic, never create_all."""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from cliprush.config import get_settings


class Base(DeclarativeBase):
    pass


engine = create_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

SCHEMA_VERSION = "20261122_01"


def init_db() -> None:
    from cliprush import models  # noqa: F401

    if engine.dialect.name == "sqlite":
        # Unit tests / explicitly local prototype only. Production uses Postgres + Alembic.
        Base.metadata.create_all(engine)
        return
    with engine.connect() as connection:
        try:
            version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        except Exception as exc:
            raise RuntimeError("Database schema is not migrated. Run: alembic upgrade head") from exc
    if version != SCHEMA_VERSION:
        raise RuntimeError(f"Database schema {version!r} != expected {SCHEMA_VERSION!r}; run alembic upgrade head")
