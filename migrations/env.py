"""Alembic migration environment; never store DB secrets in this file."""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from cliprush.config import get_settings
from cliprush.db import Base
from cliprush import models  # noqa: F401 - register ORM metadata

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata
database_url = get_settings().database_url

if context.is_offline_mode():
    context.configure(url=database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(database_url, poolclass=pool.NullPool, future=True)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata,
                              compare_type=True)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()
