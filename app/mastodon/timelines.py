"""Reusable timeline queries and DM-thread grouping.

Shared by the REST timeline/conversation endpoints (`app/mastodon/router.py`)
and the streaming event pump (`app/mastodon/streaming.py`). No FastAPI import
here on purpose: `router.py` imports this module to declare its endpoints,
and `streaming.py` does too — if this module imported `router.py` back, that
would be a circular import.
"""

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import joinedload

import activitypub.models
from activitypub import activitypub as ap
from activitypub.boxes import AnyboxObject
from app import models
from app.database import AsyncSession
from app.mastodon import ids

TIMELINE_OBJECT_TYPES = ["Announce", "Article", "Note", "Page", "Question", "Video"]


def status_id_int(obj: AnyboxObject) -> int:
    """Sort key aligning array order with the status id's own ordering.

    The status id is timestamp-prefixed (see `app.mastodon.ids`), so sorting
    by it (rather than by `ap_published_at` directly) guarantees the returned
    array order exactly matches numeric id order — which is what the `Link`
    header's `max_id`/`min_id` cursors (`pagination.build_link_header`) and
    any client that re-sorts locally by id both assume.
    """
    status_id = (
        ids.encode_outbox_id(obj)
        if isinstance(obj, activitypub.models.OutboxObject)
        else ids.encode_inbox_id(obj)
    )
    return ids.mastodon_id_int(status_id)


async def fetch_inbox_timeline_page(
    db_session: AsyncSession,
    *,
    before: datetime | None,
    after: datetime | None,
    limit: int,
    extra_where: tuple = (),
) -> list[activitypub.models.InboxObject]:
    query = (
        select(activitypub.models.InboxObject)
        .where(
            activitypub.models.InboxObject.ap_type.in_(TIMELINE_OBJECT_TYPES),
            activitypub.models.InboxObject.is_hidden_from_stream.is_(False),
            activitypub.models.InboxObject.is_deleted.is_(False),
            # Applies to every timeline (home, public, hashtag), like
            # Mastodon: a mute hides the account everywhere but the profile.
            *activitypub.models.not_from_muted_actors(),
            # Read-time and retroactive: a follow's `reblogs=false` hides
            # their boosts everywhere the stream is queried.
            *activitypub.models.not_hidden_announces(),
            *extra_where,
        )
        .options(joinedload(activitypub.models.InboxObject.actor))
        .order_by(activitypub.models.InboxObject.ap_published_at.desc())
        .limit(limit)
    )
    if before:
        query = query.where(activitypub.models.InboxObject.ap_published_at < before)
    if after:
        query = query.where(activitypub.models.InboxObject.ap_published_at > after)
    return list((await db_session.scalars(query)).unique().all())


async def fetch_outbox_timeline_page(
    db_session: AsyncSession,
    *,
    before: datetime | None,
    after: datetime | None,
    limit: int,
    extra_where: tuple = (),
) -> list[activitypub.models.OutboxObject]:
    query = (
        select(activitypub.models.OutboxObject)
        .where(
            activitypub.models.OutboxObject.ap_type.in_(TIMELINE_OBJECT_TYPES),
            activitypub.models.OutboxObject.is_hidden_from_homepage.is_(False),
            activitypub.models.OutboxObject.is_deleted.is_(False),
            *extra_where,
        )
        .options(
            joinedload(
                activitypub.models.OutboxObject.outbox_object_attachments
            ).joinedload(activitypub.models.OutboxObjectAttachment.upload)
        )
        .order_by(activitypub.models.OutboxObject.ap_published_at.desc())
        .limit(limit)
    )
    if before:
        query = query.where(activitypub.models.OutboxObject.ap_published_at < before)
    if after:
        query = query.where(activitypub.models.OutboxObject.ap_published_at > after)
    return list((await db_session.scalars(query)).unique().all())


def tag_names(obj: AnyboxObject) -> set[str]:
    """`obj`'s ActivityPub Hashtag names, normalized (lowercased, `#`-stripped).

    Normalizing the *object* once and intersecting lets the streaming pump
    match against any number of subscribed hashtags in O(len(obj.tags))
    instead of one `has_tag` scan per subscribed tag.
    """
    return {
        tag.get("name", "").lstrip("#").lower()
        for tag in obj.tags
        if tag.get("type") == "Hashtag"
    }


def normalize_tag(name: str) -> str:
    """A hashtag as the timeline queries compare them: no `#`, lowercased."""
    return name.lstrip("#").lower()


def matches_tag_query(
    obj: AnyboxObject,
    any_of: Collection[str],
    all_of: Collection[str] = (),
    none_of: Collection[str] = (),
) -> bool:
    """Mastodon's multi-hashtag timeline predicate.

    `any_of` (the path hashtag plus any `any[]` params) must match at least
    one of the object's tags, every `all_of` tag must be present, and no
    `none_of` tag may be. All three must already be normalized by the caller
    (`normalize_tag`), matching how `timelines_tag` derives them.
    """
    # Set *methods*, not operators: they take any iterable, so none of the
    # three parameter collections gets copied into a temporary set once per
    # object scanned.
    names = tag_names(obj)
    if names.isdisjoint(any_of):
        return False
    if not names.issuperset(all_of):
        return False
    return names.isdisjoint(none_of)


def has_tag(obj: AnyboxObject, wanted: str) -> bool:
    """Whether `obj` carries an ActivityPub Hashtag tag matching `wanted`.

    `wanted` must already be normalized (lowercased, `#`-stripped) by the
    caller, matching how `timelines_tag` derives it from the path param.
    """
    return wanted in tag_names(obj)


# --- Conversations (DM threads) --------------------------------------------------
# Mastodon's DM inbox: one entry per `ap_context` thread of direct-visibility
# statuses. There's no dedicated "conversation" table, so threads are grouped
# the same way `app.admin.admin_direct_messages` builds the existing HTML view.


async def dm_thread_unread_contexts(db_session: AsyncSession) -> set[str]:
    return set(
        (
            await db_session.execute(
                select(activitypub.models.InboxObject.ap_context)
                .join(
                    models.Notification,
                    models.Notification.inbox_object_id
                    == activitypub.models.InboxObject.id,
                )
                .where(
                    models.Notification.notification_type
                    == models.NotificationType.MENTION,
                    models.Notification.is_new.is_(True),
                    activitypub.models.InboxObject.visibility
                    == ap.VisibilityEnum.DIRECT,
                    activitypub.models.InboxObject.ap_context.is_not(None),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )


async def dm_threads(
    db_session: AsyncSession,
) -> list[tuple[AnyboxObject, set[int], bool]]:
    """Every DM thread's most recent status, participant actor ids (from the
    inbox side only — an outbox-only thread has none yet), and unread state,
    newest first.
    """
    inbox_objects = (
        (
            await db_session.execute(
                select(activitypub.models.InboxObject)
                .where(
                    activitypub.models.InboxObject.visibility
                    == ap.VisibilityEnum.DIRECT,
                    activitypub.models.InboxObject.ap_context.is_not(None),
                    activitypub.models.InboxObject.is_transient.is_(False),
                    activitypub.models.InboxObject.is_deleted.is_(False),
                )
                .options(joinedload(activitypub.models.InboxObject.actor))
            )
        )
        .unique()
        .scalars()
        .all()
    )
    outbox_objects = (
        (
            await db_session.execute(
                select(activitypub.models.OutboxObject)
                .where(
                    activitypub.models.OutboxObject.visibility
                    == ap.VisibilityEnum.DIRECT,
                    activitypub.models.OutboxObject.ap_context.is_not(None),
                    activitypub.models.OutboxObject.is_transient.is_(False),
                    activitypub.models.OutboxObject.is_deleted.is_(False),
                )
                .options(
                    joinedload(
                        activitypub.models.OutboxObject.outbox_object_attachments
                    ).joinedload(activitypub.models.OutboxObjectAttachment.upload)
                )
            )
        )
        .unique()
        .scalars()
        .all()
    )

    unread_contexts = await dm_thread_unread_contexts(db_session)

    by_context: dict[str, dict] = {}
    for obj in [*inbox_objects, *outbox_objects]:
        thread = by_context.setdefault(
            obj.ap_context, {"objects": [], "actor_ids": set()}
        )
        thread["objects"].append(obj)
        if isinstance(obj, activitypub.models.InboxObject):
            thread["actor_ids"].add(obj.actor_id)

    threads = [
        (
            max(thread["objects"], key=status_id_int),
            thread["actor_ids"],
            context in unread_contexts,
        )
        for context, thread in by_context.items()
    ]
    threads.sort(key=lambda item: status_id_int(item[0]), reverse=True)
    return threads
