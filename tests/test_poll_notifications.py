from datetime import timedelta

import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from activitypub import activitypub as ap
from activitypub import boxes
from activitypub.ap_object import RemoteObject
from activitypub.tests import factories
from app import models
from app import poll_notifications
from app.utils.datetime import now
from tests.utils import setup_remote_actor
from tests.utils import setup_remote_actor_as_follower


def _iso(dt) -> str:
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def _own_polls(db_session: AsyncSession) -> list[models.Notification]:
    return list(
        (
            await db_session.scalars(
                select(models.Notification).where(
                    models.Notification.notification_type
                    == models.NotificationType.POLL
                )
            )
        ).all()
    )


@pytest.mark.asyncio
async def test_own_ended_poll_produces_a_poll_notification_once(
    async_db_session: AsyncSession,
) -> None:
    _, poll = await boxes.send_create(
        async_db_session,
        "Question",
        "Pick one",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
        poll_type="oneOf",
        poll_answers=["A", "B"],
        poll_duration_in_minutes=60,
    )
    # Force it into the ended past -- `send_create` can only schedule an
    # endTime in the future.
    poll.ap_object = {**poll.ap_object, "endTime": _iso(now() - timedelta(minutes=1))}
    await async_db_session.commit()

    notified = await poll_notifications.notify_ended_polls(async_db_session)
    assert notified == 1

    notifs = await _own_polls(async_db_session)
    assert len(notifs) == 1
    assert notifs[0].actor_id is None
    assert notifs[0].outbox_object_id == poll.id

    # A second pass must not re-emit: the notification row is the watermark.
    notified_again = await poll_notifications.notify_ended_polls(async_db_session)
    assert notified_again == 0
    assert len(await _own_polls(async_db_session)) == 1


@pytest.mark.asyncio
async def test_own_poll_not_yet_ended_produces_no_notification(
    async_db_session: AsyncSession,
) -> None:
    await boxes.send_create(
        async_db_session,
        "Question",
        "Pick one",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
        poll_type="oneOf",
        poll_answers=["A", "B"],
        poll_duration_in_minutes=60,
    )

    notified = await poll_notifications.notify_ended_polls(async_db_session)
    assert notified == 0
    assert await _own_polls(async_db_session) == []


@pytest.mark.asyncio
async def test_voted_remote_poll_ended_produces_a_poll_notification(
    db: Session,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    question = factories.build_question_object(
        from_remote_actor=ra,
        options=["A", "B"],
        end_time=_iso(now() - timedelta(minutes=1)),
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        RemoteObject(question, ra), follower.actor
    )
    inbox_object.voted_for_answers = ["A"]
    db.commit()

    notified = await poll_notifications.notify_ended_polls(async_db_session)
    assert notified == 1

    notifs = await _own_polls(async_db_session)
    assert len(notifs) == 1
    assert notifs[0].actor_id == follower.actor.id
    assert notifs[0].inbox_object_id == inbox_object.id

    notified_again = await poll_notifications.notify_ended_polls(async_db_session)
    assert notified_again == 0
    assert len(await _own_polls(async_db_session)) == 1


@pytest.mark.asyncio
async def test_malformed_end_time_is_skipped_not_raised(
    db: Session,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    """`endTime` is remote-controlled: an unparseable one must not escape.

    It used to raise out of `notify_ended_polls` into the worker's
    `get_next_messages`, which aborts that pass *before* it fetches outgoing
    activities -- so a single bad poll row stalled federation delivery every
    pass, forever, since the row stayed a candidate.
    """
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    question = factories.build_question_object(from_remote_actor=ra, options=["A", "B"])
    question["endTime"] = "not-a-date"
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        RemoteObject(question, ra), follower.actor
    )
    inbox_object.voted_for_answers = ["A"]
    db.commit()

    # Must not raise, and must not invent a notification for it.
    assert await poll_notifications.notify_ended_polls(async_db_session) == 0
    assert await _own_polls(async_db_session) == []


@pytest.mark.asyncio
async def test_never_ending_polls_do_not_starve_an_ended_one(
    db: Session,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    """A poll that can't end must not consume the emit budget.

    `endTime` is optional in AP, so a Question can stay un-notified forever.
    With the batch limit applied to the *candidate* query, BATCH_SIZE such
    rows sat at the front of the window permanently and every ended poll
    behind them was never notified.
    """
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    for i in range(poll_notifications.BATCH_SIZE):
        endless = factories.build_question_object(
            from_remote_actor=ra, outbox_public_id=f"endless{i}", options=["A", "B"]
        )
        del endless["endTime"]
        obj = factories.InboxObjectFactory.from_remote_object(
            RemoteObject(endless, ra), follower.actor
        )
        obj.voted_for_answers = ["A"]

    # Created last, so it sorts behind all of them by id.
    ended = factories.build_question_object(
        from_remote_actor=ra,
        outbox_public_id="ended",
        options=["A", "B"],
        end_time=_iso(now() - timedelta(minutes=1)),
    )
    ended_object = factories.InboxObjectFactory.from_remote_object(
        RemoteObject(ended, ra), follower.actor
    )
    ended_object.voted_for_answers = ["A"]
    db.commit()

    assert await poll_notifications.notify_ended_polls(async_db_session) == 1
    notifs = await _own_polls(async_db_session)
    assert [n.inbox_object_id for n in notifs] == [ended_object.id]


@pytest.mark.asyncio
async def test_worker_isolates_a_failing_poll_pass_from_delivery(
    async_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever goes wrong in the sweep, the worker's own job must proceed."""
    from activitypub.outgoing_activities import OutgoingActivityWorker

    async def _boom(*args, **kwargs):
        raise RuntimeError("poll sweep exploded")

    monkeypatch.setattr(
        "app.poll_notifications.notify_ended_polls", _boom, raising=True
    )

    worker = OutgoingActivityWorker()
    # Does not raise: `get_next_messages` still reaches the delivery fetch.
    assert await worker.get_next_messages(async_db_session, 1) == []


@pytest.mark.asyncio
async def test_worker_rate_limits_the_poll_notifications_pass(
    async_db_session: AsyncSession,
) -> None:
    from activitypub.outgoing_activities import OutgoingActivityWorker

    worker = OutgoingActivityWorker()

    _, first_poll = await boxes.send_create(
        async_db_session,
        "Question",
        "Pick one",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
        poll_type="oneOf",
        poll_answers=["A", "B"],
        poll_duration_in_minutes=60,
    )
    first_poll.ap_object = {
        **first_poll.ap_object,
        "endTime": _iso(now() - timedelta(minutes=1)),
    }
    await async_db_session.commit()

    await worker._notify_ended_polls(async_db_session)
    assert len(await _own_polls(async_db_session)) == 1

    _, second_poll = await boxes.send_create(
        async_db_session,
        "Question",
        "Pick one",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
        poll_type="oneOf",
        poll_answers=["A", "B"],
        poll_duration_in_minutes=60,
    )
    second_poll.ap_object = {
        **second_poll.ap_object,
        "endTime": _iso(now() - timedelta(minutes=1)),
    }
    await async_db_session.commit()

    # Due, but the interval hasn't elapsed: skipped without touching the DB.
    await worker._notify_ended_polls(async_db_session)
    assert len(await _own_polls(async_db_session)) == 1

    worker.poll_notifications_interval = 0.0
    await worker._notify_ended_polls(async_db_session)
    assert len(await _own_polls(async_db_session)) == 2
