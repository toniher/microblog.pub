"""Unit tests for the streaming event pump's primitives (`EventPump.seed`/
`tick`), driven directly against `async_db_session` — no WebSocket involved.

Mirrors `tests/test_push_worker.py`'s approach of calling a worker's
primitives directly rather than running its loop: this covers all the
filtering/cursor logic with zero hang risk, since nothing here touches
`WebSocketTestSession.receive()` (see `tests/mastodon/test_streaming.py` for
why that matters).
"""

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

import activitypub.models
from activitypub import activitypub as ap
from activitypub.ap_object import ObjectType
from activitypub.ap_object import RemoteObject
from activitypub.boxes import send_create
from activitypub.tests import factories
from app import models
from app.mastodon import ids
from app.mastodon import streaming as streaming_module
from app.mastodon.streaming import _MAX_STREAMS_PER_SUBSCRIBER
from app.mastodon.streaming import EventPump
from app.mastodon.streaming import Hub
from app.mastodon.streaming import Subscriber
from tests.mastodon.test_conformance import _validate_status
from tests.utils import setup_inbox_note
from tests.utils import setup_remote_actor
from tests.utils import setup_remote_actor_as_follower


def _pump() -> EventPump:
    return Hub()._pump


def _pump_with_subscribed_streams(*streams: tuple) -> EventPump:
    """A pump whose hub has one subscriber watching `streams`.

    Needed by every test that expects an event at all: `direct`/`hashtag`
    handling is gated on `hub.active_stream_kinds()`/`active_hashtags()`, and
    the delete/edit re-poll is gated on `hub.all_subscribed_stream_keys()`, so
    a pump with no subscribers deliberately skips that work entirely.
    """
    hub = Hub()
    sub = Subscriber(token="test-token")
    sub.streams.update(streams)
    hub._subscribers.add(sub)
    return hub._pump


async def _make_notification(
    db_session: AsyncSession,
    actor_id: int | None,
    notification_type: models.NotificationType = models.NotificationType.NEW_FOLLOWER,
) -> models.Notification:
    notif = models.Notification(notification_type=notification_type, actor_id=actor_id)
    db_session.add(notif)
    await db_session.commit()
    return notif


@pytest.mark.asyncio
async def test_seed_produces_no_backlog_for_preexisting_row(
    async_db_session: AsyncSession,
) -> None:
    await send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "before connect",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    pump = _pump()
    await pump.seed(async_db_session)
    events, behind = await pump.tick(async_db_session)

    assert events == []
    assert behind is False


@pytest.mark.asyncio
async def test_new_public_outbox_post_emits_update_on_expected_streams(
    async_db_session: AsyncSession,
) -> None:
    pump = _pump()
    await pump.seed(async_db_session)

    _, obj = await send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "hello",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    events, _ = await pump.tick(async_db_session)

    assert len(events) == 1
    event = events[0]
    assert event.event == "update"
    assert event.streams == frozenset(
        {("user", None), ("public", None), ("public:local", None)}
    )

    payload = json.loads(event.payload)
    assert payload["id"] == ids.encode_outbox_id(obj)
    assert _validate_status(payload, "streaming_update") == []


@pytest.mark.asyncio
async def test_new_public_inbox_post_hits_remote_stream(
    async_db_session: AsyncSession,
    respx_mock,
) -> None:
    pump = _pump()
    await pump.seed(async_db_session)

    ra = setup_remote_actor(respx_mock)
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    setup_inbox_note(follower.actor, content="from a follower")

    events, _ = await pump.tick(async_db_session)

    assert len(events) == 1
    assert events[0].streams == frozenset(
        {("user", None), ("public", None), ("public:remote", None)}
    )


@pytest.mark.asyncio
async def test_muted_actor_produces_no_event(
    async_db_session: AsyncSession,
    db: Session,
    respx_mock,
) -> None:
    """The re-query-through-REST-fetchers design means a mute takes effect on
    the stream exactly when it takes effect on the REST timeline — this is
    the test that actually proves that reuse, not just asserts it.
    """
    pump = _pump()
    await pump.seed(async_db_session)

    ra = setup_remote_actor(respx_mock)
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    # follower.actor was created through the sync `_Session` factories, so the
    # mutation must go through the matching sync `db` session — committing it
    # via async_db_session wouldn't touch the same identity map.
    follower.actor.is_muted = True
    db.commit()

    setup_inbox_note(follower.actor, content="from a muted follower")

    events, _ = await pump.tick(async_db_session)

    assert events == []


@pytest.mark.asyncio
async def test_reblogs_hidden_actor_boost_produces_no_event(
    async_db_session: AsyncSession,
    db: Session,
    respx_mock,
) -> None:
    """Same reuse as `test_muted_actor_produces_no_event`, for the
    `reblogs=false` boost filter added to `fetch_inbox_timeline_page`.
    """
    pump = _pump()
    await pump.seed(async_db_session)

    ra = setup_remote_actor(respx_mock)
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    follower.actor.are_announces_hidden_from_stream = True
    db.commit()

    original_note = RemoteObject(
        factories.build_note_object(from_remote_actor=ra, content="Original"),
        ra,
    )
    # Mirrors `_handle_announce_activity`'s unknown-object branch: the
    # announced object itself is cached hidden, only the Announce is visible.
    original_inbox_object = factories.InboxObjectFactory.from_remote_object(
        original_note, follower.actor
    )
    original_inbox_object.is_hidden_from_stream = True
    db.commit()
    boost = RemoteObject(
        {
            "@context": ap.AS_CTX,
            "type": "Announce",
            "id": f"{ra.ap_id}/announce/reblogs_hidden",
            "actor": ra.ap_id,
            "object": original_note.ap_id,
            "to": [ap.AS_PUBLIC],
            "cc": [],
            "published": original_note.ap_object["published"],
            "url": f"{ra.ap_id}/announce/reblogs_hidden",
        },
        ra,
    )
    factories.InboxObjectFactory.from_remote_object(
        boost, follower.actor, relates_to_inbox_object_id=original_inbox_object.id
    )

    events, _ = await pump.tick(async_db_session)

    assert events == []


@pytest.mark.asyncio
async def test_unlisted_post_is_not_on_public_streams(
    async_db_session: AsyncSession,
) -> None:
    pump = _pump()
    await pump.seed(async_db_session)

    await send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "shh",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.UNLISTED,
    )

    events, _ = await pump.tick(async_db_session)

    assert len(events) == 1
    assert events[0].streams == frozenset({("user", None)})


@pytest.mark.asyncio
async def test_notification_emits_on_user_and_notification_streams(
    async_db_session: AsyncSession,
    respx_mock,
) -> None:
    pump = _pump()
    await pump.seed(async_db_session)

    ra = setup_remote_actor(respx_mock)
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    notif = await _make_notification(async_db_session, follower.actor.id)

    events, _ = await pump.tick(async_db_session)

    assert len(events) == 1
    assert events[0].event == "notification"
    assert events[0].streams == frozenset({("user:notification", None), ("user", None)})
    payload = json.loads(events[0].payload)
    assert payload["id"] == str(notif.id)
    assert payload["type"] == "follow"
    # Real grouping (app.mastodon.notification_groups), not the old
    # ungrouped-{id} shape -- streaming reuses serialize_notification
    # verbatim, so this should just follow automatically.
    assert payload["group_key"] == f"follow-{notif.created_at:%Y%m%d}"


@pytest.mark.asyncio
async def test_muted_actor_notification_produces_no_event(
    async_db_session: AsyncSession,
    db: Session,
    respx_mock,
) -> None:
    pump = _pump()
    await pump.seed(async_db_session)

    ra = setup_remote_actor(respx_mock)
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    # See test_muted_actor_produces_no_event: mutate/commit via the matching
    # sync session, not async_db_session.
    follower.actor.is_muted = True
    follower.actor.are_notifications_muted = True
    db.commit()

    await _make_notification(async_db_session, follower.actor.id)

    events, _ = await pump.tick(async_db_session)

    assert events == []


@pytest.mark.asyncio
async def test_notification_never_marks_is_new_false(
    async_db_session: AsyncSession,
    respx_mock,
) -> None:
    pump = _pump()
    await pump.seed(async_db_session)

    ra = setup_remote_actor(respx_mock)
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    notif = await _make_notification(async_db_session, follower.actor.id)

    await pump.tick(async_db_session)

    refreshed = await async_db_session.get(models.Notification, notif.id)
    assert refreshed is not None
    assert refreshed.is_new is True


@pytest.mark.asyncio
async def test_delete_emits_bare_status_id_payload(
    async_db_session: AsyncSession,
) -> None:
    _, obj = await send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "will be deleted",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    expected_status_id = ids.encode_outbox_id(obj)

    pump = _pump_with_subscribed_streams(("user", None))
    await pump.seed(async_db_session)
    # The post already existed at seed time, so it must be in the tracked set.
    assert obj.id in pump._tracked_outbox

    obj.is_deleted = True
    await async_db_session.commit()

    events, _ = await pump.tick(async_db_session)

    assert len(events) == 1
    assert events[0].event == "delete"
    assert events[0].payload == expected_status_id
    assert obj.id not in pump._tracked_outbox


@pytest.mark.asyncio
async def test_status_update_on_edit_via_revisions(
    async_db_session: AsyncSession,
) -> None:
    _, obj = await send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "original",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    pump = _pump_with_subscribed_streams(("user", None))
    await pump.seed(async_db_session)

    obj.revisions = [{"content": "original"}]
    await async_db_session.commit()

    events, _ = await pump.tick(async_db_session)

    assert len(events) == 1
    assert events[0].event == "status.update"
    payload = json.loads(events[0].payload)
    assert payload["id"] == ids.encode_outbox_id(obj)


@pytest.mark.asyncio
async def test_hard_deleted_tracked_row_produces_no_event(
    async_db_session: AsyncSession,
    db: Session,
) -> None:
    _, obj = await send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "will be pruned",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    obj_id = obj.id

    pump = _pump_with_subscribed_streams(("user", None))
    await pump.seed(async_db_session)
    assert obj_id in pump._tracked_outbox

    db.execute(
        activitypub.models.OutboxObject.__table__.delete().where(
            activitypub.models.OutboxObject.id == obj_id
        )
    )
    db.commit()

    events, _ = await pump.tick(async_db_session)

    assert events == []
    assert obj_id not in pump._tracked_outbox


@pytest.mark.asyncio
async def test_two_idle_ticks_produce_no_events(async_db_session: AsyncSession) -> None:
    pump = _pump()
    await pump.seed(async_db_session)

    events1, _ = await pump.tick(async_db_session)
    events2, _ = await pump.tick(async_db_session)

    assert events1 == []
    assert events2 == []


@pytest.mark.asyncio
async def test_cursors_never_regress(async_db_session: AsyncSession) -> None:
    pump = _pump()
    await pump.seed(async_db_session)

    await send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "one",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    await pump.tick(async_db_session)
    cursor_after_first = pump._outbox_cursor

    await pump.tick(async_db_session)
    assert pump._outbox_cursor == cursor_after_first

    await send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "two",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    await pump.tick(async_db_session)
    assert pump._outbox_cursor > cursor_after_first


@pytest.mark.asyncio
async def test_tracked_map_is_capped_keeping_the_newest(
    async_db_session: AsyncSession,
) -> None:
    pump = _pump()
    await pump.seed(async_db_session)

    original_limit = streaming_module._TRACK_LIMIT
    streaming_module._TRACK_LIMIT = 5
    try:
        last_id = None
        for i in range(10):
            _, obj = await send_create(
                async_db_session,
                ObjectType.NOTE.value,
                f"post {i}",
                uploads=[],
                in_reply_to=None,
                visibility=ap.VisibilityEnum.PUBLIC,
            )
            await pump.tick(async_db_session)
            last_id = obj.id

        assert len(pump._tracked_outbox) <= 5
        assert last_id in pump._tracked_outbox
    finally:
        streaming_module._TRACK_LIMIT = original_limit


@pytest.mark.asyncio
async def test_direct_note_emits_conversation_not_update_on_public_streams(
    async_db_session: AsyncSession,
) -> None:
    pump = _pump_with_subscribed_streams(("direct", None), ("user", None))
    await pump.seed(async_db_session)

    _, obj = await send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "just between us",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.DIRECT,
    )

    events, _ = await pump.tick(async_db_session)

    by_event = {e.event: e for e in events}
    assert by_event["update"].streams == frozenset({("user", None)})
    assert by_event["conversation"].streams == frozenset({("direct", None)})
    conversation_payload = json.loads(by_event["conversation"].payload)
    assert conversation_payload["id"] == ids.encode_outbox_id(obj)


@pytest.mark.asyncio
async def test_direct_conversation_skipped_when_nobody_subscribes(
    async_db_session: AsyncSession,
) -> None:
    pump = _pump()  # no subscriber at all
    await pump.seed(async_db_session)

    await send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "just between us",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.DIRECT,
    )

    events, _ = await pump.tick(async_db_session)

    assert [e.event for e in events] == ["update"]


@pytest.mark.asyncio
async def test_hashtag_stream_matches_only_when_subscribed(
    async_db_session: AsyncSession,
) -> None:
    pump = _pump_with_subscribed_streams(("hashtag", "cats"))
    await pump.seed(async_db_session)

    await send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "look at my #cats",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    events, _ = await pump.tick(async_db_session)

    assert len(events) == 1
    assert ("hashtag", "cats") in events[0].streams


@pytest.mark.asyncio
async def test_hashtag_not_matched_without_a_subscriber(
    async_db_session: AsyncSession,
) -> None:
    pump = _pump()  # nobody subscribes to any hashtag

    await pump.seed(async_db_session)

    await send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "look at my #cats",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    events, _ = await pump.tick(async_db_session)

    assert len(events) == 1
    assert not any(k == "hashtag" for k, _ in events[0].streams)


@pytest.mark.asyncio
async def test_hashtag_matching_is_independent_of_subscribed_tag_count(
    async_db_session: AsyncSession,
) -> None:
    """`_classify_streams` intersects the status's own tags with the active
    ones, so a client subscribing to many hashtags cannot make each arriving
    status cost O(subscribed tags) inside the pump.
    """
    many = [("hashtag", f"tag{i}") for i in range(_MAX_STREAMS_PER_SUBSCRIBER)]
    pump = _pump_with_subscribed_streams(("user", None), ("hashtag", "cats"), *many)
    await pump.seed(async_db_session)

    await send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "look at my #cats",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    events, _ = await pump.tick(async_db_session)

    assert len(events) == 1
    hashtag_keys = {k for k in events[0].streams if k[0] == "hashtag"}
    assert hashtag_keys == {("hashtag", "cats")}


@pytest.mark.asyncio
async def test_delete_is_not_polled_when_nobody_subscribes(
    async_db_session: AsyncSession,
) -> None:
    """The tracked-row re-poll is the pump's whole idle cost, so it is skipped
    while no stream is subscribed — every event it could produce would be
    dropped by `Hub.publish` anyway.
    """
    _, obj = await send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "will be deleted",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    pump = _pump()  # a connected socket that subscribed to nothing
    await pump.seed(async_db_session)
    assert obj.id is not None
    assert obj.id in pump._tracked_outbox

    obj.is_deleted = True
    await async_db_session.commit()

    events, _ = await pump.tick(async_db_session)

    assert events == []
    # Still tracked, with its pre-delete baseline: the delete is not lost, it
    # fires on the first tick after something subscribes.
    assert pump._tracked_outbox[obj.id].is_deleted is False


@pytest.mark.asyncio
async def test_delete_during_the_unsubscribed_gap_fires_on_resume(
    async_db_session: AsyncSession,
) -> None:
    _, obj = await send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "will be deleted",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    expected_status_id = ids.encode_outbox_id(obj)

    hub = Hub()
    sub = Subscriber(token="test-token")
    hub._subscribers.add(sub)
    pump = hub._pump
    await pump.seed(async_db_session)

    obj.is_deleted = True
    await async_db_session.commit()

    assert await pump.tick(async_db_session) == ([], False)

    sub.streams.add(("user", None))
    events, _ = await pump.tick(async_db_session)

    assert len(events) == 1
    assert events[0].event == "delete"
    assert events[0].payload == expected_status_id


@pytest.mark.asyncio
async def test_pump_restart_gets_its_own_stop_event(
    async_db_session: AsyncSession,
) -> None:
    """The stop signal belongs to the task, not to the pump.

    Reading it off the pump would let a `register()` that races the shutdown
    wait in `unregister()` swap the event out from under the outgoing task —
    which would then never see its own stop and would poll alongside its
    replacement, while the *replacement* got stopped instead.
    """
    hub = Hub()
    first = Subscriber(token="a")
    await hub.register(first)
    first_task, first_event = hub._task, hub._stop_event
    assert first_task is not None
    assert first_event is not None

    await hub.unregister(first)
    assert first_event.is_set()
    assert first_task.done()
    assert hub._task is None

    second = Subscriber(token="b")
    await hub.register(second)
    try:
        assert hub._task is not None and hub._task is not first_task
        assert hub._stop_event is not None and hub._stop_event is not first_event
        # Re-setting the previous shutdown's event must not reach the new task.
        first_event.set()
        assert not hub._stop_event.is_set()
        assert not hub._task.done()
    finally:
        await hub.aclose()
