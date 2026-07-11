"""Mastodon entity serializers.

`serialize_account` handles the two kinds of actor this server ever needs to
serialize: the local owner (`activitypub.actor.LOCAL_ACTOR`, not a DB row —
see `app/mastodon/ids.py`) and a cached remote actor (a
`activitypub.models.Actor` row). Serializing an arbitrary not-yet-cached
`RemoteActor` wrapper (e.g. a live search resolve) is deferred to PR-3.
"""

from datetime import datetime
from datetime import timezone
from urllib.parse import urlparse

from sqlalchemy import func
from sqlalchemy import select

import activitypub.models
from activitypub.actor import LOCAL_ACTOR
from activitypub.actor import Actor as BaseActor
from activitypub.boxes import public_outbox_objects_count
from app import config
from app.database import AsyncSession
from app.mastodon import ids

# The actor keypair is generated once, during initial setup, and never
# rotated — its mtime is a reasonable proxy for "when this instance/account
# was created" in the absence of any stored value to that effect.
_FALLBACK_CREATED_AT = datetime(1970, 1, 1, tzinfo=timezone.utc)


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
            "name": item.get("name", ""),
            "value": item.get("value", ""),
            "verified_at": None,
        }
        for item in actor.attachments
        if item.get("type") == "PropertyValue"
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
        "username": actor.preferred_username,
        "acct": acct,
        "display_name": actor.display_name,
        "locked": locked,
        "bot": actor.ap_type == "Service",
        "discoverable": True,
        "group": False,
        "created_at": created_at.replace(tzinfo=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "note": actor.summary or "",
        "url": actor.url or actor.ap_id,
        "uri": actor.ap_id,
        "avatar": actor.resized_icon_url or "",
        "avatar_static": actor.icon_url or "",
        "header": actor.image_url or "",
        "header_static": actor.image_url or "",
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
