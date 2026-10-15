from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from typing import AsyncGenerator

from src.config import settings
from src.db.base import Base


# Read the database URL from the environment (.env) instead of hard-coding it.
DATABASE_URL = settings.database_url

engine = create_async_engine(
    DATABASE_URL,
    echo=settings.db_echo,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db():
    await engine.dispose()
