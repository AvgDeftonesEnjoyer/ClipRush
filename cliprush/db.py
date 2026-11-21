from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from cliprush.config import get_settings


class Base(DeclarativeBase):
    pass


engine = create_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    from cliprush import models  # noqa: F401

    Base.metadata.create_all(engine)
