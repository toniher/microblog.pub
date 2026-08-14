"""`prefetch_status_relations` must make status serialization O(1) in queries
rather than O(page size).

The assertion is deliberately *relative* — the exact number of SELECTs behind a
page is an implementation detail that legitimately changes, but it must not
grow with the number of statuses on the page. That is the N+1 property itself.
"""

import pytest
from sqlalchemy import event
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import activitypub.models
from activitypub import activitypub as ap
from activitypub import boxes
from activitypub.ap_object import ObjectType
from app.database import async_engine
from app.mastodon import serializers


async def _make_reply_chains(db_session: AsyncSession, count: int) -> None:
    """`count` of the owner's own posts, each replying to a distinct parent."""
    for i in range(count):
        _, parent = await boxes.send_create(
            db_session,
            ObjectType.NOTE.value,
            f"parent {i}",
            uploads=[],
            in_reply_to=None,
            visibility=ap.VisibilityEnum.PUBLIC,
        )
        await boxes.send_create(
            db_session,
            ObjectType.NOTE.value,
            f"reply {i}",
            uploads=[],
            in_reply_to=parent.ap_id,
            visibility=ap.VisibilityEnum.PUBLIC,
        )
    await db_session.commit()


async def _load_replies(db_session: AsyncSession) -> list:
    everything = list(
        (await db_session.scalars(select(activitypub.models.OutboxObject)))
        .unique()
        .all()
    )
    # `in_reply_to` is a wrapper property over ap_object, not a column, so the
    # filtering happens here; the re-fetch goes through the real getter so the
    # eager-loads `serialize_status` relies on are in place.
    return await boxes.get_outbox_objects_by_ap_ids(
        db_session, [obj.ap_id for obj in everything if obj.in_reply_to]
    )


async def _count_selects_for_page(db_session: AsyncSession, objects: list) -> int:
    counter = {"n": 0}

    def _count(conn, cursor, statement, params, context, executemany) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            counter["n"] += 1

    event.listen(async_engine.sync_engine, "before_cursor_execute", _count)
    try:
        await serializers.prefetch_status_relations(db_session, objects)
        for obj in objects:
            await serializers.serialize_status(db_session, obj)
    finally:
        event.remove(async_engine.sync_engine, "before_cursor_execute", _count)
    return counter["n"]


@pytest.mark.parametrize("page_size", [2, 10])
@pytest.mark.asyncio
async def test_prefetch_keeps_query_count_flat(
    async_db_session: AsyncSession,
    page_size: int,
) -> None:
    await _make_reply_chains(async_db_session, page_size)
    replies = await _load_replies(async_db_session)
    assert len(replies) == page_size

    n_queries = await _count_selects_for_page(async_db_session, replies)

    # Two batch queries (outbox + inbox) plus the per-request memoized
    # lookups, whatever their count — the point is that it does not scale
    # with `page_size`. Without the prefetch this is ~1 + 1 per status.
    assert n_queries <= 6, f"{n_queries} queries for a page of {page_size}"

    statuses = [
        await serializers.serialize_status(async_db_session, obj) for obj in replies
    ]
    assert all(s["in_reply_to_id"] for s in statuses), "parents must still resolve"


@pytest.mark.asyncio
async def test_prefetch_is_optional(async_db_session: AsyncSession) -> None:
    """`serialize_status` stays correct without a prefetch — it just falls
    back to one query per relation."""
    await _make_reply_chains(async_db_session, 3)
    replies = await _load_replies(async_db_session)

    statuses = [
        await serializers.serialize_status(async_db_session, obj) for obj in replies
    ]
    assert len(statuses) == 3
    assert all(s["in_reply_to_id"] for s in statuses)
