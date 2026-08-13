import asyncio
import json
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.dialects import sqlite

import activitypub.models
from activitypub.actor import LOCAL_ACTOR
from activitypub.ap_object import RemoteObject
from activitypub.outgoing_activities import _MAX_RETRIES
from activitypub.outgoing_activities import fetch_next_outgoing_activities
from activitypub.outgoing_activities import fetch_next_outgoing_activity
from activitypub.outgoing_activities import new_outgoing_activity
from activitypub.outgoing_activities import process_next_outgoing_activity
from activitypub.outgoing_activities import process_outgoing_activities_batch
from activitypub.tests import factories
from app.database import AsyncSession
from app.utils.datetime import now


def _setup_outbox_object(
    follow_id: str | None = None,
) -> activitypub.models.OutboxObject:
    ra = factories.RemoteActorFactory(
        base_url="https://example.com",
        username="toto",
        public_key="pk",
    )

    # And a Follow activity in the outbox
    follow_id = follow_id or uuid4().hex
    follow_from_outbox = RemoteObject(
        factories.build_follow_activity(
            from_remote_actor=LOCAL_ACTOR,
            for_remote_actor=ra,
            outbox_public_id=follow_id,
        ),
        LOCAL_ACTOR,
    )
    outbox_object = factories.OutboxObjectFactory.from_remote_object(
        follow_id, follow_from_outbox
    )
    return outbox_object


@pytest.mark.asyncio
async def test_new_outgoing_activity(
    async_db_session: AsyncSession,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    outbox_object = _setup_outbox_object()
    inbox_url = "https://example.com/inbox"

    if not outbox_object.id:
        raise ValueError("Should never happen")

    # When queuing the activity
    outgoing_activity = await new_outgoing_activity(
        async_db_session, inbox_url, outbox_object.id
    )
    await async_db_session.commit()

    assert (
        await async_db_session.execute(select(activitypub.models.OutgoingActivity))
    ).scalar_one() == outgoing_activity
    assert outgoing_activity.outbox_object_id == outbox_object.id
    assert outgoing_activity.recipient == inbox_url


@pytest.mark.asyncio
async def test_process_next_outgoing_activity__no_next_activity(
    respx_mock: respx.MockRouter,
    async_db_session: AsyncSession,
) -> None:
    next_activity = await fetch_next_outgoing_activity(async_db_session)
    assert next_activity is None


@pytest.mark.asyncio
async def test_process_next_outgoing_activity__server_200(
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    # And an outgoing activity
    outbox_object = _setup_outbox_object()

    recipient_inbox_url = "https://example.com/users/toto/inbox"
    respx_mock.post(recipient_inbox_url).mock(return_value=httpx.Response(204))

    outgoing_activity = factories.OutgoingActivityFactory(
        recipient=recipient_inbox_url,
        outbox_object_id=outbox_object.id,
        inbox_object_id=None,
        webmention_target=None,
    )

    # When processing the next outgoing activity
    # Then it is processed
    next_activity = await fetch_next_outgoing_activity(async_db_session)
    assert next_activity
    await process_next_outgoing_activity(async_db_session, next_activity)

    assert respx_mock.calls.call_count == 1

    outgoing_activity = (
        await async_db_session.execute(select(activitypub.models.OutgoingActivity))
    ).scalar_one()
    assert outgoing_activity.is_sent is True
    assert outgoing_activity.last_status_code == 204
    assert outgoing_activity.error is None
    assert outgoing_activity.is_errored is False


@pytest.mark.asyncio
async def test_process_next_outgoing_activity__webmention(
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    # And an outgoing activity
    outbox_object = _setup_outbox_object()

    recipient_url = "https://example.com/webmention"
    respx_mock.post(recipient_url).mock(return_value=httpx.Response(204))

    outgoing_activity = factories.OutgoingActivityFactory(
        recipient=recipient_url,
        outbox_object_id=outbox_object.id,
        inbox_object_id=None,
        webmention_target="http://example.com",
    )

    # When processing the next outgoing activity
    # Then it is processed
    next_activity = await fetch_next_outgoing_activity(async_db_session)
    assert next_activity
    await process_next_outgoing_activity(async_db_session, next_activity)

    assert respx_mock.calls.call_count == 1

    outgoing_activity = (
        await async_db_session.execute(select(activitypub.models.OutgoingActivity))
    ).scalar_one()
    assert outgoing_activity.is_sent is True
    assert outgoing_activity.last_status_code == 204
    assert outgoing_activity.error is None
    assert outgoing_activity.is_errored is False


@pytest.mark.asyncio
async def test_process_next_outgoing_activity__error_500(
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    outbox_object = _setup_outbox_object()
    recipient_inbox_url = "https://example.com/inbox"
    respx_mock.post(recipient_inbox_url).mock(
        return_value=httpx.Response(500, text="oops")
    )

    # And an outgoing activity
    outgoing_activity = factories.OutgoingActivityFactory(
        recipient=recipient_inbox_url,
        outbox_object_id=outbox_object.id,
        inbox_object_id=None,
        webmention_target=None,
    )

    # When processing the next outgoing activity
    # Then it is processed
    next_activity = await fetch_next_outgoing_activity(async_db_session)
    assert next_activity
    await process_next_outgoing_activity(async_db_session, next_activity)

    assert respx_mock.calls.call_count == 1

    outgoing_activity = (
        await async_db_session.execute(select(activitypub.models.OutgoingActivity))
    ).scalar_one()
    assert outgoing_activity.is_sent is False
    assert outgoing_activity.last_status_code == 500
    assert outgoing_activity.last_response == "oops"
    assert outgoing_activity.is_errored is False
    assert outgoing_activity.tries == 1
    # Regression test: `traceback.format_exc()` must be captured inside the
    # `except` block that actually raised, not after control has returned to
    # the apply phase (where it would silently be "NoneType: None").
    assert outgoing_activity.error is not None
    assert "Traceback" in outgoing_activity.error


@pytest.mark.asyncio
async def test_process_next_outgoing_activity__errored(
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    outbox_object = _setup_outbox_object()
    recipient_inbox_url = "https://example.com/inbox"
    respx_mock.post(recipient_inbox_url).mock(
        return_value=httpx.Response(500, text="oops")
    )

    # And an outgoing activity
    outgoing_activity = factories.OutgoingActivityFactory.create(
        recipient=recipient_inbox_url,
        outbox_object_id=outbox_object.id,
        inbox_object_id=None,
        webmention_target=None,
        tries=_MAX_RETRIES - 1,
    )

    # When processing the next outgoing activity
    # Then it is processed
    next_activity = await fetch_next_outgoing_activity(async_db_session)
    assert next_activity
    await process_next_outgoing_activity(async_db_session, next_activity)

    assert respx_mock.calls.call_count == 1

    outgoing_activity = (
        await async_db_session.execute(select(activitypub.models.OutgoingActivity))
    ).scalar_one()
    assert outgoing_activity.is_sent is False
    assert outgoing_activity.last_status_code == 500
    assert outgoing_activity.last_response == "oops"
    assert outgoing_activity.is_errored is True

    # And it is skipped from processing
    next_activity = await fetch_next_outgoing_activity(async_db_session)
    assert next_activity is None


@pytest.mark.asyncio
async def test_process_next_outgoing_activity__connect_error(
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    outbox_object = _setup_outbox_object()
    recipient_inbox_url = "https://example.com/inbox"
    respx_mock.post(recipient_inbox_url).mock(side_effect=httpx.ConnectError)

    # And an outgoing activity
    outgoing_activity = factories.OutgoingActivityFactory(
        recipient=recipient_inbox_url,
        outbox_object_id=outbox_object.id,
        inbox_object_id=None,
        webmention_target=None,
    )

    # When processing the next outgoing activity
    # Then it is processed
    next_activity = await fetch_next_outgoing_activity(async_db_session)
    assert next_activity
    await process_next_outgoing_activity(async_db_session, next_activity)

    assert respx_mock.calls.call_count == 1

    outgoing_activity = (
        await async_db_session.execute(select(activitypub.models.OutgoingActivity))
    ).scalar_one()
    assert outgoing_activity.is_sent is False
    assert outgoing_activity.error is not None
    assert outgoing_activity.tries == 1


@pytest.mark.asyncio
async def test_fetch_next_outgoing_activities__respects_limit_and_order(
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    outbox_object = _setup_outbox_object()

    oldest = factories.OutgoingActivityFactory(
        recipient="https://example.com/inbox1",
        outbox_object_id=outbox_object.id,
        inbox_object_id=None,
        webmention_target=None,
        next_try=now() - timedelta(seconds=30),
    )
    middle = factories.OutgoingActivityFactory(
        recipient="https://example.com/inbox2",
        outbox_object_id=outbox_object.id,
        inbox_object_id=None,
        webmention_target=None,
        next_try=now() - timedelta(seconds=20),
    )
    factories.OutgoingActivityFactory(
        recipient="https://example.com/inbox3",
        outbox_object_id=outbox_object.id,
        inbox_object_id=None,
        webmention_target=None,
        next_try=now() - timedelta(seconds=10),
    )

    activities = await fetch_next_outgoing_activities(async_db_session, limit=2)

    assert [a.id for a in activities] == [oldest.id, middle.id]


@pytest.mark.asyncio
async def test_process_outgoing_activities_batch__different_hosts_run_concurrently(
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    outbox_object = _setup_outbox_object()

    host_a_started = asyncio.Event()
    host_b_started = asyncio.Event()

    async def _host_a_side_effect(request: httpx.Request) -> httpx.Response:
        host_a_started.set()
        await asyncio.wait_for(host_b_started.wait(), timeout=5)
        return httpx.Response(204)

    async def _host_b_side_effect(request: httpx.Request) -> httpx.Response:
        host_b_started.set()
        await asyncio.wait_for(host_a_started.wait(), timeout=5)
        return httpx.Response(204)

    respx_mock.post("https://a.example.com/inbox").mock(side_effect=_host_a_side_effect)
    respx_mock.post("https://b.example.com/inbox").mock(side_effect=_host_b_side_effect)

    factories.OutgoingActivityFactory(
        recipient="https://a.example.com/inbox",
        outbox_object_id=outbox_object.id,
        inbox_object_id=None,
        webmention_target=None,
    )
    factories.OutgoingActivityFactory(
        recipient="https://b.example.com/inbox",
        outbox_object_id=outbox_object.id,
        inbox_object_id=None,
        webmention_target=None,
    )

    activities = await fetch_next_outgoing_activities(async_db_session, limit=10)
    # Deadlocks (and times out) if delivery is still serial: each side_effect
    # waits for the other host's request to have started.
    await asyncio.wait_for(
        process_outgoing_activities_batch(async_db_session, activities),
        timeout=5,
    )

    rows = (
        (await async_db_session.execute(select(activitypub.models.OutgoingActivity)))
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert all(row.is_sent for row in rows)


@pytest.mark.asyncio
async def test_process_outgoing_activities_batch__same_recipient_delivered_in_order(
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    outbox_object_1 = _setup_outbox_object(follow_id="follow-1")
    outbox_object_2 = _setup_outbox_object(follow_id="follow-2")
    recipient = "https://example.com/inbox"

    delivered_ids: list[str] = []

    def _record(request: httpx.Request) -> httpx.Response:
        delivered_ids.append(json.loads(request.content)["id"])
        return httpx.Response(204)

    respx_mock.post(recipient).mock(side_effect=_record)

    factories.OutgoingActivityFactory(
        recipient=recipient,
        outbox_object_id=outbox_object_1.id,
        inbox_object_id=None,
        webmention_target=None,
    )
    factories.OutgoingActivityFactory(
        recipient=recipient,
        outbox_object_id=outbox_object_2.id,
        inbox_object_id=None,
        webmention_target=None,
    )

    activities = await fetch_next_outgoing_activities(async_db_session, limit=10)
    await process_outgoing_activities_batch(async_db_session, activities)

    assert delivered_ids == [outbox_object_1.ap_id, outbox_object_2.ap_id]


@pytest.mark.asyncio
async def test_process_outgoing_activities_batch__429_short_circuits_same_recipient(
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    outbox_object_1 = _setup_outbox_object(follow_id="follow-1")
    outbox_object_2 = _setup_outbox_object(follow_id="follow-2")
    recipient = "https://example.com/inbox"

    respx_mock.post(recipient).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "120"})
    )

    factories.OutgoingActivityFactory(
        recipient=recipient,
        outbox_object_id=outbox_object_1.id,
        inbox_object_id=None,
        webmention_target=None,
    )
    factories.OutgoingActivityFactory(
        recipient=recipient,
        outbox_object_id=outbox_object_2.id,
        inbox_object_id=None,
        webmention_target=None,
    )

    activities = await fetch_next_outgoing_activities(async_db_session, limit=10)
    await process_outgoing_activities_batch(async_db_session, activities)

    assert respx_mock.calls.call_count == 1

    rows = (
        (await async_db_session.execute(select(activitypub.models.OutgoingActivity)))
        .scalars()
        .all()
    )
    assert len(rows) == 2
    for row in rows:
        assert row.tries == 1
        assert row.is_sent is False
        assert row.is_errored is False
        assert row.next_try is not None
        assert (
            abs((row.next_try - (now() + timedelta(seconds=120))).total_seconds()) < 5
        )


@pytest.mark.asyncio
async def test_process_outgoing_activities_batch__connect_error_short_circuits_same_recipient(  # noqa: E501
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    outbox_object_1 = _setup_outbox_object(follow_id="follow-1")
    outbox_object_2 = _setup_outbox_object(follow_id="follow-2")
    recipient = "https://example.com/inbox"

    respx_mock.post(recipient).mock(side_effect=httpx.ConnectError)

    factories.OutgoingActivityFactory(
        recipient=recipient,
        outbox_object_id=outbox_object_1.id,
        inbox_object_id=None,
        webmention_target=None,
    )
    factories.OutgoingActivityFactory(
        recipient=recipient,
        outbox_object_id=outbox_object_2.id,
        inbox_object_id=None,
        webmention_target=None,
    )

    activities = await fetch_next_outgoing_activities(async_db_session, limit=10)
    await process_outgoing_activities_batch(async_db_session, activities)

    assert respx_mock.calls.call_count == 1

    rows = (
        (await async_db_session.execute(select(activitypub.models.OutgoingActivity)))
        .scalars()
        .all()
    )
    assert len(rows) == 2
    for row in rows:
        assert row.tries == 1
        assert row.is_sent is False
        assert row.is_errored is False


@pytest.mark.asyncio
async def test_process_outgoing_activities_batch__failure_is_isolated(
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    outbox_object = _setup_outbox_object()

    respx_mock.post("https://a.example.com/inbox").mock(
        return_value=httpx.Response(500, text="oops")
    )
    respx_mock.post("https://b.example.com/inbox").mock(
        return_value=httpx.Response(204)
    )

    factories.OutgoingActivityFactory(
        recipient="https://a.example.com/inbox",
        outbox_object_id=outbox_object.id,
        inbox_object_id=None,
        webmention_target=None,
    )
    factories.OutgoingActivityFactory(
        recipient="https://b.example.com/inbox",
        outbox_object_id=outbox_object.id,
        inbox_object_id=None,
        webmention_target=None,
    )

    activities = await fetch_next_outgoing_activities(async_db_session, limit=10)
    await process_outgoing_activities_batch(async_db_session, activities)

    rows = {
        row.recipient: row
        for row in (
            await async_db_session.execute(select(activitypub.models.OutgoingActivity))
        )
        .scalars()
        .all()
    }
    assert rows["https://a.example.com/inbox"].is_sent is False
    assert rows["https://a.example.com/inbox"].tries == 1
    assert rows["https://b.example.com/inbox"].is_sent is True
    assert rows["https://b.example.com/inbox"].tries == 1


@pytest.mark.asyncio
async def test_process_outgoing_activities_batch__prepare_failure_is_recorded(
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    # Bypasses `new_outgoing_activity`'s guard: neither an outbox nor an
    # inbox object is referenced, so `anybox_object` raises in the prepare
    # phase.
    poison = factories.OutgoingActivityFactory(
        recipient="https://example.com/inbox",
        outbox_object_id=None,
        inbox_object_id=None,
        webmention_target=None,
    )

    activities = await fetch_next_outgoing_activities(async_db_session, limit=10)
    assert [a.id for a in activities] == [poison.id]

    # Must not raise -- a poison row must never crash the worker.
    await process_outgoing_activities_batch(async_db_session, activities)

    assert respx_mock.calls.call_count == 0

    row = (
        await async_db_session.execute(select(activitypub.models.OutgoingActivity))
    ).scalar_one()
    assert row.error is not None
    assert row.tries == 1
    assert row.is_sent is False
    assert row.next_try is not None


@pytest.mark.asyncio
async def test_queue_index_is_used(async_db_session: AsyncSession) -> None:
    """The only thing that would catch it if someone edited the poll
    predicate back into a form SQLite's partial-index matcher can't use
    (see the `.is_(False)` -> `IS 0` trap)."""
    query = (
        select(activitypub.models.OutgoingActivity)
        .where(
            activitypub.models.OutgoingActivity.next_try <= now(),
            activitypub.models.OutgoingActivity.is_errored.is_(False),
            activitypub.models.OutgoingActivity.is_sent.is_(False),
        )
        .limit(20)
        .order_by(
            activitypub.models.OutgoingActivity.next_try,
            activitypub.models.OutgoingActivity.id,
        )
    )
    compiled = query.compile(
        dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}
    )

    result = await async_db_session.execute(text(f"EXPLAIN QUERY PLAN {compiled}"))
    plan = "\n".join(str(row) for row in result.fetchall())
    assert "ix_outgoing_activity_queue" in plan
    assert "TEMP B-TREE" not in plan


# TODO(ts):
# - parse retry after
