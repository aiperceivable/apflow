"""Application-bound session factories and async session lifetime management."""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Callable
from typing import Any
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker


def session_factory_for(session: Session | AsyncSession) -> Callable[[], Any]:
    """Reuse the exact engine, including an in-memory SQLite database."""
    if isinstance(session, AsyncSession):
        return async_sessionmaker(bind=session.bind, expire_on_commit=False)
    return sessionmaker(bind=session.get_bind(), expire_on_commit=False)


@asynccontextmanager
async def session_scope(factory: Callable[[], Any]) -> AsyncIterator[Any]:
    """Close and roll back request sessions on success, errors, and disconnects."""
    session = factory()
    if isinstance(session, AsyncSession):
        async with session:
            yield session
    else:
        with session:
            yield session
