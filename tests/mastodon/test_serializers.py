import secrets
from contextlib import contextmanager
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from activitypub import activitypub as ap
from activitypub import boxes
from activitypub.ap_object import ObjectType
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


async def _make_access_token(db_session: AsyncSession, scope: str) -> str:
    token = models.IndieAuthAccessToken(
        access_token=secrets.token_urlsafe(16),
        refresh_token=None,
        expires_in=3600,
        scope=scope,
    )
    db_session.add(token)
    await db_session.commit()
    return token.access_token


async def _seed_own_posts(db_session: AsyncSession, count: int) -> None:
    for i in range(count):
        await boxes.send_create(
            db_session,
            ObjectType.NOTE.value,
            f"Post {i}",
            uploads=[],
            in_reply_to=None,
            visibility=ap.VisibilityEnum.PUBLIC,
        )


@pytest.mark.asyncio
async def test_home_timeline_query_count_does_not_scale_with_status_count(
    client: TestClient,
    async_db_session: AsyncSession,
) -> None:
    """The regression guard for the serializer N+1 (`performance.md` #3).

    Asserting a fixed threshold would need re-tuning whenever an unrelated
    query is added, so assert the property that actually matters instead:
    serving four times as many statuses must not cost a single extra query.
    Top-level own posts only — the per-status `in_reply_to`/reblog lookups are
    a separate, still-outstanding N+1 (the deferred batch prefetch).
    """
    token = await _make_access_token(async_db_session, "read:statuses")
    headers = {"Authorization": f"Bearer {token}"}

    await _seed_own_posts(async_db_session, 2)
    with _count_statements() as statements:
        response = client.get("/api/v1/timelines/home", headers=headers)
        assert response.status_code == 200
        assert len(response.json()) == 2
        baseline = len(statements)
    assert baseline > 0

    await _seed_own_posts(async_db_session, 6)
    with _count_statements() as statements:
        response = client.get("/api/v1/timelines/home", headers=headers)
        assert response.status_code == 200
        assert len(response.json()) == 8
        grown = len(statements)

    assert grown == baseline, (
        f"serving 8 statuses issued {grown} queries vs {baseline} for 2 — the "
        f"per-status memoization in serializers.py has regressed"
    )


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
