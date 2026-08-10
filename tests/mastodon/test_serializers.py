from contextlib import contextmanager
from typing import Generator

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.database import async_engine
from app.mastodon import serializers


@contextmanager
def _count_statements() -> Generator[list[str], None, None]:
    """Record every SQL statement issued on the underlying sync engine, so a
    test can assert a cached path doesn't re-query."""
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    event.listen(async_engine.sync_engine, "before_cursor_execute", _record)
    try:
        yield statements
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", _record)


@pytest.mark.asyncio
async def test_muted_conversations_is_cached_per_request(
    async_db_session: AsyncSession,
) -> None:
    with _count_statements() as statements:
        await serializers._muted_conversations(async_db_session)
        query_count_after_first_call = len(statements)
        assert query_count_after_first_call > 0

        await serializers._muted_conversations(async_db_session)
        assert len(statements) == query_count_after_first_call


@pytest.mark.asyncio
async def test_muted_conversations_cache_invalidated_after_commit(
    async_db_session: AsyncSession,
) -> None:
    assert "noisy-thread" not in (
        await serializers._muted_conversations(async_db_session)
    )

    async_db_session.add(models.MutedConversation(conversation="noisy-thread"))
    await async_db_session.commit()

    assert "noisy-thread" in (await serializers._muted_conversations(async_db_session))


@pytest.mark.asyncio
async def test_owner_counts_is_cached_per_request(
    async_db_session: AsyncSession,
) -> None:
    with _count_statements() as statements:
        await serializers._owner_counts(async_db_session)
        query_count_after_first_call = len(statements)
        assert query_count_after_first_call > 0

        await serializers._owner_counts(async_db_session)
        assert len(statements) == query_count_after_first_call
