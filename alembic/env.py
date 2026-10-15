"""
Alembic Environment Configuration - async (asyncpg) version.
Reads DATABASE_URL from src.config.settings (i.e. the .env file).
"""
import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

# Add project root to path so src.* imports work
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import settings
from src.db.base import Base
from src.db.models import (  # noqa: F401 - register all models on Base.metadata
    Company,
    Asset,
    Task,
    ExplorationNode,
    ExplorationAnchor,
    Activity,
)

# Alembic Config object (parsed alembic.ini)
config = context.config

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in offline mode - no DB connection, just emit SQL."""
    url = settings.database_url
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations given a connection (called via run_sync)."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = create_async_engine(settings.database_url, poolclass=pool.NullPool)
    async with connectable.connect() as conn:
        await conn.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
