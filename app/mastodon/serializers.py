"""Mastodon entity serializers.

`serialize_account` handles the two kinds of actor this server ever needs to
serialize: the local owner (`activitypub.actor.LOCAL_ACTOR`, not a DB row —
see `app/mastodon/ids.py`) and a cached remote actor (a
`activitypub.models.Actor` row). Serializing an arbitrary not-yet-cached
`RemoteActor` wrapper (e.g. a live search resolve) is deferred to PR-3.

`serialize_status` handles both `InboxObject` and `OutboxObject` rows
(`activitypub.boxes.AnyboxObject`) uniformly via their shared `Object`
wrapper interface.
"""

import hashlib
import mimetypes
from collections.abc import Iterable
from datetime import datetime
from datetime import timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import event
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload

import activitypub.models
from activitypub import activitypub as ap
from activitypub.actor import LOCAL_ACTOR
from activitypub.actor import Actor as BaseActor
from activitypub.ap_object import Attachment
from activitypub.ap_object import Object as APObjectView
from activitypub.boxes import AnyboxObject
from activitypub.boxes import get_anybox_object_by_ap_id
from activitypub.boxes import get_anybox_objects_by_ap_ids
from activitypub.boxes import public_outbox_objects_count
from app import config
from app import models
from app import scheduled_statuses
from app.database import AsyncSession
from app.mastodon import ids
from app.media import proxied_media_url
from app.utils.datetime import parse_isoformat

# Sentinel distinguishing "no override" from "override to None" in
# serialize_media_attachment's `description` param.
_UNSET = object()

# --- Notifications -------------------------------------------------------------
#
# Public (not `app.mastodon.router`-private) because the push delivery worker
# (`app/push_notifications.py`) needs them too, and importing `router.py` from
# a worker would drag the whole FastAPI app into a background process.

# Only these carry a real Mastodon equivalent. Everything else (undo_*,
# webmention_*, block/unblock, unfollow, follow_request_accepted/rejected) has
# no matching Mastodon notification type, so it's filtered out entirely
# rather than surfaced with a made-up/incorrect `type`.
NOTIFICATION_TYPE_MAP = {
    models.NotificationType.NEW_FOLLOWER: "follow",
    models.NotificationType.PENDING_INCOMING_FOLLOWER: "follow_request",
    models.NotificationType.LIKE: "favourite",
    models.NotificationType.ANNOUNCE: "reblog",
    models.NotificationType.MENTION: "mention",
    models.NotificationType.MOVE: "move",
    models.NotificationType.STATUS: "status",
    models.NotificationType.UPDATE: "update",
    models.NotificationType.POLL: "poll",
}

NOTIFICATION_OPTIONS = [
    joinedload(models.Notification.actor),
    joinedload(models.Notification.inbox_object).options(
        joinedload(activitypub.models.InboxObject.actor)
    ),
    joinedload(models.Notification.outbox_object).options(
        joinedload(activitypub.models.OutboxObject.outbox_object_attachments).options(
            joinedload(activitypub.models.OutboxObjectAttachment.upload)
        ),
    ),
]


async def serialize_notification(
    db_session: AsyncSession, notification: models.Notification
) -> dict | None:
    if notification.notification_type is None:
        return None
    # A POLL notification about the owner's own poll has no remote actor;
    # every other type is dropped without one.
    if (
        notification.actor is None
        and notification.notification_type != models.NotificationType.POLL
    ):
        return None

    mastodon_type = NOTIFICATION_TYPE_MAP.get(notification.notification_type)
    if mastodon_type is None:
        return None

    target = notification.outbox_object or notification.inbox_object
    # The FK survived but the row it pointed to is gone: drop it rather than
    # serialize a dangling reference (rationale in
    # `models.notification_target_present`, the SQL guard that keeps these
    # out of the endpoints' pages in the first place -- this is the backstop
    # for callers that build their own query, e.g. `mastodon/streaming.py`).
    if target is None and (
        notification.outbox_object_id or notification.inbox_object_id
    ):
        return None

    # Local import: `notification_groups` reuses this module's
    # NOTIFICATION_TYPE_MAP/NOTIFICATION_OPTIONS, so importing it at module
    # level here would be circular.
    from app.mastodon import notification_groups

    created_at = notification.created_at or datetime.min.replace(tzinfo=timezone.utc)
    account = (
        await serialize_account(db_session, notification.actor)
        if notification.actor is not None
        else await serialize_owner_account(db_session)
    )
    result = {
        "id": str(notification.id),
        "type": mastodon_type,
        # Non-optional since Mastodon 4.3, which is the compatibility level
        # `_MASTODON_COMPAT_VERSION` advertises — a strict client decoder
        # (the official app's) fails the whole notifications screen on a
        # missing key, not just this row. Real grouping (`favourite`/
        # `follow`/`reblog`), `ungrouped-{id}` for everything else — see
        # `app.mastodon.notification_groups.group_key_for`, shared with the
        # `/api/v2/notifications*` endpoints so a notification never carries
        # two different keys depending on which surface asked.
        "group_key": notification_groups.group_key_for(notification),
        "created_at": format_datetime(created_at),
        "account": account,
    }

    if target is not None:
        result["status"] = await serialize_status(db_session, target)

    return result


_CACHE_KEY = "_mastodon_serializer_cache"


def _request_cache(db_session: AsyncSession) -> dict:
    """Per-request memoization for values `serialize_status`/`serialize_account`
    would otherwise re-fetch once per status on a timeline (see `_owner_counts`
    and `_is_conversation_muted`). Scoped to `db_session.info` since a fresh
    session is created per request (`get_db_session`), so session lifetime is
    request lifetime here. Invalidated on every commit (see the `after_commit`
    listener below) so a handler that writes then re-serializes never reads
    stale cached values.
    """
    return db_session.info.setdefault(_CACHE_KEY, {})


@event.listens_for(Session, "after_commit")
def _invalidate_request_cache(session: Session) -> None:
    session.info.pop(_CACHE_KEY, None)


async def _muted_conversations(db_session: AsyncSession) -> set[str]:
    cache = _request_cache(db_session)
    if (muted := cache.get("muted_conversations")) is None:
        muted = set(
            (
                await db_session.scalars(select(models.MutedConversation.conversation))
            ).all()
        )
        cache["muted_conversations"] = muted
    return muted


async def _is_conversation_muted(
    db_session: AsyncSession, conversation: str | None
) -> bool:
    if conversation is None:
        return False
    return conversation in (await _muted_conversations(db_session))


def _anybox_cache(db_session: AsyncSession) -> dict[str, AnyboxObject | None]:
    return _request_cache(db_session).setdefault("anybox_objects", {})


async def _get_anybox_object(
    db_session: AsyncSession, ap_id: str
) -> AnyboxObject | None:
    """Request-cached `get_anybox_object_by_ap_id`.

    `None` is cached too: a reply whose parent was never fetched is common,
    and without negative caching every serialization of it would re-query.
    """
    cache = _anybox_cache(db_session)
    if ap_id not in cache:
        cache[ap_id] = await get_anybox_object_by_ap_id(db_session, ap_id)
    return cache[ap_id]


def _related_ap_ids(objects: Iterable[AnyboxObject]) -> set[str]:
    """The ap_ids `serialize_status` resolves for each object: the boost
    target, the in-reply-to parent, and the quoted post."""
    ap_ids = set()
    for obj in objects:
        if obj.in_reply_to:
            ap_ids.add(obj.in_reply_to)
        if obj.ap_type == "Announce" and obj.activity_object_ap_id:
            ap_ids.add(obj.activity_object_ap_id)
        if obj.quote_ap_id:
            ap_ids.add(obj.quote_ap_id)
    return ap_ids


async def prefetch_status_relations(
    db_session: AsyncSession, objects: Iterable[AnyboxObject]
) -> None:
    """Load a page's boost targets and in-reply-to parents in two `IN (...)`
    queries, into the same cache `_get_anybox_object` reads.

    Call this before serializing a list of statuses; `serialize_status` stays
    correct without it, just one query per relation per status again.
    """
    cache = _anybox_cache(db_session)
    pending = _related_ap_ids(objects) - cache.keys()

    # Two rounds: the statuses themselves, then the boost targets they
    # resolved, since serialize_status follows those targets' own
    # `in_reply_to`. Depth stops there — the recursive call passes
    # `_resolve_reblog=False`.
    for _ in range(2):
        if not pending:
            return
        fetched = await get_anybox_objects_by_ap_ids(db_session, pending)
        for obj in fetched:
            cache[obj.ap_id] = obj
        for ap_id in pending:
            cache.setdefault(ap_id, None)
        pending = _related_ap_ids(fetched) - cache.keys()


# The actor keypair is generated once, during initial setup, and never
# rotated — its mtime is a reasonable proxy for "when this instance/account
# was created" in the absence of any stored value to that effect.
_FALLBACK_CREATED_AT = datetime(1970, 1, 1, tzinfo=timezone.utc)


def format_datetime(dt: datetime) -> str:
    """Format a datetime the way the Mastodon API does: RFC3339 with
    millisecond precision and a ``Z`` suffix (``2024-01-01T00:00:00.000Z``).

    Python's default ``isoformat()`` emits either no fractional part or
    6-digit microseconds; pinning to 3 digits keeps strict clients that only
    accept millisecond precision (some RFC3339 parsers) from rejecting it.
    """
    return (
        dt.replace(tzinfo=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _as_str(value: object, fallback: str = "") -> str:
    """Coerce a federation-supplied value to a plain non-empty string.

    Remote actors/objects can populate a field the Mastodon API types as a
    non-null string with a dict, list, or null instead (e.g. ``url`` given as
    a Link object, or a non-string ``name``). Emitting a non-string there
    makes strict clients (Tusky/Fedilab) fail to deserialize and silently drop
    the entire response, so fall back to ``fallback`` when the value isn't a
    usable string.
    """
    return value if isinstance(value, str) and value else fallback


def _owner_created_at() -> datetime:
    try:
        return datetime.fromtimestamp(config.KEY_PATH.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return _FALLBACK_CREATED_AT


async def _owner_counts(db_session: AsyncSession) -> tuple[int, int, int]:
    cache = _request_cache(db_session)
    if (counts := cache.get("owner_counts")) is None:
        followers_count = (
            await db_session.scalar(select(func.count(activitypub.models.Follower.id)))
            or 0
        )
        following_count = (
            await db_session.scalar(select(func.count(activitypub.models.Following.id)))
            or 0
        )
        statuses_count = await public_outbox_objects_count(db_session)
        counts = (followers_count, following_count, statuses_count)
        cache["owner_counts"] = counts
    return counts


def serialize_extended_description() -> dict:
    """`GET /api/v1/instance/extended_description`.

    Prefers `profile.toml`'s optional long-form `about` field (also shown on
    `/about`), falling back to the same bio text `/api/v1/instance`'s
    `description`/`short_description` already collapse to. `updated_at` has no
    real change-history to draw from either, so it reuses the same
    keypair-mtime proxy `_owner_created_at` already stands in for "when this
    instance was set up" on the account entity.
    """
    return {
        "updated_at": format_datetime(_owner_created_at()),
        "content": config.ABOUT_HTML or LOCAL_ACTOR.summary or "",
    }


def serialize_instance_domain_blocks() -> list[dict]:
    """`GET /api/v1/instance/domain_blocks` — the public transparency list.

    Distinct from `GET /api/v1/domain_blocks` (gap #20, already implemented):
    that one is the authenticated personal blocklist a logged-in client reads;
    this one is unauthenticated and includes the optional reason, matching
    real Mastodon's `DomainBlock` entity. Severity is always `suspend` since
    `blocked_servers` doesn't distinguish silence/suspend/reject.
    """
    return [
        {
            "domain": blocked_server.hostname,
            "digest": hashlib.sha256(blocked_server.hostname.encode()).hexdigest(),
            "severity": "suspend",
            "comment": blocked_server.reason,
        }
        for blocked_server in sorted(
            config.CONFIG.blocked_servers, key=lambda b: b.hostname
        )
    ]


async def serialize_featured_tags(db_session: AsyncSession) -> list[dict]:
    """Map the profile's `featured_tags` config list to Mastodon's
    FeaturedTag entity.

    Read-only: featured tags are configured in `data/profile.toml`, not
    stored in the DB, so there's no POST/DELETE — just this GET-backed view.
    Counts/`last_status_at` come from `TaggedOutboxObject`, the same index
    `GET /t/{tag}` uses, rather than the Mastodon router's bounded JSON-scan
    hashtag timeline.
    """
    featured_tags = []
    for index, raw_tag in enumerate(config.FEATURED_TAGS):
        tag = raw_tag.lstrip("#").lower()
        where = [
            activitypub.models.TaggedOutboxObject.tag == tag,
            activitypub.models.OutboxObject.visibility == ap.VisibilityEnum.PUBLIC,
            activitypub.models.OutboxObject.is_deleted.is_(False),
        ]
        statuses_count, last_status_at = (
            await db_session.execute(
                select(
                    func.count(activitypub.models.OutboxObject.id),
                    func.max(activitypub.models.OutboxObject.ap_published_at),
                )
                .join(activitypub.models.TaggedOutboxObject)
                .where(*where)
            )
        ).one()
        featured_tags.append(
            {
                "id": str(index),
                "name": tag,
                "url": f"{config.BASE_URL}/t/{tag}",
                "statuses_count": str(statuses_count or 0),
                "last_status_at": (
                    last_status_at.date().isoformat() if last_status_at else None
                ),
            }
        )
    return featured_tags


def _fields(actor: BaseActor) -> list[dict]:
    return [
        {
            "name": _as_str(item.get("name")),
            "value": _as_str(item.get("value")),
            "verified_at": None,
        }
        for item in actor.attachments
        if isinstance(item, dict) and item.get("type") == "PropertyValue"
    ]


async def serialize_account(
    db_session: AsyncSession,
    actor: BaseActor,
    *,
    moved_to: activitypub.models.Actor | None = None,
) -> dict:
    if isinstance(actor, activitypub.models.Actor):
        account_id = ids.encode_account_id(actor)
        # Never use the wrapper's `.handle` here: for a transient (non-DB)
        # RemoteActor it does a live webfinger call. Deriving it from the
        # actor's own ap_id is free and always available.
        acct = f"{actor.preferred_username}@{urlparse(actor.ap_id).netloc}"
        created_at = actor.created_at or _FALLBACK_CREATED_AT
        locked = bool(actor.ap_actor.get("manuallyApprovesFollowers", False))
        # Honour the remote actor's own hints when it publishes them, rather
        # than asserting anything on its behalf. Absent means "didn't say",
        # so keep the permissive reading both properties defaulted to before.
        discoverable = bool(actor.ap_actor.get("discoverable", True))
        indexable = bool(actor.ap_actor.get("indexable", True))
        # Cached on demand from the actor's own AP collections (see
        # `refresh_actor_counts`) — 0 until the first refresh happens.
        followers_count = actor.followers_count or 0
        following_count = actor.following_count or 0
        statuses_count = actor.statuses_count or 0
    else:
        account_id = ids.LOCAL_ACTOR_ID
        acct = actor.preferred_username
        created_at = _owner_created_at()
        locked = config.MANUALLY_APPROVES_FOLLOWERS
        discoverable = config.DISCOVERABLE
        indexable = config.INDEXABLE
        followers_count, following_count, statuses_count = await _owner_counts(
            db_session
        )

    return {
        "id": account_id,
        "username": _as_str(actor.preferred_username),
        "acct": _as_str(acct),
        "display_name": _as_str(actor.display_name),
        "locked": locked,
        "bot": actor.ap_type == "Service",
        "discoverable": discoverable,
        "noindex": not indexable,
        "group": False,
        "created_at": format_datetime(created_at),
        "note": _as_str(actor.summary),
        # `actor.url` can be a Link dict/list on some servers; coerce to a
        # string, falling back to the actor's id.
        "url": _as_str(actor.url, actor.ap_id),
        "uri": actor.ap_id,
        "avatar": _as_str(actor.resized_icon_url),
        "avatar_static": _as_str(actor.proxied_icon_url),
        "header": _as_str(actor.proxied_image_url),
        "header_static": _as_str(actor.proxied_image_url),
        "followers_count": followers_count,
        "following_count": following_count,
        "statuses_count": statuses_count,
        "last_status_at": None,
        "emojis": [],
        "fields": _fields(actor),
        "moved": (await serialize_account(db_session, moved_to) if moved_to else None),
    }


async def serialize_owner_account(db_session: AsyncSession) -> dict:
    return await serialize_account(db_session, LOCAL_ACTOR)


_VISIBILITY_MAP = {
    ap.VisibilityEnum.PUBLIC: "public",
    ap.VisibilityEnum.UNLISTED: "unlisted",
    ap.VisibilityEnum.FOLLOWERS_ONLY: "private",
    ap.VisibilityEnum.DIRECT: "direct",
}


# Matches the `video.duration <= 10.0` GIF-mode threshold in
# app/static/common.js, so a status looks the same whether a client renders
# it via this `type` or via the web UI's own client-side heuristic.
_GIFV_MAX_DURATION = 10.0


def _mastodon_media_type(
    media_type: str | None,
    url: str,
    *,
    duration: float | None = None,
    has_audio: bool | None = None,
) -> str:
    if not media_type:
        media_type, _ = mimetypes.guess_type(url)
    if not media_type:
        return "unknown"
    top_level = media_type.split("/", 1)[0]
    if (
        top_level == "video"
        and has_audio is False
        and duration is not None
        and duration <= _GIFV_MAX_DURATION
    ):
        return "gifv"
    return top_level if top_level in ("image", "video", "audio") else "unknown"


def _thumbnail_box(width: int, height: int, max_size: int = 740) -> tuple[int, int]:
    """Approximates the box PIL's `Image.thumbnail((740, 740))` produces, so
    `meta.small` matches the dimensions of the webp actually served."""
    if width <= max_size and height <= max_size:
        return width, height
    ratio = min(max_size / width, max_size / height)
    return max(1, round(width * ratio)), max(1, round(height * ratio))


def _format_length(duration: float) -> str:
    """Mastodon's `meta.length`, e.g. 88.65 -> "0:01:28.65"."""
    hours, remainder = divmod(duration, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{int(hours)}:{int(minutes):02d}:{seconds:05.2f}"


def _media_meta(
    width: int | None,
    height: int | None,
    duration: float | None,
    has_thumbnail: bool,
    focus: tuple[float, float] | None = None,
) -> dict:
    meta: dict = {}
    original: dict = {}

    if width and height:
        size = f"{width}x{height}"
        aspect = width / height
        original.update(
            {"width": width, "height": height, "size": size, "aspect": aspect}
        )
        meta.update({"width": width, "height": height, "size": size, "aspect": aspect})

    if duration is not None:
        original["duration"] = duration
        meta["duration"] = duration
        meta["length"] = _format_length(duration)

    if original:
        meta["original"] = original

    if has_thumbnail and width and height:
        small_width, small_height = _thumbnail_box(width, height)
        meta["small"] = {
            "width": small_width,
            "height": small_height,
            "size": f"{small_width}x{small_height}",
            "aspect": small_width / small_height,
        }

    if focus is not None:
        meta["focus"] = {"x": focus[0], "y": focus[1]}

    return meta


def serialize_media_attachment(
    attachment: Attachment,
    index: int,
    status_id: str,
    media_id: str | None = None,
    description: Any = _UNSET,
) -> dict:
    """`media_id`/`description` override the id/description this attachment
    reports. Local (`OutboxObject`) attachments pass the underlying Upload's
    id and the per-attachment alt text, so `media_ids` round-trips on an edit
    and a missing alt doesn't fall back to the synthetic filename. Remote and
    revision-snapshot attachments have no `Upload` row behind them and keep
    the id/description derived from the AP object itself.
    """
    url = attachment.proxied_url or attachment.url
    duration = attachment.duration_seconds
    media_type = _mastodon_media_type(
        attachment.media_type,
        attachment.url,
        duration=duration,
        has_audio=attachment.has_audio,
    )

    has_thumbnail = bool(attachment.poster_url or attachment.resized_url)
    preview_url = attachment.poster_url or attachment.resized_url
    if preview_url is None and media_type not in ("video", "audio", "gifv"):
        # Images degrade to the full-size URL when no thumbnail exists;
        # video/audio never do (Mastodon's preview_url is nullable, and a
        # client shouldn't be handed raw media to render as a poster).
        preview_url = url

    return {
        # Not independently addressable in this backend (no separate media
        # lookup for already-attached media) — scoped to the parent status,
        # unless the caller supplied the underlying Upload's own id.
        "id": media_id if media_id is not None else f"{status_id}-{index}",
        "type": media_type,
        "url": url,
        "preview_url": preview_url,
        "remote_url": attachment.url,
        "meta": _media_meta(
            attachment.width,
            attachment.height,
            duration,
            has_thumbnail,
            focus=attachment.focus,
        ),
        "description": attachment.name if description is _UNSET else description,
        "blurhash": attachment.blurhash,
    }


def _serialize_status_media_attachments(
    obj: AnyboxObject, status_id: str
) -> list[dict]:
    if isinstance(obj, activitypub.models.OutboxObject):
        # Pair each Attachment view 1:1 with the OutboxObjectAttachment row it
        # was built from (activitypub/models.py's `attachments` property
        # iterates `outbox_object_attachments` in the same order) so the
        # reported id is the underlying Upload's own id, and the description
        # is the per-attachment alt rather than a synthetic filename.
        return [
            serialize_media_attachment(
                attachment,
                index,
                status_id,
                media_id=ids.encode_upload_id(row.upload),
                description=row.alt,
            )
            for index, (attachment, row) in enumerate(
                zip(obj.attachments, obj.outbox_object_attachments)
            )
        ]
    return [
        serialize_media_attachment(attachment, index, status_id)
        for index, attachment in enumerate(obj.attachments)
    ]


def _object_language(obj: AnyboxObject) -> str | None:
    content_map = obj.ap_object.get("contentMap")
    if isinstance(content_map, dict) and content_map:
        return next(iter(content_map))
    return None


def serialize_poll(obj: AnyboxObject, status_id: str) -> dict | None:
    if not obj.poll_items:
        return None

    options = [
        {
            "title": item.get("name", ""),
            "votes_count": item.get("replies", {}).get("totalItems", 0),
        }
        for item in obj.poll_items
    ]

    # Only InboxObject tracks the owner's own vote (`send_vote` only supports
    # voting on a remote (inbox) poll — see app/mastodon/router.py).
    voted_names = (
        obj.voted_for_answers or []
        if isinstance(obj, activitypub.models.InboxObject)
        else []
    )
    own_votes = [
        index
        for index, item in enumerate(obj.poll_items)
        if item.get("name") in voted_names
    ]

    # `votes_count` is votes *cast* and `voters_count` is people: on a
    # multiple-choice poll the first is the larger number, so it can't just
    # mirror the second (a client labelling "N votes" would under-report, and
    # one that takes votes_count as the percentage denominator would render
    # totals over 100%). Floored at the voter count for remotes that publish
    # `votersCount` without per-option tallies, where the sum reads 0.
    voters_count = obj.poll_voters_count
    votes_count = max(
        sum(option["votes_count"] for option in options), voters_count or 0
    )

    return {
        "id": status_id,
        "expires_at": (
            format_datetime(obj.poll_end_time) if obj.poll_end_time else None
        ),
        "expired": obj.is_poll_ended,
        "multiple": not obj.is_one_of_poll,
        "votes_count": votes_count,
        "voters_count": voters_count,
        "voted": bool(own_votes),
        "own_votes": own_votes,
        "options": options,
        "emojis": [],
    }


async def _serialize_mentions(
    db_session: AsyncSession, obj: AnyboxObject
) -> list[dict]:
    mention_tags = [
        tag
        for tag in obj.tags
        if isinstance(tag, dict)
        and tag.get("type") == "Mention"
        and _as_str(tag.get("href"))
    ]
    if not mention_tags:
        return []

    hrefs = [tag["href"] for tag in mention_tags]
    known_actors = {
        actor.ap_id: actor
        for actor in (
            await db_session.scalars(
                select(activitypub.models.Actor).where(
                    activitypub.models.Actor.ap_id.in_(hrefs)
                )
            )
        ).all()
    }

    mentions = []
    for tag in mention_tags:
        href = tag["href"]
        actor = known_actors.get(href)
        if href == LOCAL_ACTOR.ap_id:
            # The owner is never a row in `Actor` (it's the `RemoteActor`
            # wrapper `LOCAL_ACTOR`, same as `serialize_owner_account`), so
            # without this branch every self-mention fell through to the
            # "not cached locally" stub below with `id: ""` -- in a Mastodon
            # client, tapping your own handle in a mention then does nothing.
            mentions.append(
                {
                    "id": ids.LOCAL_ACTOR_ID,
                    "username": _as_str(LOCAL_ACTOR.preferred_username),
                    "url": _as_str(LOCAL_ACTOR.url, LOCAL_ACTOR.ap_id),
                    "acct": _as_str(LOCAL_ACTOR.preferred_username),
                }
            )
        elif actor:
            mentions.append(
                {
                    "id": ids.encode_account_id(actor),
                    "username": _as_str(actor.preferred_username),
                    "url": _as_str(actor.url, actor.ap_id),
                    "acct": f"{actor.preferred_username}@{urlparse(actor.ap_id).netloc}",
                }
            )
        else:
            # Not cached locally: degrade to a stub built from the tag itself
            # rather than fetching it (serializing must stay network-free).
            # The tag's `name` is the full "@user@domain" handle -- split it
            # so `username` is bare like every other account this serializer
            # emits, while `acct` keeps the full handle.
            handle = _as_str(tag.get("name")).lstrip("@")
            username = handle.split("@", 1)[0]
            mentions.append(
                {"id": "", "username": username, "url": href, "acct": handle}
            )

    return mentions


def _serialize_hashtags(obj: AnyboxObject) -> list[dict]:
    return [
        {
            "name": _as_str(tag.get("name")).lstrip("#"),
            "url": _as_str(tag.get("href")),
        }
        for tag in obj.tags
        if isinstance(tag, dict)
        and tag.get("type") == "Hashtag"
        and _as_str(tag.get("name"))
    ]


def serialize_card(obj: AnyboxObject) -> dict | None:
    """Map stored OpenGraph metadata onto a Mastodon PreviewCard.

    `og_meta` is scraped and persisted when a status is created or received
    (`app/utils/opengraph.py`, called from `activitypub/boxes.py`), so this is
    pure re-serialization — no network access on the read path.

    It's a list (one entry per external link in the post) while Mastodon's
    `card` is singular, so the first usable entry wins — the same "first link"
    rule the web UI applies in `display_og_meta` (app/templates/utils.html).
    """
    for og_meta in obj.og_meta or []:
        if not isinstance(og_meta, dict):
            continue

        url = _as_str(og_meta.get("url"))
        title = _as_str(og_meta.get("title"))
        if not url or not title:
            continue

        image = _as_str(og_meta.get("image"))
        return {
            "url": url,
            "title": title,
            "description": _as_str(og_meta.get("description")),
            # `og:type` isn't scraped and there's no oEmbed lookup, so the
            # richer photo/video/rich variants aren't derivable here.
            "type": "link",
            "author_name": "",
            "author_url": "",
            "provider_name": _as_str(og_meta.get("site_name")),
            "provider_url": "",
            "html": "",
            # The scraper doesn't record image dimensions.
            "width": 0,
            "height": 0,
            # Proxied like every other remote media URL, so a client rendering
            # the card doesn't leak the reader's IP to the linked host.
            "image": proxied_media_url(image) if image else None,
            "embed_url": "",
            # Only computed for local uploads, never for scraped OG images.
            "blurhash": None,
        }

    return None


async def _serialize_quote(db_session: AsyncSession, obj: AnyboxObject) -> dict | None:
    """FEP-044f quote, mapped to Mastodon's `Quote` entity: `{state,
    quoted_status}`. `quoted_status` is only populated for an accepted
    quote -- a pending/rejected/unauthorized one has nothing to show beyond
    the `RE:` link already in the content, and Mastodon's own client
    doesn't render one either.
    """
    quote_ap_id = obj.quote_ap_id
    if not quote_ap_id:
        return None

    quoted_object = await _get_anybox_object(db_session, quote_ap_id)

    if isinstance(obj, activitypub.models.OutboxObject):
        state = obj.quote_state or "pending"
    else:
        state = "accepted" if obj.quote_is_verified else "unauthorized"

    if quoted_object is None:
        # Either never fetched, or gone since -- either way nothing to embed.
        state = "deleted" if state == "accepted" else state

    quoted_status = None
    if state == "accepted" and quoted_object is not None:
        quoted_status = await serialize_status(
            db_session, quoted_object, _resolve_reblog=False, _resolve_quote=False
        )

    return {"state": state, "quoted_status": quoted_status}


async def serialize_status(
    db_session: AsyncSession,
    obj: AnyboxObject,
    *,
    _resolve_reblog: bool = True,
    _resolve_quote: bool = True,
) -> dict:
    if isinstance(obj, activitypub.models.OutboxObject):
        status_id = ids.encode_outbox_id(obj)
        favourites_count = obj.likes_count
        reblogs_count = obj.announces_count
        bookmarked = False
        pinned = obj.is_pinned
        # send_like/send_announce only operate on inbox objects — liking or
        # boosting one's own post isn't a first-class flow this backend
        # tracks, so these stay false for the owner's own statuses.
        favourited = False
        reblogged = False
    else:
        status_id = ids.encode_inbox_id(obj)
        # We don't track how many likes/boosts a remote post received unless
        # embedded in its own AP object — 0 is honest "unknown", not a guess.
        favourites_count = 0
        reblogs_count = 0
        bookmarked = obj.is_bookmarked or False
        pinned = False
        favourited = bool(obj.liked_via_outbox_object_ap_id)
        reblogged = bool(obj.announced_via_outbox_object_ap_id)

    reblog = None
    if _resolve_reblog and obj.ap_type == "Announce" and obj.activity_object_ap_id:
        target = await _get_anybox_object(db_session, obj.activity_object_ap_id)
        if target is not None:
            reblog = await serialize_status(db_session, target, _resolve_reblog=False)

    quote = await _serialize_quote(db_session, obj) if _resolve_quote else None

    in_reply_to_id = None
    in_reply_to_account_id = None
    if obj.in_reply_to:
        parent = await _get_anybox_object(db_session, obj.in_reply_to)
        if parent is not None:
            in_reply_to_id = (
                ids.encode_outbox_id(parent)
                if isinstance(parent, activitypub.models.OutboxObject)
                else ids.encode_inbox_id(parent)
            )
            in_reply_to_account_id = ids.account_id_for_actor(parent.actor)

    created_at = obj.ap_published_at or _FALLBACK_CREATED_AT

    # `updated` is only ever set by an edit (send_update in activitypub/boxes.py
    # for our own statuses, or an incoming Update activity for a cached remote
    # one) — absent on every freshly-created object.
    updated_raw = obj.ap_object.get("updated")
    edited_at = format_datetime(parse_isoformat(updated_raw)) if updated_raw else None

    return {
        "id": status_id,
        "uri": obj.ap_id,
        "url": _as_str(obj.url, obj.ap_id),
        "created_at": format_datetime(created_at),
        "edited_at": edited_at,
        "account": await serialize_account(db_session, obj.actor),
        "content": _as_str(obj.content),
        "visibility": _VISIBILITY_MAP.get(
            obj.visibility or ap.VisibilityEnum.PUBLIC, "public"
        ),
        "sensitive": bool(obj.sensitive),
        "spoiler_text": _as_str(obj.summary),
        "media_attachments": _serialize_status_media_attachments(obj, status_id),
        "mentions": await _serialize_mentions(db_session, obj),
        "tags": _serialize_hashtags(obj),
        "emojis": [],
        "reblogs_count": reblogs_count,
        "favourites_count": favourites_count,
        "replies_count": obj.replies_count,
        "favourited": favourited,
        "reblogged": reblogged,
        "muted": await _is_conversation_muted(
            db_session, obj.conversation or obj.ap_id
        ),
        "bookmarked": bookmarked,
        "pinned": pinned,
        "reblog": reblog,
        "quote": quote,
        "in_reply_to_id": in_reply_to_id,
        "in_reply_to_account_id": in_reply_to_account_id,
        "poll": serialize_poll(obj, status_id),
        "card": serialize_card(obj),
        "language": _object_language(obj) or config.LANGUAGE_CODE,
        "text": None,
        "filtered": [],
    }


class _RevisionSnapshot(APObjectView):
    """Wraps one `OutboxObject.revisions[]` entry (or the object's own live
    state) in the same `Object` interface `serialize_status` uses, so
    `content`/`summary`/`sensitive`/`attachments` don't need reimplementing.
    """

    def __init__(self, ap_object: ap.RawObject, actor: BaseActor) -> None:
        self._ap_object = ap_object
        self._actor = actor

    @property
    def ap_object(self) -> ap.RawObject:
        return self._ap_object

    @property
    def actor(self) -> BaseActor:
        return self._actor


def serialize_status_edit(
    ap_object: ap.RawObject,
    created_at_raw: str | None,
    actor: BaseActor,
    account: dict,
    status_id: str,
) -> dict:
    snapshot = _RevisionSnapshot(ap_object, actor)
    created_at = (
        parse_isoformat(created_at_raw) if created_at_raw else _FALLBACK_CREATED_AT
    )

    return {
        "content": _as_str(snapshot.content),
        "spoiler_text": _as_str(snapshot.summary),
        "sensitive": bool(snapshot.sensitive),
        "created_at": format_datetime(created_at),
        "account": account,
        # Polls aren't editable through send_update — every revision shares
        # the same poll as the live status, so there's nothing distinct to
        # report per historical entry.
        "poll": None,
        "media_attachments": [
            serialize_media_attachment(attachment, index, status_id)
            for index, attachment in enumerate(snapshot.attachments)
        ],
        "emojis": [],
    }


def synthetic_filename(upload: activitypub.models.Upload) -> str:
    # `Upload` doesn't store the client's original filename (only content_hash
    # + content_type) — deterministically derive one instead of persisting it,
    # since it's only used to build the /attachments/... URL path (the actual
    # file lookup is by content_hash; the filename segment is unvalidated —
    # see app/main.py's serve_attachment/serve_attachment_thumbnail). Reused
    # by app/mastodon/router.py when attaching media to a new status.
    extension = mimetypes.guess_extension(upload.content_type or "") or ""
    return f"{upload.content_hash}{extension}"


def serialize_upload(upload: activitypub.models.Upload) -> dict:
    """Serialize a freshly-uploaded (not yet attached to any status) Upload
    to a Mastodon MediaAttachment. For already-attached media, see
    `serialize_media_attachment` instead.
    """
    filename = synthetic_filename(upload)
    url = f"{config.BASE_URL}/attachments/{upload.content_hash}/{filename}"
    duration = float(upload.duration) if upload.duration is not None else None

    mastodon_type = _mastodon_media_type(
        upload.content_type, url, duration=duration, has_audio=upload.has_audio
    )

    preview_url = (
        f"{config.BASE_URL}/attachments/thumbnails/{upload.content_hash}/{filename}"
        if upload.has_thumbnail
        else (None if mastodon_type in ("video", "audio", "gifv") else url)
    )

    return {
        "id": ids.encode_upload_id(upload),
        "type": mastodon_type,
        "url": url,
        "preview_url": preview_url,
        "remote_url": None,
        "meta": _media_meta(
            upload.width,
            upload.height,
            duration,
            bool(upload.has_thumbnail),
            focus=(
                (float(upload.focus_x), float(upload.focus_y))
                if upload.focus_x is not None and upload.focus_y is not None
                else None
            ),
        ),
        "description": upload.description,
        "blurhash": upload.blurhash,
    }


async def prefetch_scheduled_status_uploads(
    db_session: AsyncSession,
    rows: Iterable[models.ScheduledStatus],
) -> None:
    """Load every attachment referenced by a page of scheduled statuses at once.

    Same reason `prefetch_status_relations` exists: serializing a page row by
    row otherwise means one lookup per attachment. Nothing is returned — the
    single `IN` query puts the `Upload` rows in the session's identity map, so
    the per-row `db_session.get()` in `serialize_scheduled_status` resolves
    from memory instead of issuing SQL.
    """
    internal_ids = set()
    for row in rows:
        for media_id in scheduled_statuses.ComposeParams.from_json(
            row.params
        ).media_ids:
            internal_id = ids.decode_upload_id(media_id)
            if internal_id is not None:
                internal_ids.add(internal_id)

    if not internal_ids:
        return

    await db_session.scalars(
        select(activitypub.models.Upload).where(
            activitypub.models.Upload.id.in_(internal_ids)
        )
    )


async def serialize_scheduled_status(
    db_session: AsyncSession,
    scheduled_status: models.ScheduledStatus,
) -> dict:
    """Serialize a queued status to a Mastodon `ScheduledStatus`.

    `params` echoes the compose parameters back under Mastodon's own names
    (`text`, not `status`), with the nested `scheduled_at` always null — the
    time lives in the top-level field, matching upstream. `application_id` is
    always null: this server doesn't record which OAuth app authored a post.
    """
    params = scheduled_statuses.ComposeParams.from_json(scheduled_status.params)

    uploads = []
    for media_id in params.media_ids:
        upload = await ids.get_upload_by_mastodon_id(db_session, media_id)
        if upload is not None:
            uploads.append(upload)

    poll = None
    if params.poll_options:
        poll = {
            "options": params.poll_options,
            "expires_in": params.poll_expires_in or 3600,
            "multiple": params.poll_multiple,
        }

    return {
        "id": str(scheduled_status.id),
        "scheduled_at": format_datetime(scheduled_status.scheduled_at),
        "params": {
            "text": params.content,
            "poll": poll,
            "media_ids": params.media_ids or None,
            "sensitive": params.sensitive,
            "spoiler_text": params.content_warning,
            "visibility": _VISIBILITY_MAP.get(params.visibility, "public"),
            "language": params.language,
            "scheduled_at": None,
            "idempotency": params.idempotency,
            "with_rate_limit": False,
            "in_reply_to_id": params.in_reply_to_id,
            "application_id": None,
        },
        "media_attachments": [serialize_upload(upload) for upload in uploads],
    }


async def serialize_conversation(
    db_session: AsyncSession,
    last: AnyboxObject,
    actor_ids: set[int],
    unread: bool,
) -> dict:
    actors: list[activitypub.models.Actor] = []
    if actor_ids:
        actors = list(
            (
                await db_session.execute(
                    select(activitypub.models.Actor).where(
                        activitypub.models.Actor.id.in_(actor_ids)
                    )
                )
            ).scalars()
        )
    elif last.is_from_outbox:
        # A thread the owner started that hasn't been replied to yet has no
        # inbox rows to read participants off of — fall back to the mention
        # tags, like the HTML DM view does.
        mention_ap_ids = [
            tag["href"]
            for tag in last.tags
            if isinstance(tag, dict)
            and tag.get("type") == "Mention"
            and isinstance(tag.get("href"), str)
        ]
        if mention_ap_ids:
            actors = list(
                (
                    await db_session.execute(
                        select(activitypub.models.Actor).where(
                            activitypub.models.Actor.ap_id.in_(mention_ap_ids)
                        )
                    )
                ).scalars()
            )

    status_id = (
        ids.encode_outbox_id(last)
        if isinstance(last, activitypub.models.OutboxObject)
        else ids.encode_inbox_id(last)
    )

    return {
        "id": status_id,
        "unread": unread,
        "accounts": [await serialize_account(db_session, actor) for actor in actors],
        "last_status": await serialize_status(db_session, last),
    }
