"""Grouping logic for Mastodon 4.3's grouped notifications
(`GET /api/v2/notifications` and friends, `app/mastodon/router.py`).

Shared with v1: `serializers.serialize_notification` calls `group_key_for` too,
so one notification carries the same `group_key` regardless of which surface
asked.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Sequence

from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select

from activitypub.boxes import AnyboxObject
from app import models
from app.database import AsyncSession

# Mastodon groups exactly these three types by default; `grouped_types[]`
# narrows the set further. Keys are explicitly opaque (Mastodon's own spec
# says so), so ours need only be deterministic, distinct, and URL-safe --
# they're a path segment.
GROUPABLE_TYPES = frozenset({"favourite", "follow", "reblog"})

_FALLBACK_CREATED_AT = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Bounds the id-desc walk that builds a page of groups: enough rows to fill
# `limit` groups without an unbounded scan.
_ASSEMBLY_WINDOW_CAP = 200

_FOLLOW_KEY_RE = re.compile(r"^follow-(\d{8})$")
_UNGROUPED_KEY_RE = re.compile(r"^ungrouped-(\d+)$")


def _decode_id(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _target_id(notification: models.Notification) -> int | None:
    return notification.outbox_object_id or notification.inbox_object_id


def is_groupable_row(notification: models.Notification) -> bool:
    """Mirrors the drop conditions `serializers.serialize_notification`
    applies before it would return ``None`` -- group a row that serializer
    would reject and the group ends up carrying an unserializable member
    (e.g. no `actor` to build `sample_account_ids` from).
    """
    # Local import: avoids a circular import, since `serializers` also
    # imports this module (to compute the `group_key` it emits).
    from app.mastodon import serializers

    if notification.notification_type is None:
        return False
    if (
        notification.actor is None
        and notification.notification_type != models.NotificationType.POLL
    ):
        return False
    if notification.notification_type not in serializers.NOTIFICATION_TYPE_MAP:
        return False
    # The FK survived but the row it pointed to is gone, so the group would
    # carry a dangling `status_id` -- mirrors the drop in
    # `serializers.serialize_notification`. Normally already excluded in SQL
    # by `models.notification_target_present()`; kept as the backstop for a
    # caller that builds its own `where`.
    if (
        (notification.outbox_object_id or notification.inbox_object_id)
        and notification.outbox_object is None
        and notification.inbox_object is None
    ):
        return False
    return True


def group_key_for(
    notification: models.Notification,
    grouped_types: frozenset[str] = GROUPABLE_TYPES,
) -> str:
    # Local import: avoids a circular import (see `is_groupable_row`).
    from app.mastodon import serializers

    notification_type = notification.notification_type
    mastodon_type = (
        serializers.NOTIFICATION_TYPE_MAP.get(notification_type)
        if notification_type is not None
        else None
    )
    if mastodon_type is None or mastodon_type not in grouped_types:
        return f"ungrouped-{notification.id}"

    if mastodon_type in ("favourite", "reblog"):
        target_id = _target_id(notification)
        if target_id is None:
            return f"ungrouped-{notification.id}"
        return f"{mastodon_type}-{target_id}"

    if mastodon_type == "follow":
        created_at = notification.created_at or _FALLBACK_CREATED_AT
        return f"follow-{created_at:%Y%m%d}"

    return f"ungrouped-{notification.id}"


def group_key_where_clause(group_key: str) -> Any | None:
    """The inverse of `group_key_for`: a SQL where-clause matching every
    notification that would compute back to `group_key`, or `None` if the
    key doesn't parse as one of the shapes `group_key_for` produces.
    """
    # Local import: avoids a circular import (see `is_groupable_row`).
    from app.mastodon import serializers

    if match := _UNGROUPED_KEY_RE.match(group_key):
        return models.Notification.id == int(match.group(1))

    if match := _FOLLOW_KEY_RE.match(group_key):
        follow_types = [
            internal
            for internal, mastodon in serializers.NOTIFICATION_TYPE_MAP.items()
            if mastodon == "follow"
        ]
        return and_(
            models.Notification.notification_type.in_(follow_types),
            func.strftime("%Y%m%d", models.Notification.created_at) == match.group(1),
        )

    for mastodon_type in ("favourite", "reblog"):
        prefix = f"{mastodon_type}-"
        if group_key.startswith(prefix):
            target_id = _decode_id(group_key[len(prefix) :])
            if target_id is None:
                return None
            internal_types = [
                internal
                for internal, mastodon in serializers.NOTIFICATION_TYPE_MAP.items()
                if mastodon == mastodon_type
            ]
            return and_(
                models.Notification.notification_type.in_(internal_types),
                or_(
                    models.Notification.outbox_object_id == target_id,
                    models.Notification.inbox_object_id == target_id,
                ),
            )

    return None


@dataclass
class NotificationGroup:
    key: str
    mastodon_type: str
    # Newest-first, the order the assembly walk appends them in. For the
    # list endpoint this may be a subset of the group's true membership (a
    # group can be cut mid-page -- see `fetch_notification_group_page`); for
    # a single-group lookup it's capped at `_SAMPLE_LIMIT`.
    notifications: list[models.Notification]
    notifications_count: int

    @property
    def most_recent_notification_id(self) -> int:
        notification_id = self.notifications[0].id
        assert notification_id is not None
        return notification_id

    @property
    def page_max_id(self) -> int:
        return self.most_recent_notification_id

    @property
    def page_min_id(self) -> int:
        notification_id = self.notifications[-1].id
        assert notification_id is not None
        return notification_id

    @property
    def latest_page_notification_at(self) -> datetime:
        return self.notifications[0].created_at or _FALLBACK_CREATED_AT

    @property
    def sample_account_ids(self) -> list[int]:
        seen: list[int] = []
        for notification in self.notifications:
            if notification.actor_id is None or notification.actor_id in seen:
                continue
            seen.append(notification.actor_id)
            if len(seen) >= 4:
                break
        return seen

    @property
    def target(self) -> AnyboxObject | None:
        anchor = self.notifications[0]
        return anchor.outbox_object or anchor.inbox_object


async def _group_counts(
    db_session: AsyncSession,
    where: Sequence[Any],
    grouped_types: frozenset[str],
) -> dict[str, int]:
    """True per-group totals via one aggregate query, covering every
    groupable row the page's filters match -- not just the ones fetched into
    the bounded assembly window. Ungrouped groups are 1 by construction and
    aren't queried here; the assembly walk already gives an exact count for
    those.
    """
    # Local import: avoids a circular import (see `is_groupable_row`).
    from app.mastodon import serializers

    active_grouped_types = GROUPABLE_TYPES & grouped_types
    if not active_grouped_types:
        return {}

    internal_types = [
        internal
        for internal, mastodon in serializers.NOTIFICATION_TYPE_MAP.items()
        if mastodon in active_grouped_types
    ]

    day_bucket = func.strftime("%Y%m%d", models.Notification.created_at)
    query = select(
        models.Notification.notification_type,
        models.Notification.outbox_object_id,
        models.Notification.inbox_object_id,
        day_bucket,
        func.count(),
    ).where(*where, models.Notification.notification_type.in_(internal_types))
    query = query.group_by(
        models.Notification.notification_type,
        models.Notification.outbox_object_id,
        models.Notification.inbox_object_id,
        day_bucket,
    )

    counts: dict[str, int] = {}
    for notif_type, outbox_id, inbox_id, bucket, count in await db_session.execute(
        query
    ):
        mastodon_type = serializers.NOTIFICATION_TYPE_MAP[notif_type]
        if mastodon_type in ("favourite", "reblog"):
            target_id = outbox_id or inbox_id
            if target_id is None:
                continue
            key = f"{mastodon_type}-{target_id}"
        else:
            key = f"follow-{bucket}"
        counts[key] = counts.get(key, 0) + count
    return counts


async def fetch_notification_group_page(
    db_session: AsyncSession,
    *,
    where: Sequence[Any],
    grouped_types: frozenset[str],
    limit: int,
    max_id: str | None,
    cursor: str | None,
) -> list[NotificationGroup]:
    """The `GET /api/v2/notifications` (and `unread_count`) core: a page of
    up to `limit` groups, id-desc, built by walking a bounded window of raw
    notification rows and folding them into groups in first-seen order.

    Cutting a group mid-page is fine: `NotificationGroup.page_min_id` is the
    oldest row *seen so far* for that group, so the next page's `max_id`
    picks up exactly where this one left off, and `notifications_count`
    comes from the true aggregate below, not the window.
    """
    # Local import: avoids a circular import (see `is_groupable_row`).
    from app.mastodon import serializers

    window = min(limit * 4, _ASSEMBLY_WINDOW_CAP)
    query = (
        select(models.Notification)
        .where(*where)
        .options(*serializers.NOTIFICATION_OPTIONS)
        .order_by(models.Notification.id.desc())
        .limit(window)
    )
    if max_id and (decoded := _decode_id(max_id)) is not None:
        query = query.where(models.Notification.id < decoded)
    if cursor and (decoded := _decode_id(cursor)) is not None:
        query = query.where(models.Notification.id > decoded)

    rows = list((await db_session.scalars(query)).unique().all())

    grouped_rows: dict[str, list[models.Notification]] = {}
    order: list[str] = []
    for notification in rows:
        if not is_groupable_row(notification):
            continue
        key = group_key_for(notification, grouped_types)
        if key not in grouped_rows:
            if len(order) >= limit:
                break
            order.append(key)
            grouped_rows[key] = []
        grouped_rows[key].append(notification)

    counts = await _group_counts(db_session, where, grouped_types)

    groups = []
    for key in order:
        rows_for_key = grouped_rows[key]
        # `is_groupable_row` already guaranteed a non-None, mapped type.
        notification_type = rows_for_key[0].notification_type
        assert notification_type is not None
        mastodon_type = serializers.NOTIFICATION_TYPE_MAP[notification_type]
        count = counts.get(key, len(rows_for_key))
        groups.append(NotificationGroup(key, mastodon_type, rows_for_key, count))
    return groups


_SAMPLE_LIMIT = 4


async def fetch_notification_group(
    db_session: AsyncSession,
    *,
    group_key: str,
    where: Sequence[Any],
    grouped_types: frozenset[str],
) -> NotificationGroup | None:
    """The `GET /api/v2/notifications/{group_key}` core: one group, looked
    up directly by key rather than by re-walking a page. `notifications_count`
    is an exact `COUNT(*)` scoped to the key, cheap since it's a single
    key/target/day match rather than a whole-page aggregate.
    """
    # Local import: avoids a circular import (see `is_groupable_row`).
    from app.mastodon import serializers

    key_where = group_key_where_clause(group_key)
    if key_where is None:
        return None

    count = await db_session.scalar(
        select(func.count()).select_from(
            select(models.Notification.id).where(*where, key_where).subquery()
        )
    )
    if not count:
        return None

    rows = list(
        (
            await db_session.scalars(
                select(models.Notification)
                .where(*where, key_where)
                .options(*serializers.NOTIFICATION_OPTIONS)
                .order_by(models.Notification.id.desc())
                .limit(_SAMPLE_LIMIT)
            )
        )
        .unique()
        .all()
    )
    if not rows:
        return None

    # Ground-truth check: confirm the newest matching row actually computes
    # back to the requested key (guards e.g. a `grouped_types[]` narrower
    # than the one implied by the key, which would make this a stale key
    # rather than a real group).
    if group_key_for(rows[0], grouped_types) != group_key:
        return None

    mastodon_type = serializers.NOTIFICATION_TYPE_MAP[rows[0].notification_type]
    return NotificationGroup(group_key, mastodon_type, rows, count)


async def fetch_group_account_ids(
    db_session: AsyncSession,
    *,
    group_key: str,
    where: Sequence[Any],
) -> list[int] | None:
    """The `GET /api/v2/notifications/{group_key}/accounts` core: every
    distinct actor in the group, newest-first, unbounded (unlike the sample
    of 4 `NotificationGroup.sample_account_ids` caps at) -- the endpoint's
    whole point is the full list. `None` means the key doesn't resolve to
    any (visible) notification, which the caller turns into a 404.
    """
    key_where = group_key_where_clause(group_key)
    if key_where is None:
        return None

    actor_ids = list(
        (
            await db_session.scalars(
                select(models.Notification.actor_id)
                .where(*where, key_where, models.Notification.actor_id.is_not(None))
                .order_by(models.Notification.id.desc())
            )
        ).all()
    )
    if not actor_ids:
        return None

    seen: set[int] = set()
    ordered: list[int] = []
    for actor_id in actor_ids:
        if actor_id not in seen:
            seen.add(actor_id)
            ordered.append(actor_id)
    return ordered
