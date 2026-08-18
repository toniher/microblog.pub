"""Scheduled statuses (`POST /api/v1/statuses` with `scheduled_at`).

Two halves:

- `ComposeParams`, the validated compose parameters of a status write. The
  Mastodon router turns a request into one of these and then either publishes
  it immediately or stores it here as JSON — so the immediate and the deferred
  path can never drift apart, they call the same `publish()`.
- `publish_due_scheduled_statuses()`, the pass that publishes rows whose time
  has come. It runs inside the existing outgoing-activity worker rather than a
  process of its own: publishing *is* outbox work, and that worker is running
  in every deployment already (without it nothing federates at all), so a
  scheduled post can't silently never appear because an upgraded install
  missed a new supervisord entry.
"""

import dataclasses
from datetime import datetime
from datetime import timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select

import activitypub.models
from activitypub import activitypub as ap
from app import models
from app.database import AsyncSession
from app.utils.datetime import now

# A publish attempt hits the network (`markdownify` resolves mentions against
# remote actors) and re-reads rows the client may have deleted since, so a due
# row can fail for reasons that later fix themselves. Same retry shape as
# `PushSubscription`/`OutgoingActivity` — back off, then stop rather than
# retrying forever. `PUT /api/v1/scheduled_statuses/{id}` resets the state, so
# a given-up row is recoverable by rescheduling it.
MAX_TRIES = 5
RETRY_BASE_DELAY = timedelta(minutes=5)

# How many due rows a single worker pass publishes. Each one sends a Create to
# every recipient's inbox, so this stays small enough not to starve the
# delivery queue the same worker is meant to be draining.
BATCH_SIZE = 5


class ScheduledStatusError(Exception):
    """A scheduled status that cannot be published as stored."""


@dataclasses.dataclass(frozen=True)
class ComposeParams:
    """The validated compose parameters of a status write.

    `media_ids` and `in_reply_to_id` stay *Mastodon ids* rather than resolved
    rows: they're what the entity echoes back in `params`, and re-resolving
    them at publish time means an upload or a parent status deleted in the
    meantime surfaces as a publish failure instead of a dangling reference.

    `visibility` is the ActivityPub enum, not the Mastodon name — the request
    is validated once, at parse time (`app.mastodon.serializers`' inverse map
    turns it back into the Mastodon name for the entity).
    """

    content: str
    content_warning: str | None = None
    sensitive: bool = False
    visibility: ap.VisibilityEnum = ap.VisibilityEnum.PUBLIC
    language: str | None = None
    in_reply_to_id: str | None = None
    media_ids: list[str] = dataclasses.field(default_factory=list)
    poll_options: list[str] = dataclasses.field(default_factory=list)
    poll_multiple: bool = False
    poll_expires_in: int | None = None
    idempotency: str | None = None

    def to_json(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["visibility"] = self.visibility.value
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ComposeParams":
        """Rebuild from a stored blob.

        Unknown keys are dropped and missing ones fall back to their defaults:
        a row written by an older version of this code must stay publishable
        after an upgrade that adds or renames a compose parameter.
        """
        known = {f.name for f in dataclasses.fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}
        if "visibility" in kwargs:
            kwargs["visibility"] = ap.VisibilityEnum(kwargs["visibility"])
        return cls(**kwargs)


async def publish(
    db_session: AsyncSession,
    params: ComposeParams,
    delete_on_send: models.ScheduledStatus | None = None,
) -> activitypub.models.OutboxObject:
    """Send `params` as a new status now, returning the created outbox object.

    `delete_on_send` (the queue row this came from, when it came from one) is
    removed inside the transaction `send_create` commits, which is what makes
    publishing a queued post exactly-once — there's no window where the status
    exists and the queue row still does, so a crash can't republish it on the
    next pass. It's staged only after the uploads and the reply parent have been
    resolved, keeping those reads out of the write transaction: SQLite
    serializes writers, so the transaction wants to be as short as it can be.
    (It still spans `send_create`'s own network calls — recipient resolution and
    webmention discovery — exactly as it does for an interactive post.)

    Raises `ScheduledStatusError` if a referenced upload or reply parent no
    longer exists.
    """
    # Imported lazily, not at module level: `activitypub.boxes` imports
    # `activitypub.outgoing_activities`, whose worker calls back into this
    # module — and that worker is a process entry point, so a module-level
    # import here would make the cycle unresolvable at startup.
    from activitypub.boxes import send_create
    from app.mastodon import ids
    from app.mastodon.serializers import synthetic_filename

    uploads = []
    for media_id in params.media_ids:
        upload = await ids.get_upload_by_mastodon_id(db_session, media_id)
        if upload is None:
            raise ScheduledStatusError(f"unknown media id {media_id}")
        uploads.append((upload, synthetic_filename(upload), upload.description))

    in_reply_to = None
    if params.in_reply_to_id:
        parent = await ids.get_object_by_mastodon_id(db_session, params.in_reply_to_id)
        if parent is None:
            raise ScheduledStatusError(
                f"unknown in_reply_to_id {params.in_reply_to_id}"
            )
        in_reply_to = parent.ap_id

    ap_type = "Note"
    poll_type = None
    poll_answers = None
    poll_duration_in_minutes = None
    if params.poll_options:
        ap_type = "Question"
        poll_answers = params.poll_options
        poll_type = "anyOf" if params.poll_multiple else "oneOf"
        # send_create takes whole minutes; never round a short-lived poll down
        # to 0 (which would mean "no expiration" / immediately expired).
        poll_duration_in_minutes = max(1, (params.poll_expires_in or 3600) // 60)

    if delete_on_send is not None:
        await db_session.delete(delete_on_send)

    _, outbox_object = await send_create(
        db_session,
        ap_type=ap_type,
        source=params.content,
        uploads=uploads,
        in_reply_to=in_reply_to,
        visibility=params.visibility,
        content_warning=params.content_warning,
        is_sensitive=True if params.content_warning else params.sensitive,
        poll_type=poll_type,
        poll_answers=poll_answers,
        poll_duration_in_minutes=poll_duration_in_minutes,
        name=None,
        language=params.language,
    )
    return outbox_object


async def schedule(
    db_session: AsyncSession,
    params: ComposeParams,
    scheduled_at: datetime,
) -> models.ScheduledStatus:
    scheduled_status = models.ScheduledStatus(
        scheduled_at=scheduled_at,
        params=params.to_json(),
        next_try=scheduled_at,
    )
    db_session.add(scheduled_status)
    await db_session.commit()
    return scheduled_status


def reschedule(
    scheduled_status: models.ScheduledStatus,
    scheduled_at: datetime,
) -> None:
    """Move a row's publication time, clearing any past failure.

    Callers commit. Resetting the retry state is what makes a row that gave up
    (NULL `next_try`) publishable again.
    """
    scheduled_status.scheduled_at = scheduled_at
    scheduled_status.next_try = scheduled_at
    scheduled_status.tries = 0
    scheduled_status.last_error = None


async def fetch_due_scheduled_statuses(
    db_session: AsyncSession,
    limit: int = BATCH_SIZE,
) -> list[models.ScheduledStatus]:
    return list(
        (
            await db_session.scalars(
                select(models.ScheduledStatus)
                .where(
                    models.ScheduledStatus.next_try.is_not(None),
                    models.ScheduledStatus.next_try <= now(),
                )
                # Ordered by the same column the filter uses, so SQLite reads
                # the due rows straight off `ix_scheduled_status_next_try` with
                # no sort step. For a row that never failed this is its
                # `scheduled_at`; for one that did, it's its next attempt —
                # which is the order to publish in anyway.
                .order_by(models.ScheduledStatus.next_try)
                .limit(limit)
            )
        ).all()
    )


async def _record_failure(
    db_session: AsyncSession,
    scheduled_status_id: int,
    error: Exception,
) -> None:
    # Re-fetched rather than reused: the caller rolled back the failed attempt,
    # which restored (and expired) the row this bookkeeping belongs on.
    scheduled_status = await db_session.get(models.ScheduledStatus, scheduled_status_id)
    if scheduled_status is None:
        return

    scheduled_status.tries = (scheduled_status.tries or 0) + 1
    scheduled_status.last_error = str(error)[:500]
    if scheduled_status.tries >= MAX_TRIES:
        scheduled_status.next_try = None
        logger.error(
            f"Giving up on scheduled status {scheduled_status_id} after "
            f"{scheduled_status.tries} tries: {scheduled_status.last_error}"
        )
    else:
        scheduled_status.next_try = now() + RETRY_BASE_DELAY * (
            2 ** (scheduled_status.tries - 1)
        )
        logger.warning(
            f"Scheduled status {scheduled_status_id} failed to publish "
            f"({scheduled_status.last_error}), next try at "
            f"{scheduled_status.next_try}"
        )
    await db_session.commit()


async def publish_due_scheduled_statuses(
    db_session: AsyncSession,
    limit: int = BATCH_SIZE,
) -> int:
    """Publish every row whose time has come; returns how many went out."""
    due = await fetch_due_scheduled_statuses(db_session, limit)

    # Read out as plain data before anything can fail: one row's failure rolls
    # the session back, which expires every other ORM object in this list, and
    # an expired attribute can't be lazy-loaded from async code (it raises
    # MissingGreenlet). Each row is then re-attached with an awaited `get()`
    # inside its own iteration.
    pending = [(row.id, row.params) for row in due if row.id is not None]

    published = 0
    for scheduled_status_id, raw_params in pending:
        try:
            params = ComposeParams.from_json(raw_params)
            scheduled_status = await db_session.get(
                models.ScheduledStatus, scheduled_status_id
            )
            if scheduled_status is None:
                continue
            # The row is deleted as part of the send's own transaction — see
            # `publish`'s `delete_on_send`.
            await publish(db_session, params, delete_on_send=scheduled_status)
        except Exception as exc:
            await db_session.rollback()
            await _record_failure(db_session, scheduled_status_id, exc)
            continue

        logger.info(f"Published scheduled status {scheduled_status_id}")
        published += 1

    return published
