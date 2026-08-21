"""`poll`-ended notifications.

Rides along on `OutgoingActivityWorker`'s poll, exactly like
`app.scheduled_statuses` publishing due posts: notifying about an ended poll
isn't outbox work, but this worker already runs in every deployment (nothing
federates without it), so an upgraded install can't silently miss a new
supervisord entry the way it could with a process of its own.

The `Notification` row itself is the watermark: once one exists for a given
outbox/inbox Question, `notify_ended_polls` never emits a second one for it —
there's no separate "already notified" column to keep in sync or backfill.
`Object.is_poll_ended` (`activitypub/ap_object.py`) stays the single source
of truth for closed-ness; nothing about the poll's state is persisted here.
"""

from loguru import logger
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import text

import activitypub.models
from activitypub.boxes import is_notification_enabled
from app import models
from app.database import AsyncSession
from app.utils.datetime import now
from app.utils.datetime import parse_isoformat

# How many notifications one pass emits. Deliberately *not* also the limit on
# the candidate query: the "has it ended?" test can only be made in Python
# (`endTime` arrives in any ISO-8601 offset, so a lexical SQL comparison would
# be wrong for a non-UTC one), and a `LIMIT` on the candidates would let rows
# that never pass that test sit at the front of the window forever, starving
# every ended poll behind them -- see
# `test_never_ending_polls_do_not_starve_an_ended_one`.
BATCH_SIZE = 5

# `endTime` lives only inside the JSON, so the candidate queries extract it to
# drop the polls that can never end (the field is optional -- see
# `Object.poll_end_time`). Without this they would stay candidates forever,
# re-scanned on every pass. Written as a literal path per the `in_reply_to_expr`
# convention in `activitypub/models.py`; there is no index to defeat here, but
# keeping the form consistent means it stays indexable if that ever changes.
_END_TIME_JSON_PATH = "'$.endTime'"


def _end_time_expr(ap_object_column):
    return func.json_extract(ap_object_column, text(_END_TIME_JSON_PATH))


async def _candidate_own_polls(db_session: AsyncSession) -> list[tuple[int, dict]]:
    already_notified = (
        select(models.Notification.id)
        .where(
            models.Notification.notification_type == models.NotificationType.POLL,
            models.Notification.outbox_object_id == activitypub.models.OutboxObject.id,
        )
        .exists()
    )
    rows = (
        await db_session.execute(
            select(
                activitypub.models.OutboxObject.id,
                activitypub.models.OutboxObject.ap_object,
            )
            .where(
                activitypub.models.OutboxObject.ap_type == "Question",
                activitypub.models.OutboxObject.is_deleted.is_(False),
                _end_time_expr(activitypub.models.OutboxObject.ap_object).is_not(None),
                ~already_notified,
            )
            .order_by(activitypub.models.OutboxObject.id)
        )
    ).all()
    return [(row[0], row[1]) for row in rows]


async def _candidate_voted_polls(
    db_session: AsyncSession,
) -> list[tuple[int, int, dict]]:
    already_notified = (
        select(models.Notification.id)
        .where(
            models.Notification.notification_type == models.NotificationType.POLL,
            models.Notification.inbox_object_id == activitypub.models.InboxObject.id,
        )
        .exists()
    )
    rows = (
        await db_session.execute(
            select(
                activitypub.models.InboxObject.id,
                activitypub.models.InboxObject.actor_id,
                activitypub.models.InboxObject.ap_object,
            )
            .where(
                activitypub.models.InboxObject.ap_type == "Question",
                activitypub.models.InboxObject.voted_for_answers.is_not(None),
                _end_time_expr(activitypub.models.InboxObject.ap_object).is_not(None),
                ~already_notified,
            )
            .order_by(activitypub.models.InboxObject.id)
        )
    ).all()
    return [(row[0], row[1], row[2]) for row in rows]


def _is_ended(ap_object: dict) -> bool:
    """Whether the poll's `endTime` has passed.

    `endTime` is remote-controlled and only ever validated by being parsed, so
    a malformed value must not escape: raising here would abort the worker
    pass this sweep rides on (and take that pass's federation delivery with
    it), every pass, forever -- the row stays a candidate, so it would recur.
    """
    end_time = ap_object.get("endTime")
    if not end_time:
        return False
    try:
        return now() > parse_isoformat(end_time)
    except (ValueError, OverflowError, TypeError):
        # Debug, not warning: nothing an operator can act on, and this is
        # re-evaluated on every pass for as long as the row exists.
        logger.debug(f"Ignoring poll with unparseable endTime {end_time!r}")
        return False


async def notify_ended_polls(db_session: AsyncSession, limit: int = BATCH_SIZE) -> int:
    """Emit a `poll` notification for every ended poll that doesn't have one
    yet, up to `limit`; returns how many were emitted.

    `limit` caps the notifications *written*, not the candidates examined: a
    poll that hasn't ended is skipped without consuming any of the budget, so
    it can never keep an ended one from being noticed. Anything over the
    budget is picked up on the next pass, seconds later.
    """
    if not is_notification_enabled(models.NotificationType.POLL):
        return 0

    notified = 0

    for outbox_object_id, ap_object in await _candidate_own_polls(db_session):
        if notified >= limit:
            break
        if not _is_ended(ap_object):
            continue
        db_session.add(
            models.Notification(
                notification_type=models.NotificationType.POLL,
                actor_id=None,
                outbox_object_id=outbox_object_id,
            )
        )
        notified += 1

    for inbox_object_id, actor_id, ap_object in await _candidate_voted_polls(
        db_session
    ):
        if notified >= limit:
            break
        if not _is_ended(ap_object):
            continue
        db_session.add(
            models.Notification(
                notification_type=models.NotificationType.POLL,
                actor_id=actor_id,
                inbox_object_id=inbox_object_id,
            )
        )
        notified += 1

    if notified:
        await db_session.commit()

    return notified
