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

import mimetypes
from datetime import datetime
from datetime import timezone
from urllib.parse import urlparse

from sqlalchemy import func
from sqlalchemy import select

import activitypub.models
from activitypub import activitypub as ap
from activitypub.actor import LOCAL_ACTOR
from activitypub.actor import Actor as BaseActor
from activitypub.ap_object import Attachment
from activitypub.boxes import AnyboxObject
from activitypub.boxes import get_anybox_object_by_ap_id
from activitypub.boxes import public_outbox_objects_count
from app import config
from app.database import AsyncSession
from app.mastodon import ids

# The actor keypair is generated once, during initial setup, and never
# rotated — its mtime is a reasonable proxy for "when this instance/account
# was created" in the absence of any stored value to that effect.
_FALLBACK_CREATED_AT = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _format_datetime(dt: datetime) -> str:
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
    followers_count = (
        await db_session.scalar(select(func.count(activitypub.models.Follower.id))) or 0
    )
    following_count = (
        await db_session.scalar(select(func.count(activitypub.models.Following.id)))
        or 0
    )
    statuses_count = await public_outbox_objects_count(db_session)
    return followers_count, following_count, statuses_count


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
        # We don't fetch a remote actor's own follower/following/statuses
        # counts live — 0 is an honest "unknown", not a stale guess.
        followers_count = following_count = statuses_count = 0
    else:
        account_id = ids.LOCAL_ACTOR_ID
        acct = actor.preferred_username
        created_at = _owner_created_at()
        locked = config.MANUALLY_APPROVES_FOLLOWERS
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
        "discoverable": True,
        "group": False,
        "created_at": _format_datetime(created_at),
        "note": _as_str(actor.summary),
        # `actor.url` can be a Link dict/list on some servers; coerce to a
        # string, falling back to the actor's id.
        "url": _as_str(actor.url, actor.ap_id),
        "uri": actor.ap_id,
        "avatar": _as_str(actor.resized_icon_url),
        "avatar_static": _as_str(actor.icon_url),
        "header": _as_str(actor.image_url),
        "header_static": _as_str(actor.image_url),
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


def _media_type_category(attachment: Attachment) -> str:
    media_type = attachment.media_type
    if not media_type:
        media_type, _ = mimetypes.guess_type(attachment.url)
    if not media_type:
        return "unknown"
    top_level = media_type.split("/", 1)[0]
    return top_level if top_level in ("image", "video", "audio") else "unknown"


def serialize_media_attachment(
    attachment: Attachment, index: int, status_id: str
) -> dict:
    url = attachment.proxied_url or attachment.url
    meta = {}
    if attachment.width and attachment.height:
        meta["original"] = {"width": attachment.width, "height": attachment.height}

    return {
        # Not independently addressable in this backend (no separate media
        # lookup for already-attached media) — scoped to the parent status.
        "id": f"{status_id}-{index}",
        "type": _media_type_category(attachment),
        "url": url,
        "preview_url": attachment.resized_url or url,
        "remote_url": attachment.url,
        "meta": meta,
        "description": attachment.name,
        "blurhash": None,
    }


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

    return {
        "id": status_id,
        "expires_at": (
            _format_datetime(obj.poll_end_time) if obj.poll_end_time else None
        ),
        "expired": obj.is_poll_ended,
        "multiple": not obj.is_one_of_poll,
        "votes_count": obj.poll_voters_count or 0,
        "voters_count": obj.poll_voters_count,
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
        if actor:
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
            name = _as_str(tag.get("name")).lstrip("@")
            mentions.append({"id": "", "username": name, "url": href, "acct": name})

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


async def serialize_status(
    db_session: AsyncSession,
    obj: AnyboxObject,
    *,
    _resolve_reblog: bool = True,
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
        target = await get_anybox_object_by_ap_id(db_session, obj.activity_object_ap_id)
        if target is not None:
            reblog = await serialize_status(db_session, target, _resolve_reblog=False)

    in_reply_to_id = None
    in_reply_to_account_id = None
    if obj.in_reply_to:
        parent = await get_anybox_object_by_ap_id(db_session, obj.in_reply_to)
        if parent is not None:
            in_reply_to_id = (
                ids.encode_outbox_id(parent)
                if isinstance(parent, activitypub.models.OutboxObject)
                else ids.encode_inbox_id(parent)
            )
            in_reply_to_account_id = ids.account_id_for_actor(parent.actor)

    created_at = obj.ap_published_at or _FALLBACK_CREATED_AT

    return {
        "id": status_id,
        "uri": obj.ap_id,
        "url": _as_str(obj.url, obj.ap_id),
        "created_at": _format_datetime(created_at),
        "edited_at": None,
        "account": await serialize_account(db_session, obj.actor),
        "content": _as_str(obj.content),
        "visibility": _VISIBILITY_MAP.get(
            obj.visibility or ap.VisibilityEnum.PUBLIC, "public"
        ),
        "sensitive": bool(obj.sensitive),
        "spoiler_text": _as_str(obj.summary),
        "media_attachments": [
            serialize_media_attachment(attachment, index, status_id)
            for index, attachment in enumerate(obj.attachments)
        ],
        "mentions": await _serialize_mentions(db_session, obj),
        "tags": _serialize_hashtags(obj),
        "emojis": [],
        "reblogs_count": reblogs_count,
        "favourites_count": favourites_count,
        "replies_count": obj.replies_count,
        "favourited": favourited,
        "reblogged": reblogged,
        "muted": False,
        "bookmarked": bookmarked,
        "pinned": pinned,
        "reblog": reblog,
        "in_reply_to_id": in_reply_to_id,
        "in_reply_to_account_id": in_reply_to_account_id,
        "poll": serialize_poll(obj, status_id),
        "card": None,
        "language": _object_language(obj) or config.LANGUAGE_CODE,
        "text": None,
        "filtered": [],
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
    preview_url = (
        f"{config.BASE_URL}/attachments/thumbnails/{upload.content_hash}/{filename}"
        if upload.has_thumbnail
        else url
    )

    media_type = upload.content_type or ""
    top_level = media_type.split("/", 1)[0]
    mastodon_type = top_level if top_level in ("image", "video", "audio") else "unknown"

    meta = {}
    if upload.width and upload.height:
        meta["original"] = {"width": upload.width, "height": upload.height}

    return {
        "id": ids.encode_upload_id(upload),
        "type": mastodon_type,
        "url": url,
        "preview_url": preview_url,
        "remote_url": None,
        "meta": meta,
        "description": upload.description,
        "blurhash": upload.blurhash,
    }
