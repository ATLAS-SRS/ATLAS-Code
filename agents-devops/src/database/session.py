from __future__ import annotations

import os
from contextlib import asynccontextmanager, contextmanager

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@postgres:5432/atlas_db"


def _raw_database_url() -> str:
    return os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL).strip()


def _to_async_database_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


def _to_sync_database_url(url: str) -> str:
    if url.startswith("postgresql+psycopg://"):
        return url
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


ASYNC_DATABASE_URL = _to_async_database_url(_raw_database_url())
SYNC_DATABASE_URL = _to_sync_database_url(_raw_database_url())

async_engine = create_async_engine(
    ASYNC_DATABASE_URL,
    pool_pre_ping=True,
)
AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    expire_on_commit=False,
    class_=AsyncSession,
)

sync_engine = create_engine(
    SYNC_DATABASE_URL,
    pool_pre_ping=True,
)
SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    expire_on_commit=False,
    class_=Session,
)


async def init_database() -> None:
    async with async_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


def init_database_sync() -> None:
    """Synchronous database initialization - safe for use in Streamlit and other event loop contexts."""
    with sync_engine.begin() as connection:
        Base.metadata.create_all(bind=connection)


async def dispose_async_database() -> None:
    await async_engine.dispose()


def dispose_sync_database() -> None:
    sync_engine.dispose()


@asynccontextmanager
async def get_async_session():
    async with AsyncSessionLocal() as session:
        yield session


@contextmanager
def get_sync_session():
    session = SyncSessionLocal()
    try:
        yield session
    finally:
        session.close()
