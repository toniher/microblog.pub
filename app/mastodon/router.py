"""Mastodon client REST API — /api/v1 and /api/v2 endpoints.

Grown incrementally across build phases; see PLAN-0.md for the full map.
This module currently covers Phase 0's instance/meta surface, Phase 1a's
accounts/relationships surface, Phase 1b's timelines/statuses surface,
Phase 1c's notifications + read-degradation surface, Phase 2a's media
upload surface, Phase 2b's status-write surface, and Phase 3's social
graph + search surface.
"""

import asyncio
import re
from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import Sequence
from typing import cast
from urllib.parse import urlparse

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi import UploadFile as FastAPIUploadFile
from loguru import logger
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.orm import joinedload
from starlette.datastructures import UploadFile
from starlette.responses import JSONResponse

import activitypub.models
from activitypub import activitypub as ap
from activitypub.actor import RemoteActor
from activitypub.actor import fetch_actor
from activitypub.actor import get_actors_metadata
from activitypub.actor import mute_actor
from activitypub.actor import refresh_actor_counts
from activitypub.actor import unmute_actor
from activitypub.ap_object import RemoteObject
from activitypub.boxes import AnyboxObject
from activitypub.boxes import ReplyTreeNode
from activitypub.boxes import fetch_replies
from activitypub.boxes import get_anybox_object_by_ap_id
from activitypub.boxes import get_replies_tree
from activitypub.boxes import prefetch_actor_outbox
from activitypub.boxes import remove_follower
from activitypub.boxes import save_object_to_inbox
from activitypub.boxes import send_accept
from activitypub.boxes import send_announce
from activitypub.boxes import send_block
from activitypub.boxes import send_delete
from activitypub.boxes import send_follow
from activitypub.boxes import send_like
from activitypub.boxes import send_reject
from activitypub.boxes import send_unblock
from activitypub.boxes import send_undo
from activitypub.boxes import send_update
from activitypub.boxes import send_vote
from app import config
from app import models
from app import scheduled_statuses
from app.database import AsyncSession
from app.database import get_db_session
from app.indieauth import AccessTokenInfo
from app.indieauth import check_access_token
from app.lookup import lookup
from app.mastodon import ids
from app.mastodon import pagination
from app.mastodon import serializers
from app.mastodon import streaming
from app.mastodon import timelines
from app.mastodon.errors import MastodonError
from app.mastodon.scopes import require_scope
from app.uploads import IncompatibleMediaError
from app.uploads import UploadTooLargeError
from app.uploads import save_upload
from app.utils.datetime import as_utc
from app.utils.datetime import now
from app.utils.datetime import parse_isoformat
from app.utils.emoji import EMOJIS
from app.utils.search_text import glob_pattern
from app.utils.search_text import normalize
from app.utils.url import InvalidURLError
from app.utils.url import check_url_async
from app.webpush import decode_client_key
from app.webpush import parse_auth_secret
from app.webpush import parse_p256dh
from app.webpush import vapid_public_key_b64

router = APIRouter()

# The size limits below are real (enforced in app/uploads.py's save_upload,
# reading the same app.config constants) — everything else here is an
# advisory client hint only. The backend has no hard cap on note/article
# length, so max_characters is set generously rather than mirroring
# Mastodon's 500.
# Poll limits. Named constants rather than literals inside
# `_INSTANCE_CONFIGURATION` below because `POST /api/v1/statuses` enforces the
# same numbers: a client reads these off `/api/v1/instance` to build its poll
# composer, so what's advertised and what's accepted have to be the same values,
# not two copies that can drift. Mastodon's own defaults.
_POLL_MAX_OPTIONS = 4
_POLL_MAX_CHARACTERS_PER_OPTION = 100
_POLL_MIN_EXPIRATION = 300
_POLL_MAX_EXPIRATION = 2_629_746

_INSTANCE_CONFIGURATION = {
    "statuses": {
        "max_characters": 100_000,
        "max_media_attachments": 4,
        "characters_reserved_per_url": 23,
        "max_pinned_statuses": activitypub.models.MAX_PINNED_OBJECTS,
    },
    "media_attachments": {
        "supported_mime_types": [
            "image/jpeg",
            "image/png",
            "image/gif",
            "image/webp",
            # video/quicktime is deliberately absent: the compatibility
            # classifier (app/ffmpeg.py) rejects the `qt  ` container brand.
            "video/mp4",
            "video/webm",
            "video/ogg",
            "audio/mpeg",
            "audio/mp4",
            "audio/ogg",
            "audio/wav",
            "audio/x-wav",
            "audio/webm",
            "audio/flac",
            "audio/aac",
        ],
        "image_size_limit": config.MAX_IMAGE_UPLOAD_SIZE,
        "image_matrix_limit": config.MAX_IMAGE_PIXELS,
        "video_size_limit": config.MAX_VIDEO_UPLOAD_SIZE,
        "video_frame_rate_limit": 60,
        "video_matrix_limit": 2_304_000,
    },
    "polls": {
        "max_options": _POLL_MAX_OPTIONS,
        "max_characters_per_option": _POLL_MAX_CHARACTERS_PER_OPTION,
        "min_expiration": _POLL_MIN_EXPIRATION,
        "max_expiration": _POLL_MAX_EXPIRATION,
    },
}

# Mirrors pyproject.toml's [tool.poetry].repository; not parsed dynamically
# since it's a cosmetic field only shown on some clients' "about" screens.
_SOURCE_URL = "https://github.com/toniher/microblog.pub"

# The Mastodon version this API surface is compatible with, reported in the
# leading slot of `version` on /api/v1/instance and /api/v2/instance. Clients
# parse that number to decide which features to *offer*, so it must describe
# the API, not this software: interpolating microblog.pub's own version here
# (which is what used to happen) silently told clients we were Mastodon 2.x
# and made them hide features that work fine — editing a status needs 3.5,
# bookmarks 3.1, markers 3.0, /api/v2/instance 4.0. It would also have drifted
# on its own the moment microblog.pub reached 3.x.
#
# 4.3.0 is the highest version whose gated features are all either implemented
# or degrade gracefully here. Raising it is a deliberate act: check what the
# new gate makes clients *expect*. Two known consequences of 4.3 itself:
# clients prefer `GET /api/v2/notifications` (not implemented — the 404 is
# what makes them fall back to v1, so do not stub it empty), and every
# Notification must carry `group_key` (it does, see serializers.py).
#
# Deliberately *not* advertising `api_versions` (Instance, 4.3.0+): it's an
# opaque fast-moving counter — mastodon.social on 4.7.0 reports 11 — with no
# published mapping from version to value, so any number here would be a
# guess that clients act on. Omitting it makes them fall back to parsing
# `version`, which the constant above now states correctly.
_MASTODON_COMPAT_VERSION = "4.3.0"
_VERSION_STRING = (
    f"{_MASTODON_COMPAT_VERSION} (compatible; microblogpub {config.VERSION})"
)


@router.get("/api/v1/instance", response_model=None)
async def instance_v1(
    db_session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    owner_account = await serializers.serialize_owner_account(db_session)

    return JSONResponse(
        content={
            "uri": config.DOMAIN,
            "title": config.CONFIG.name,
            "short_description": config.CONFIG.summary,
            "description": config.CONFIG.summary,
            "email": config.CONFIG.contact_email or "",
            "version": _VERSION_STRING,
            "urls": (
                {"streaming_api": streaming_url}
                if (streaming_url := streaming.streaming_base_url())
                else {}
            ),
            "stats": {
                "user_count": 1,
                "status_count": owner_account["statuses_count"],
                "domain_count": 1,
            },
            "thumbnail": config.IMAGE_URL or config.ICON_URL,
            "languages": [config.LANGUAGE_CODE],
            "registrations": False,
            "approval_required": False,
            "invites_enabled": False,
            "configuration": {
                **_INSTANCE_CONFIGURATION,
                "vapid": {"public_key": vapid_public_key_b64()},
            },
            "contact_account": owner_account,
            "rules": [],
        },
        status_code=200,
    )


@router.get("/api/v2/instance", response_model=None)
async def instance_v2(
    db_session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    owner_account = await serializers.serialize_owner_account(db_session)
    thumbnail_url = config.IMAGE_URL or config.ICON_URL

    return JSONResponse(
        content={
            "domain": config.DOMAIN,
            "title": config.CONFIG.name,
            "version": _VERSION_STRING,
            "source_url": _SOURCE_URL,
            "description": config.CONFIG.summary,
            "usage": {"users": {"active_month": 1}},
            "thumbnail": {
                "url": thumbnail_url,
                "blurhash": None,
                "versions": {},
            },
            "languages": [config.LANGUAGE_CODE],
            "configuration": {
                **_INSTANCE_CONFIGURATION,
                "vapid": {"public_key": vapid_public_key_b64()},
                "urls": (
                    {"streaming": streaming_url}
                    if (streaming_url := streaming.streaming_base_url())
                    else {}
                ),
            },
            "registrations": {
                "enabled": False,
                "approval_required": False,
                "message": None,
            },
            "contact": {
                "email": config.CONFIG.contact_email or "",
                "account": owner_account,
            },
            "rules": [],
        },
        status_code=200,
    )


@router.get("/api/v1/instance/rules", response_model=None)
async def instance_rules() -> JSONResponse:
    return JSONResponse(content=[], status_code=200)


@router.get("/api/v1/instance/extended_description", response_model=None)
async def instance_extended_description() -> JSONResponse:
    return JSONResponse(
        content=serializers.serialize_extended_description(), status_code=200
    )


@router.get("/api/v1/instance/peers", response_model=None)
async def instance_peers() -> JSONResponse:
    # Deliberately empty rather than the real federated-peers list: exposing
    # who you've federated with is a privacy tradeoff (and a ready-made probe
    # target), not just a data-availability gap, so this instance opts out.
    return JSONResponse(content=[], status_code=200)


@router.get("/api/v1/instance/domain_blocks", response_model=None)
async def instance_domain_blocks() -> JSONResponse:
    return JSONResponse(
        content=serializers.serialize_instance_domain_blocks(), status_code=200
    )


_ACTIVITY_WEEKS = 12


def _week_start(dt: datetime) -> datetime:
    return (dt - timedelta(days=dt.isoweekday() - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


@router.get("/api/v1/instance/activity", response_model=None)
async def instance_activity(
    db_session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    """Weekly stats for the last `_ACTIVITY_WEEKS` weeks, most recent first.

    `registrations` is always 0 (registration is disabled on a single-user
    instance). `logins` counts new `IndieAuthAccessToken` rows — the same
    OAuth flow backs every client login here, so a fresh token is a login.
    """
    current_week_start = _week_start(now())
    weeks = []
    for offset in range(_ACTIVITY_WEEKS):
        week_start = current_week_start - timedelta(weeks=offset)
        week_end = week_start + timedelta(weeks=1)
        statuses_count = await db_session.scalar(
            select(func.count(activitypub.models.OutboxObject.id)).where(
                activitypub.models.OutboxObject.visibility == ap.VisibilityEnum.PUBLIC,
                activitypub.models.OutboxObject.is_deleted.is_(False),
                activitypub.models.OutboxObject.ap_published_at >= week_start,
                activitypub.models.OutboxObject.ap_published_at < week_end,
            )
        )
        logins_count = await db_session.scalar(
            select(func.count(models.IndieAuthAccessToken.id)).where(
                models.IndieAuthAccessToken.created_at >= week_start,
                models.IndieAuthAccessToken.created_at < week_end,
            )
        )
        weeks.append(
            {
                "week": str(int(week_start.timestamp())),
                "statuses": str(statuses_count or 0),
                "logins": str(logins_count or 0),
                "registrations": "0",
            }
        )
    return JSONResponse(content=weeks, status_code=200)


@router.get("/api/v1/custom_emojis", response_model=None)
async def custom_emojis() -> JSONResponse:
    return JSONResponse(
        content=[
            {
                "shortcode": ap_emoji["name"].strip(":"),
                "url": ap_emoji["icon"]["url"],
                "static_url": ap_emoji["icon"]["url"],
                "visible_in_picker": True,
                "category": None,
            }
            for ap_emoji in EMOJIS.values()
        ],
        status_code=200,
    )


@router.get("/api/v1/preferences", response_model=None)
async def preferences(
    token_info: AccessTokenInfo = Depends(require_scope("read")),
) -> JSONResponse:
    return JSONResponse(
        content={
            "posting:default:visibility": "public",
            "posting:default:sensitive": False,
            "posting:default:language": None,
            "reading:expand:media": "default",
            "reading:expand:spoilers": False,
        },
        status_code=200,
    )


@router.get("/api/v1/announcements", response_model=None)
async def announcements(
    token_info: AccessTokenInfo = Depends(require_scope("read")),
) -> JSONResponse:
    return JSONResponse(content=[], status_code=200)


_MARKER_TIMELINES = ("home", "notifications")


def _serialize_marker(marker: models.Marker) -> dict:
    return {
        "last_read_id": marker.last_read_id,
        "version": marker.version,
        "updated_at": serializers.format_datetime(marker.updated_at or now()),
    }


@router.get("/api/v1/markers", response_model=None)
async def get_markers(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("read:statuses")),
) -> JSONResponse:
    requested = request.query_params.getlist("timeline[]") or list(_MARKER_TIMELINES)
    markers = (
        await db_session.scalars(
            select(models.Marker).where(models.Marker.timeline.in_(requested))
        )
    ).all()
    return JSONResponse(
        content={marker.timeline: _serialize_marker(marker) for marker in markers},
        status_code=200,
    )


@router.post("/api/v1/markers", response_model=None)
async def post_markers(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:statuses")),
) -> JSONResponse:
    form_data = await request.form()

    content = {}
    for timeline in _MARKER_TIMELINES:
        last_read_id = form_data.get(f"{timeline}[last_read_id]")
        if last_read_id is None:
            continue

        marker = (
            await db_session.scalars(
                select(models.Marker).where(models.Marker.timeline == timeline)
            )
        ).one_or_none()
        if marker is None:
            marker = models.Marker(timeline=timeline)
            db_session.add(marker)

        marker.last_read_id = str(last_read_id)
        marker.version = (marker.version or 0) + 1
        marker.updated_at = now()
        content[timeline] = marker

    await db_session.commit()

    return JSONResponse(
        content={
            timeline: _serialize_marker(marker) for timeline, marker in content.items()
        },
        status_code=200,
    )


# --- Accounts + relationships -----------------------------------------------
# Static-path routes (verify_credentials/relationships/lookup) are registered
# before the dynamic "/{account_id}" ones below so FastAPI doesn't try to
# match them as an account id.


async def _pending_follow_requests_count(db_session: AsyncSession) -> int:
    return await db_session.scalar(
        select(func.count())
        .select_from(models.Notification)
        .where(
            models.Notification.notification_type
            == models.NotificationType.PENDING_INCOMING_FOLLOWER,
            models.Notification.is_accepted.is_(None),
            models.Notification.is_rejected.is_(None),
        )
    )


@router.get("/api/v1/accounts/verify_credentials", response_model=None)
async def accounts_verify_credentials(
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("read:accounts")),
) -> JSONResponse:
    account = await serializers.serialize_owner_account(db_session)
    account["source"] = {
        "privacy": "public",
        "sensitive": False,
        "language": config.LANGUAGE_CODE,
        "note": account["note"],
        "fields": account["fields"],
        "follow_requests_count": await _pending_follow_requests_count(db_session),
    }
    return JSONResponse(content=account, status_code=200)


@router.get("/api/v1/accounts", response_model=None)
async def accounts_index(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("read:accounts")),
) -> JSONResponse:
    # Mastodon's "view multiple profiles" endpoint (GET /api/v1/accounts?
    # id[]=1&id[]=2) — mastodon-ios fetches this when loading a profile,
    # including the signed-in user's own. Unknown ids are silently skipped
    # rather than 404ing the whole batch.
    raw_ids = request.query_params.getlist("id[]") or request.query_params.getlist("id")

    accounts = []
    for raw_id in raw_ids:
        if raw_id == ids.LOCAL_ACTOR_ID:
            accounts.append(await serializers.serialize_owner_account(db_session))
            continue

        actor = await ids.get_account_by_mastodon_id(db_session, raw_id)
        if actor is not None:
            accounts.append(await serializers.serialize_account(db_session, actor))

    return JSONResponse(content=accounts, status_code=200)


def _serialize_relationship(
    account_id: str,
    actor: activitypub.models.Actor | None,
    meta,
) -> dict:
    if actor is None:
        # LOCAL_ACTOR_ID sentinel — a relationship with yourself is
        # trivially all-false; there's no metadata to look up.
        return {
            "id": account_id,
            "following": False,
            "showing_reblogs": True,
            "notifying": False,
            "followed_by": False,
            "blocking": False,
            "blocked_by": False,
            "muting": False,
            "muting_notifications": False,
            "requested": False,
            "domain_blocking": False,
            "endorsed": False,
            "note": "",
        }
    return {
        "id": account_id,
        "following": meta.is_following if meta else False,
        "showing_reblogs": not actor.are_announces_hidden_from_stream,
        "notifying": actor.are_new_posts_notified,
        "followed_by": meta.is_follower if meta else False,
        "blocking": actor.is_blocked,
        "blocked_by": meta.has_blocked_local_actor if meta else False,
        "muting": actor.is_muted_now,
        "muting_notifications": actor.are_notifications_muted_now,
        "requested": meta.is_follow_request_sent if meta else False,
        "domain_blocking": False,
        "endorsed": False,
        "note": actor.note or "",
    }


async def _relationship_for_actor(
    db_session: AsyncSession, account_id: str, actor: activitypub.models.Actor
) -> dict:
    metadata = await get_actors_metadata(db_session, [actor])
    return _serialize_relationship(account_id, actor, metadata.get(actor.ap_id))


@router.get("/api/v1/accounts/relationships", response_model=None)
async def accounts_relationships(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("read:accounts")),
) -> JSONResponse:
    raw_ids = request.query_params.getlist("id[]") or request.query_params.getlist("id")

    relationships = []
    remote_actors_by_raw_id: dict[str, activitypub.models.Actor] = {}

    for raw_id in raw_ids:
        if raw_id == ids.LOCAL_ACTOR_ID:
            relationships.append(
                _serialize_relationship(ids.LOCAL_ACTOR_ID, None, None)
            )
            continue

        actor = await ids.get_account_by_mastodon_id(db_session, raw_id)
        if actor is not None:
            remote_actors_by_raw_id[raw_id] = actor

    if remote_actors_by_raw_id:
        metadata = await get_actors_metadata(
            db_session, list(remote_actors_by_raw_id.values())
        )
        for raw_id, actor in remote_actors_by_raw_id.items():
            relationships.append(
                _serialize_relationship(raw_id, actor, metadata.get(actor.ap_id))
            )

    return JSONResponse(content=relationships, status_code=200)


@router.get("/api/v1/accounts/lookup", response_model=None)
async def accounts_lookup(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    acct = request.query_params.get("acct", "").lstrip("@")
    if not acct:
        raise MastodonError(400, "invalid_request", "acct is required")

    if acct in (config.USERNAME, f"{config.USERNAME}@{config.WEBFINGER_DOMAIN}"):
        return JSONResponse(
            content=await serializers.serialize_owner_account(db_session),
            status_code=200,
        )

    if "@" not in acct:
        raise MastodonError(404, "not_found", "account not found")
    username, _, host = acct.partition("@")

    # DB-only: we don't live-fetch/webfinger unknown actors here. PR-3's
    # search (resolve=true) covers that case; this only finds actors already
    # cached from prior federation activity.
    known_actors = (await db_session.scalars(select(activitypub.models.Actor))).all()
    match = next(
        (
            actor
            for actor in known_actors
            if actor.preferred_username == username
            and urlparse(actor.ap_id).netloc == host
        ),
        None,
    )
    if match is None:
        raise MastodonError(404, "not_found", "account not found")

    return JSONResponse(
        content=await serializers.serialize_account(db_session, match),
        status_code=200,
    )


@router.get("/api/v1/accounts/familiar_followers", response_model=None)
async def accounts_familiar_followers(
    request: Request,
    token_info: AccessTokenInfo = Depends(require_scope("read:accounts")),
) -> JSONResponse:
    # This must be registered before /api/v1/accounts/{account_id} — otherwise
    # that route swallows this path (account_id="familiar_followers") and 404s,
    # which is what clients like Ice Cubes see when loading a profile, in turn
    # blanking the whole profile view including the account's own statuses.
    # Single-user instance: there's no concept of mutual/familiar followers.
    raw_ids = request.query_params.getlist("id[]") or request.query_params.getlist("id")

    return JSONResponse(
        content=[{"id": raw_id, "accounts": []} for raw_id in raw_ids],
        status_code=200,
    )


@router.get("/api/v1/accounts/{account_id}", response_model=None)
async def accounts_show(
    account_id: str,
    db_session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    if account_id == ids.LOCAL_ACTOR_ID:
        return JSONResponse(
            content=await serializers.serialize_owner_account(db_session),
            status_code=200,
        )

    actor = await ids.get_account_by_mastodon_id(db_session, account_id)
    if actor is None:
        raise MastodonError(404, "not_found", "account not found")

    if _should_refresh_counts(actor):
        try:
            await refresh_actor_counts(actor)
            await db_session.commit()
        except Exception:
            await db_session.rollback()
            logger.exception(f"Failed to refresh counts for {actor.ap_id}")

    return JSONResponse(
        content=await serializers.serialize_account(db_session, actor),
        status_code=200,
    )


async def _respond_with_status_list(
    request: Request, db_session: AsyncSession, objects: list
) -> JSONResponse:
    await serializers.prefetch_status_relations(db_session, objects)
    statuses = [await serializers.serialize_status(db_session, obj) for obj in objects]
    response = JSONResponse(content=statuses, status_code=200)
    link_header = pagination.build_link_header(
        request, [status["id"] for status in statuses]
    )
    if link_header:
        response.headers["Link"] = link_header
    return response


@router.get("/api/v1/accounts/{account_id}/statuses", response_model=None)
async def accounts_statuses(
    account_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    params = pagination.parse_pagination(request)
    exclude_replies = request.query_params.get("exclude_replies") == "true"
    pinned_only = request.query_params.get("pinned") == "true"
    # Only the owner's own authenticated session may see non-public posts
    # here (e.g. followers-only or direct posts/DMs); everyone else gets the
    # public-facing view, matching statuses_show's visibility gate.
    is_admin = await _is_authenticated_admin(request, db_session)
    allowed_visibility = (
        list(ap.VisibilityEnum)
        if is_admin
        else [ap.VisibilityEnum.PUBLIC, ap.VisibilityEnum.UNLISTED]
    )

    if account_id == ids.LOCAL_ACTOR_ID:
        query = (
            select(activitypub.models.OutboxObject)
            .where(
                activitypub.models.OutboxObject.is_deleted.is_(False),
                # Must include "Announce" — otherwise the owner's own boosts
                # never appear on their own profile, unlike a remote actor's
                # (below), which already lists it.
                activitypub.models.OutboxObject.ap_type.in_(
                    timelines.TIMELINE_OBJECT_TYPES
                ),
                activitypub.models.OutboxObject.visibility.in_(allowed_visibility),
            )
            .options(
                joinedload(
                    activitypub.models.OutboxObject.outbox_object_attachments
                ).joinedload(activitypub.models.OutboxObjectAttachment.upload)
            )
            .order_by(activitypub.models.OutboxObject.id.desc())
            .limit(params.limit)
        )
        if pinned_only:
            query = query.where(activitypub.models.OutboxObject.is_pinned.is_(True))
        if exclude_replies:
            query = query.where(
                activitypub.models.OutboxObject.is_hidden_from_homepage.is_(False)
            )
        if params.max_id:
            decoded = ids.decode_object_id_for_source(
                params.max_id, ids.ObjectSource.OUTBOX
            )
            if decoded is not None:
                query = query.where(activitypub.models.OutboxObject.id < decoded)
        cursor = params.min_id or params.since_id
        if cursor:
            decoded = ids.decode_object_id_for_source(cursor, ids.ObjectSource.OUTBOX)
            if decoded is not None:
                query = query.where(activitypub.models.OutboxObject.id > decoded)

        items = (await db_session.scalars(query)).unique().all()
        return await _respond_with_status_list(request, db_session, items)

    actor = await ids.get_account_by_mastodon_id(db_session, account_id)
    if actor is None:
        raise MastodonError(404, "not_found", "account not found")

    if pinned_only:
        # We don't track pins on a remote actor's own posts.
        return JSONResponse(content=[], status_code=200)

    # Captured once up front: a failed backfill attempt below rolls back the
    # session, which expires every loaded ORM object (including `actor`), so
    # any later attribute access would need an unawaited implicit reload and
    # crash with MissingGreenlet in this async session.
    actor_ap_id = actor.ap_id

    def _build_query() -> Any:
        query = (
            select(activitypub.models.InboxObject)
            .where(
                activitypub.models.InboxObject.ap_actor_id == actor_ap_id,
                activitypub.models.InboxObject.is_deleted.is_(False),
                activitypub.models.InboxObject.ap_type.in_(
                    timelines.TIMELINE_OBJECT_TYPES
                ),
                activitypub.models.InboxObject.visibility.in_(allowed_visibility),
            )
            .options(joinedload(activitypub.models.InboxObject.actor))
            .order_by(activitypub.models.InboxObject.id.desc())
            .limit(params.limit)
        )
        if exclude_replies:
            query = query.where(
                activitypub.models.InboxObject.is_hidden_from_stream.is_(False)
            )
        if params.max_id:
            decoded = ids.decode_object_id_for_source(
                params.max_id, ids.ObjectSource.INBOX
            )
            if decoded is not None:
                query = query.where(activitypub.models.InboxObject.id < decoded)
        cursor = params.min_id or params.since_id
        if cursor:
            decoded = ids.decode_object_id_for_source(cursor, ids.ObjectSource.INBOX)
            if decoded is not None:
                query = query.where(activitypub.models.InboxObject.id > decoded)
        return query

    items = (await db_session.scalars(_build_query())).unique().all()

    # We only cache a remote actor's posts as they arrive in our inbox (via a
    # follow or a reply/boost from someone we follow). For an actor we've
    # never interacted with, that means nothing to show here. Best-effort
    # backfill their outbox on demand (throttled) so clients like Tusky/Ice
    # Cubes don't render a broken-looking empty profile.
    if not items and _should_backfill_outbox(actor):
        try:
            await prefetch_actor_outbox(db_session, actor)
            await db_session.commit()
        except Exception:
            # A concurrent request (multiple clients polling the same
            # not-yet-cached profile) or a normal incoming delivery may have
            # raced us and already saved the same post — rollback first,
            # since the session is unusable until we do, then re-query
            # either way: on a unique-constraint failure the post is already
            # there under the other writer's commit.
            await db_session.rollback()
            logger.exception(f"Failed to backfill outbox for {actor_ap_id}")
        items = (await db_session.scalars(_build_query())).unique().all()

    return await _respond_with_status_list(request, db_session, items)


_OUTBOX_BACKFILL_TTL = timedelta(hours=1)


def _should_backfill_outbox(actor: activitypub.models.Actor) -> bool:
    if actor.outbox_backfilled_at is None:
        return True
    return now() - as_utc(actor.outbox_backfilled_at) > _OUTBOX_BACKFILL_TTL


_ACTOR_COUNTS_TTL = timedelta(hours=1)


def _should_refresh_counts(actor: activitypub.models.Actor) -> bool:
    if actor.counts_refreshed_at is None:
        return True
    return now() - as_utc(actor.counts_refreshed_at) > _ACTOR_COUNTS_TTL


def _apply_account_cursor(query, params: pagination.PaginationParams):
    """Apply max_id/since_id/min_id to a query ordered by `Actor.id` desc."""
    if params.max_id:
        decoded = ids.decode_account_id(params.max_id)
        if decoded is not None:
            query = query.where(activitypub.models.Actor.id < decoded)

    cursor = params.min_id or params.since_id
    if cursor:
        decoded = ids.decode_account_id(cursor)
        if decoded is not None:
            query = query.where(activitypub.models.Actor.id > decoded)

    return query


async def _respond_with_account_list(
    request: Request,
    db_session: AsyncSession,
    actors: Sequence[activitypub.models.Actor],
) -> JSONResponse:
    accounts = [
        await serializers.serialize_account(db_session, actor) for actor in actors
    ]

    response = JSONResponse(content=accounts, status_code=200)
    link_header = pagination.build_link_header(
        request, [account["id"] for account in accounts]
    )
    if link_header:
        response.headers["Link"] = link_header
    return response


async def _paginated_actor_list(
    request: Request,
    db_session: AsyncSession,
    *,
    model: type[activitypub.models.Follower] | type[activitypub.models.Following],
    join_column,
) -> JSONResponse:
    params = pagination.parse_pagination(request)
    query = _apply_account_cursor(
        select(model)
        .join(activitypub.models.Actor, join_column == activitypub.models.Actor.id)
        .options(joinedload(model.actor))
        .order_by(activitypub.models.Actor.id.desc())
        .limit(params.limit),
        params,
    )

    rows = (await db_session.scalars(query)).unique().all()
    return await _respond_with_account_list(
        request, db_session, [row.actor for row in rows]
    )


@router.get("/api/v1/accounts/{account_id}/followers", response_model=None)
async def accounts_followers(
    account_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    if account_id != ids.LOCAL_ACTOR_ID:
        if await ids.get_account_by_mastodon_id(db_session, account_id) is None:
            raise MastodonError(404, "not_found", "account not found")
        # We only have OUR OWN followers cached; a remote actor's follower
        # list lives on their home server.
        return JSONResponse(content=[], status_code=200)

    if config.HIDES_FOLLOWERS:
        return JSONResponse(content=[], status_code=200)

    return await _paginated_actor_list(
        request,
        db_session,
        model=activitypub.models.Follower,
        join_column=activitypub.models.Follower.actor_id,
    )


@router.get("/api/v1/accounts/{account_id}/following", response_model=None)
async def accounts_following(
    account_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    if account_id != ids.LOCAL_ACTOR_ID:
        if await ids.get_account_by_mastodon_id(db_session, account_id) is None:
            raise MastodonError(404, "not_found", "account not found")
        return JSONResponse(content=[], status_code=200)

    if config.HIDES_FOLLOWING:
        return JSONResponse(content=[], status_code=200)

    return await _paginated_actor_list(
        request,
        db_session,
        model=activitypub.models.Following,
        join_column=activitypub.models.Following.actor_id,
    )


@router.get("/api/v1/accounts/{account_id}/featured_tags", response_model=None)
async def accounts_featured_tags(
    account_id: str,
    db_session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    if account_id != ids.LOCAL_ACTOR_ID:
        # We only have featured tags for our own profile (configured in
        # `data/profile.toml`) — a remote actor's featured tags live on
        # their home server.
        return JSONResponse(content=[], status_code=200)

    return JSONResponse(
        content=await serializers.serialize_featured_tags(db_session),
        status_code=200,
    )


@router.get("/api/v1/accounts/{account_id}/endorsements", response_model=None)
async def accounts_endorsements(
    account_id: str,
) -> JSONResponse:
    # Endorsements (accounts featured on a profile) are not supported —
    # always return an empty list rather than 404ing.
    return JSONResponse(content=[], status_code=200)


@router.get("/api/v1/accounts/{account_id}/lists", response_model=None)
async def accounts_lists(
    account_id: str,
    token_info: AccessTokenInfo = Depends(require_scope("read:lists")),
) -> JSONResponse:
    # Which of the owner's lists this account is in. Lists are an empty stub
    # (`lists_index`), so this is empty for the same reason — but it has to
    # *exist*: clients call it when opening a profile, and a 404 there is an
    # error dialog rather than an absent section.
    return JSONResponse(content=[], status_code=200)


# --- Statuses ----------------------------------------------------------------


async def _is_authenticated_admin(request: Request, db_session: AsyncSession) -> bool:
    """Every valid access token belongs to the single owner (no
    client_credentials/multi-user support — see PR-0b's security fix), so a
    valid token always means "the admin is asking".
    """
    token_info = await check_access_token(request, db_session)
    return token_info is not None


async def _get_visible_status_or_404(
    request: Request, db_session: AsyncSession, status_id: str
) -> AnyboxObject:
    obj = await ids.get_object_by_mastodon_id(db_session, status_id)
    if obj is None or obj.is_deleted:
        raise MastodonError(404, "not_found", "status not found")

    if obj.visibility not in (ap.VisibilityEnum.PUBLIC, ap.VisibilityEnum.UNLISTED):
        if not await _is_authenticated_admin(request, db_session):
            raise MastodonError(404, "not_found", "status not found")

    return obj


@router.get("/api/v1/statuses/{status_id}", response_model=None)
async def statuses_show(
    status_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    obj = await _get_visible_status_or_404(request, db_session, status_id)
    return JSONResponse(
        content=await serializers.serialize_status(db_session, obj), status_code=200
    )


@router.get("/api/v1/statuses/{status_id}/source", response_model=None)
async def statuses_source(
    status_id: str,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("read:statuses")),
) -> JSONResponse:
    # Only the owner's own statuses (OutboxObject) can be edited, so this is
    # meaningless for a cached remote (InboxObject) status.
    obj = await ids.get_object_by_mastodon_id(db_session, status_id)
    if obj is None or not isinstance(obj, activitypub.models.OutboxObject):
        raise MastodonError(404, "not_found", "status not found")

    return JSONResponse(
        content={
            "id": status_id,
            "text": obj.source or "",
            "spoiler_text": obj.summary or "",
        },
        status_code=200,
    )


@router.get("/api/v1/statuses/{status_id}/history", response_model=None)
async def statuses_history(
    status_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    # Unlike /source (editing-only, always requires auth), history is public
    # for any status a viewer could already see — same visibility check as
    # /context. But the `revisions` column (and the pre-edit snapshots it
    # holds) only exists on OutboxObject, so remote (InboxObject) statuses
    # 404 here too.
    obj = await _get_visible_status_or_404(request, db_session, status_id)
    if not isinstance(obj, activitypub.models.OutboxObject):
        raise MastodonError(404, "not_found", "status not found")

    account = await serializers.serialize_account(db_session, obj.actor)
    entries = [
        serializers.serialize_status_edit(
            revision["ap_object"],
            revision.get("updated"),
            obj.actor,
            account,
            status_id,
        )
        for revision in obj.revisions or []
    ]
    entries.append(
        serializers.serialize_status_edit(
            obj.ap_object,
            obj.ap_object.get("updated") or obj.ap_object.get("published"),
            obj.actor,
            account,
            status_id,
        )
    )

    return JSONResponse(content=entries, status_code=200)


def _find_node_with_ancestors(
    node: ReplyTreeNode, target_ap_id: str, path: list[ReplyTreeNode]
) -> tuple[ReplyTreeNode, list[ReplyTreeNode]] | None:
    if node.ap_object is not None and node.ap_object.ap_id == target_ap_id:
        return node, path
    for child in node.children:
        found = _find_node_with_ancestors(child, target_ap_id, path + [node])
        if found is not None:
            return found
    return None


def _flatten_descendants(node: ReplyTreeNode) -> list[ReplyTreeNode]:
    out = []
    for child in node.children:
        out.append(child)
        out.extend(_flatten_descendants(child))
    return out


@router.get("/api/v1/statuses/{status_id}/context", response_model=None)
async def statuses_context(
    status_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    obj = await _get_visible_status_or_404(request, db_session, status_id)
    is_admin = await _is_authenticated_admin(request, db_session)

    # Mastodon apps only hit this endpoint when a user actually opens a
    # thread, so it's a reasonable place to opportunistically backfill
    # remote replies push delivery never gave us — mirrors the admin UI's
    # "fetch replies" action. Local (outbox) statuses have nothing to pull.
    if isinstance(obj, activitypub.models.InboxObject):
        try:
            if await fetch_replies(db_session, obj):
                await db_session.commit()
        except Exception:
            logger.exception(f"Failed to backfill replies for {obj.ap_id}")

    tree = await get_replies_tree(db_session, obj, is_admin)
    found = _find_node_with_ancestors(tree, obj.ap_id, [])

    ancestor_nodes: list[ReplyTreeNode] = []
    descendant_nodes: list[ReplyTreeNode] = []
    if found is not None:
        requested_node, ancestor_nodes = found
        descendant_nodes = _flatten_descendants(requested_node)

    await serializers.prefetch_status_relations(
        db_session,
        [
            node.ap_object
            for node in ancestor_nodes + descendant_nodes
            if node.ap_object is not None
        ],
    )
    ancestors = [
        await serializers.serialize_status(db_session, node.ap_object)
        for node in ancestor_nodes
        if node.ap_object is not None
    ]
    descendants = [
        await serializers.serialize_status(db_session, node.ap_object)
        for node in descendant_nodes
        if node.ap_object is not None
    ]

    return JSONResponse(
        content={"ancestors": ancestors, "descendants": descendants},
        status_code=200,
    )


@router.get("/api/v1/statuses/{status_id}/favourited_by", response_model=None)
async def statuses_favourited_by(
    status_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    # Same visibility gate as statuses_show: a private/direct status's likers
    # must not be discoverable (nor its existence confirmed) by non-admins.
    obj = await _get_visible_status_or_404(request, db_session, status_id)
    if not isinstance(obj, activitypub.models.OutboxObject):
        # We only know who liked OUR OWN posts (their Like activities land in
        # our inbox); a remote post's likers aren't visible to us.
        return JSONResponse(content=[], status_code=200)

    likers = (
        (
            await db_session.scalars(
                select(activitypub.models.InboxObject)
                .where(
                    activitypub.models.InboxObject.ap_type == "Like",
                    activitypub.models.InboxObject.activity_object_ap_id == obj.ap_id,
                    activitypub.models.InboxObject.undone_by_inbox_object_id.is_(None),
                )
                .options(joinedload(activitypub.models.InboxObject.actor))
            )
        )
        .unique()
        .all()
    )

    accounts = [
        await serializers.serialize_account(db_session, like.actor) for like in likers
    ]
    return JSONResponse(content=accounts, status_code=200)


@router.get("/api/v1/statuses/{status_id}/reblogged_by", response_model=None)
async def statuses_reblogged_by(
    status_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    obj = await _get_visible_status_or_404(request, db_session, status_id)
    if not isinstance(obj, activitypub.models.OutboxObject):
        return JSONResponse(content=[], status_code=200)

    boosters = (
        (
            await db_session.scalars(
                select(activitypub.models.InboxObject)
                .where(
                    activitypub.models.InboxObject.ap_type == "Announce",
                    activitypub.models.InboxObject.activity_object_ap_id == obj.ap_id,
                    activitypub.models.InboxObject.undone_by_inbox_object_id.is_(None),
                )
                .options(joinedload(activitypub.models.InboxObject.actor))
            )
        )
        .unique()
        .all()
    )

    accounts = [
        await serializers.serialize_account(db_session, boost.actor)
        for boost in boosters
    ]
    return JSONResponse(content=accounts, status_code=200)


# --- Timelines -----------------------------------------------------------------


async def _resolve_cursor_published_at(
    db_session: AsyncSession, mastodon_id: str | None
) -> datetime | None:
    if not mastodon_id:
        return None
    obj = await ids.get_object_by_mastodon_id(db_session, mastodon_id)
    return obj.ap_published_at if obj else None


@router.get("/api/v1/timelines/home", response_model=None)
async def timelines_home(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("read:statuses")),
) -> JSONResponse:
    params = pagination.parse_pagination(request)
    before = await _resolve_cursor_published_at(db_session, params.max_id)
    after = await _resolve_cursor_published_at(
        db_session, params.min_id or params.since_id
    )

    # Mixed inbox+outbox timeline: ids aren't comparable across the two
    # tables, so the cursor is the boundary object's ap_published_at instead
    # (see PLAN-0.md's pagination design). Fetching `limit` from EACH side
    # before merging guarantees the merged top-`limit` is correct even if
    # every recent post came from just one side.
    inbox_items = await timelines.fetch_inbox_timeline_page(
        db_session, before=before, after=after, limit=params.limit
    )
    outbox_items = await timelines.fetch_outbox_timeline_page(
        db_session, before=before, after=after, limit=params.limit
    )
    combined: list[AnyboxObject] = [*inbox_items, *outbox_items]
    merged = sorted(
        combined,
        key=timelines.status_id_int,
        reverse=True,
    )[: params.limit]

    return await _respond_with_status_list(request, db_session, merged)


@router.get("/api/v1/timelines/public", response_model=None)
async def timelines_public(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    params = pagination.parse_pagination(request)
    local_only = request.query_params.get("local") == "true"

    if local_only:
        # Single table: plain id-based pagination, no published_at cursor
        # needed.
        # NOTE: unlike timelines.fetch_outbox_timeline_page, this doesn't
        # filter is_hidden_from_homepage — a pre-existing inconsistency with
        # the non-local branch below, left as-is (see PLAN-push.md Step 1).
        query = (
            select(activitypub.models.OutboxObject)
            .where(
                activitypub.models.OutboxObject.ap_type.in_(
                    timelines.TIMELINE_OBJECT_TYPES
                ),
                activitypub.models.OutboxObject.visibility == ap.VisibilityEnum.PUBLIC,
                activitypub.models.OutboxObject.is_deleted.is_(False),
            )
            .options(
                joinedload(
                    activitypub.models.OutboxObject.outbox_object_attachments
                ).joinedload(activitypub.models.OutboxObjectAttachment.upload)
            )
            .order_by(activitypub.models.OutboxObject.id.desc())
            .limit(params.limit)
        )
        if params.max_id:
            decoded = ids.decode_object_id_for_source(
                params.max_id, ids.ObjectSource.OUTBOX
            )
            if decoded is not None:
                query = query.where(activitypub.models.OutboxObject.id < decoded)
        cursor = params.min_id or params.since_id
        if cursor:
            decoded = ids.decode_object_id_for_source(cursor, ids.ObjectSource.OUTBOX)
            if decoded is not None:
                query = query.where(activitypub.models.OutboxObject.id > decoded)

        items = (await db_session.scalars(query)).unique().all()
        return await _respond_with_status_list(request, db_session, items)

    before = await _resolve_cursor_published_at(db_session, params.max_id)
    after = await _resolve_cursor_published_at(
        db_session, params.min_id or params.since_id
    )
    inbox_items = await timelines.fetch_inbox_timeline_page(
        db_session,
        before=before,
        after=after,
        limit=params.limit,
        extra_where=(
            activitypub.models.InboxObject.visibility == ap.VisibilityEnum.PUBLIC,
        ),
    )
    outbox_items = await timelines.fetch_outbox_timeline_page(
        db_session,
        before=before,
        after=after,
        limit=params.limit,
        extra_where=(
            activitypub.models.OutboxObject.visibility == ap.VisibilityEnum.PUBLIC,
        ),
    )
    combined: list[AnyboxObject] = [*inbox_items, *outbox_items]
    merged = sorted(
        combined,
        key=timelines.status_id_int,
        reverse=True,
    )[: params.limit]

    return await _respond_with_status_list(request, db_session, merged)


def _tag_query_params(request: Request, key: str) -> set[str]:
    """Normalized `any[]`/`all[]`/`none[]` query values.

    Clients disagree on the trailing `[]` (same split as the form bodies
    `_StatusParams` normalizes), so both spellings are accepted.
    """
    q = request.query_params
    return {
        timelines.normalize_tag(value)
        for value in [*q.getlist(f"{key}[]"), *q.getlist(key)]
        if value
    }


@router.get("/api/v1/timelines/tag/{hashtag}", response_model=None)
async def timelines_tag(
    hashtag: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    params = pagination.parse_pagination(request)

    # Mastodon's multi-tag form: the path hashtag plus `any[]` widen the match,
    # `all[]` narrows it, `none[]` excludes. Some clients build saved searches
    # out of these.
    any_of = {timelines.normalize_tag(hashtag)} | _tag_query_params(request, "any")
    all_of = _tag_query_params(request, "all")
    none_of = _tag_query_params(request, "none")

    # Hashtags live inside the ap_object JSON blob, not a queryable column, so
    # this scans a bounded recent-public-posts window and filters in Python
    # rather than pushing the predicate into SQL. Fine for a single-user
    # instance's post volume; not a real search index. Note that an `all[]`/
    # `none[]` query filters the same window more aggressively, so it can
    # return fewer than `limit` results while older matches exist.
    before = await _resolve_cursor_published_at(db_session, params.max_id)
    after = await _resolve_cursor_published_at(
        db_session, params.min_id or params.since_id
    )
    scan_limit = max(params.limit * 5, 100)

    inbox_items = await timelines.fetch_inbox_timeline_page(
        db_session,
        before=before,
        after=after,
        limit=scan_limit,
        extra_where=(
            activitypub.models.InboxObject.visibility == ap.VisibilityEnum.PUBLIC,
        ),
    )
    outbox_items = await timelines.fetch_outbox_timeline_page(
        db_session,
        before=before,
        after=after,
        limit=scan_limit,
        extra_where=(
            activitypub.models.OutboxObject.visibility == ap.VisibilityEnum.PUBLIC,
        ),
    )

    combined: list[AnyboxObject] = [*inbox_items, *outbox_items]
    candidates = [
        obj
        for obj in combined
        if timelines.matches_tag_query(obj, any_of, all_of, none_of)
    ]
    merged = sorted(candidates, key=timelines.status_id_int, reverse=True)[
        : params.limit
    ]

    return await _respond_with_status_list(request, db_session, merged)


# --- Notifications -------------------------------------------------------------


def _decode_notification_id(mastodon_id: str) -> int | None:
    # Notifications are a single table (unlike statuses/accounts), so the
    # Mastodon id is just the row's own PK — no dual-table encoding needed.
    try:
        return int(mastodon_id)
    except ValueError:
        return None


def _allowed_notification_types(request: Request) -> list[models.NotificationType]:
    include_types = set(request.query_params.getlist("types[]"))
    exclude_types = set(request.query_params.getlist("exclude_types[]"))

    allowed_internal_types = list(serializers.NOTIFICATION_TYPE_MAP.keys())
    if include_types:
        allowed_internal_types = [
            t
            for t in allowed_internal_types
            if serializers.NOTIFICATION_TYPE_MAP[t] in include_types
        ]
    if exclude_types:
        allowed_internal_types = [
            t
            for t in allowed_internal_types
            if serializers.NOTIFICATION_TYPE_MAP[t] not in exclude_types
        ]
    return allowed_internal_types


@router.get("/api/v1/notifications", response_model=None)
async def notifications_list(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("read:notifications")),
) -> JSONResponse:
    params = pagination.parse_pagination(request)
    allowed_internal_types = _allowed_notification_types(request)

    query = (
        select(models.Notification)
        .where(
            models.Notification.notification_type.in_(allowed_internal_types),
            models.notification_not_muted(),
            models.notification_not_in_muted_conversation(),
        )
        .options(*serializers.NOTIFICATION_OPTIONS)
        .order_by(models.Notification.id.desc())
        .limit(params.limit)
    )
    if params.max_id:
        decoded = _decode_notification_id(params.max_id)
        if decoded is not None:
            query = query.where(models.Notification.id < decoded)
    cursor = params.min_id or params.since_id
    if cursor:
        decoded = _decode_notification_id(cursor)
        if decoded is not None:
            query = query.where(models.Notification.id > decoded)

    notifications = list((await db_session.scalars(query)).unique().all())

    serialized = [
        entity
        for notif in notifications
        if (entity := await serializers.serialize_notification(db_session, notif))
        is not None
    ]
    logger.info(
        "notifications_list: query returned "
        f"{len(notifications)} row(s) "
        f"types={[n.notification_type for n in notifications]} "
        f"without_actor={sum(1 for n in notifications if n.actor is None)}, "
        f"serialized {len(serialized)}"
    )

    # Mirror the existing HTML notifications page (app/admin.py): viewing
    # marks them read.
    if any(notif.is_new for notif in notifications):
        for notif in notifications:
            notif.is_new = False
        await db_session.commit()

    response = JSONResponse(content=serialized, status_code=200)
    link_header = pagination.build_link_header(
        request, [entity["id"] for entity in serialized]
    )
    if link_header:
        response.headers["Link"] = link_header
    return response


# Notification requests (filtered-notifications queue, Mastodon 4.3+): this
# server never filters notifications, so the queue is always empty and the
# policy is always "accept everything" — but the endpoints must exist (200,
# not 404) or clients that fetch them alongside the main list (Ice Cubes
# among them) fail to render notifications at all. Must be registered before
# `/api/v1/notifications/{notification_id}` below, since Starlette matches
# GET routes in registration order and that route would otherwise swallow
# these static paths (e.g. "requests" as notification_id).


def _notification_policy_content() -> dict[str, Any]:
    return {
        "for_not_following": "accept",
        "for_not_followers": "accept",
        "for_new_accounts": "accept",
        "for_private_mentions": "accept",
        "for_limited_accounts": "accept",
        "summary": {
            "pending_requests_count": 0,
            "pending_notifications_count": 0,
        },
    }


@router.get("/api/v2/notifications/policy", response_model=None)
async def notifications_policy_get(
    token_info: AccessTokenInfo = Depends(require_scope("read:notifications")),
) -> JSONResponse:
    return JSONResponse(content=_notification_policy_content(), status_code=200)


@router.put("/api/v2/notifications/policy", response_model=None)
async def notifications_policy_put(
    token_info: AccessTokenInfo = Depends(require_scope("write:notifications")),
) -> JSONResponse:
    # No filtering is implemented, so there is nothing to persist — echo the
    # fixed accept-all policy back.
    return JSONResponse(content=_notification_policy_content(), status_code=200)


@router.get("/api/v1/notifications/requests/merged", response_model=None)
async def notification_requests_merged(
    token_info: AccessTokenInfo = Depends(require_scope("read:notifications")),
) -> JSONResponse:
    return JSONResponse(content={"merged": True}, status_code=200)


@router.get("/api/v1/notifications/requests", response_model=None)
async def notification_requests_index(
    token_info: AccessTokenInfo = Depends(require_scope("read:notifications")),
) -> JSONResponse:
    return JSONResponse(content=[], status_code=200)


@router.get("/api/v1/notifications/unread_count", response_model=None)
async def notifications_unread_count(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("read:notifications")),
) -> JSONResponse:
    allowed_internal_types = _allowed_notification_types(request)
    limit = min(int(request.query_params.get("limit", 100)), 1000)

    query = select(func.count()).select_from(
        select(models.Notification.id)
        .where(
            models.Notification.notification_type.in_(allowed_internal_types),
            models.Notification.is_new.is_(True),
            models.notification_not_muted(),
            models.notification_not_in_muted_conversation(),
        )
        .limit(limit)
        .subquery()
    )
    count = await db_session.scalar(query)
    return JSONResponse(content={"count": count}, status_code=200)


@router.get("/api/v1/notifications/{notification_id}", response_model=None)
async def notifications_show(
    notification_id: str,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("read:notifications")),
) -> JSONResponse:
    internal_id = _decode_notification_id(notification_id)
    notification = (
        await db_session.get(
            models.Notification, internal_id, options=serializers.NOTIFICATION_OPTIONS
        )
        if internal_id is not None
        else None
    )
    serialized = (
        await serializers.serialize_notification(db_session, notification)
        if notification is not None
        else None
    )
    if serialized is None:
        raise MastodonError(404, "not_found", "notification not found")

    return JSONResponse(content=serialized, status_code=200)


@router.post("/api/v1/notifications/clear", response_model=None)
async def notifications_clear(
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:notifications")),
) -> JSONResponse:
    await db_session.execute(delete(models.Notification))
    await db_session.commit()
    return JSONResponse(content={}, status_code=200)


@router.post("/api/v1/notifications/{notification_id}/dismiss", response_model=None)
async def notifications_dismiss(
    notification_id: str,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:notifications")),
) -> JSONResponse:
    internal_id = _decode_notification_id(notification_id)
    if internal_id is not None:
        await db_session.execute(
            delete(models.Notification).where(models.Notification.id == internal_id)
        )
        await db_session.commit()
    return JSONResponse(content={}, status_code=200)


# --- Web Push ------------------------------------------------------------------

_ALERT_FIELDS = [
    "mention",
    "status",
    "reblog",
    "follow",
    "follow_request",
    "favourite",
    "poll",
    "update",
]


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _unflatten_form(form_data: Any) -> dict[str, Any]:
    """Turn `subscription[keys][p256dh]=...`-style flat form keys into the
    same nested shape a JSON body would already have."""
    result: dict[str, Any] = {}
    for key in form_data.keys():
        parts = re.findall(r"[^\[\]]+", key)
        if not parts:
            continue
        node = result
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = form_data.get(key)
    return result


async def _parse_push_body(request: Request) -> dict[str, Any]:
    content_type, _, _ = request.headers.get("Content-Type", "").partition(";")
    if content_type.strip().lower() == "application/json":
        body = await request.json()
        return body if isinstance(body, dict) else {}
    return _unflatten_form(await request.form())


async def _get_push_subscription(
    db_session: AsyncSession, access_token_id: int | None
) -> models.PushSubscription | None:
    return (
        await db_session.scalars(
            select(models.PushSubscription).where(
                models.PushSubscription.access_token_id == access_token_id
            )
        )
    ).one_or_none()


async def _get_access_token_row(
    db_session: AsyncSession, token_info: AccessTokenInfo
) -> models.IndieAuthAccessToken:
    row = (
        await db_session.scalars(
            select(models.IndieAuthAccessToken).where(
                models.IndieAuthAccessToken.access_token == token_info.access_token
            )
        )
    ).one_or_none()
    if row is None:
        raise ValueError("Should never happen")
    return row


def _serialize_push_subscription(sub: models.PushSubscription) -> dict:
    return {
        "id": str(sub.id),
        "endpoint": sub.endpoint,
        "alerts": {
            "mention": sub.alert_mention,
            "status": sub.alert_status,
            "reblog": sub.alert_reblog,
            "follow": sub.alert_follow,
            "follow_request": sub.alert_follow_request,
            "favourite": sub.alert_favourite,
            "poll": sub.alert_poll,
            "update": sub.alert_update,
            # Never fired on a single-user instance with no sign-up/reports.
            "admin.sign_up": False,
            "admin.report": False,
        },
        "policy": sub.policy,
        "server_key": vapid_public_key_b64(),
        "standard": True,
    }


@router.post("/api/v1/push/subscription", response_model=None)
async def push_subscription_create(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("push")),
) -> JSONResponse:
    body = await _parse_push_body(request)
    subscription_data = body.get("subscription") or {}
    keys = subscription_data.get("keys") or {}
    data = body.get("data") or {}
    alerts_raw = data.get("alerts") or {}

    endpoint = subscription_data.get("endpoint")
    if (
        not endpoint
        or not isinstance(endpoint, str)
        or not endpoint.startswith("https://")
    ):
        raise MastodonError(
            422, "validation_failed", "subscription.endpoint must be an https:// URL"
        )

    try:
        p256dh_bytes = decode_client_key(str(keys.get("p256dh") or ""))
        auth_bytes = decode_client_key(str(keys.get("auth") or ""))
    except Exception:
        raise MastodonError(422, "validation_failed", "subscription.keys is invalid")

    try:
        parse_p256dh(p256dh_bytes)
    except ValueError:
        raise MastodonError(
            422, "validation_failed", "subscription.keys.p256dh is invalid"
        )

    try:
        parse_auth_secret(auth_bytes)
    except ValueError:
        raise MastodonError(
            422, "validation_failed", "subscription.keys.auth is invalid"
        )

    policy = str(data.get("policy") or "all")
    if policy not in ("all", "followed", "follower", "none"):
        raise MastodonError(422, "validation_failed", "data.policy is invalid")

    try:
        await check_url_async(endpoint)
    except InvalidURLError:
        raise MastodonError(
            422, "validation_failed", "subscription.endpoint is not allowed"
        )

    access_token_row = await _get_access_token_row(db_session, token_info)
    sub = await _get_push_subscription(db_session, access_token_row.id)
    if sub is None:
        sub = models.PushSubscription(access_token_id=access_token_row.id)
        db_session.add(sub)

    sub.endpoint = endpoint
    sub.p256dh = keys["p256dh"]
    sub.auth = keys["auth"]
    sub.policy = policy
    for field in _ALERT_FIELDS:
        setattr(sub, f"alert_{field}", _truthy(alerts_raw.get(field, True)))

    # A new subscription (or a replaced one) never gets a backlog: seed the
    # watermark at the current high-water mark.
    sub.last_notification_id = (
        await db_session.scalar(select(func.max(models.Notification.id))) or 0
    )
    sub.tries = 0
    sub.next_try = now()
    sub.last_try = None
    sub.error = None

    await db_session.commit()

    # An install that upgraded without wiring up the push_worker process
    # would otherwise silently accept subscriptions and deliver nothing —
    # indistinguishable from a broken feature. Flag it loudly.
    oldest_undelivered = await db_session.scalar(
        select(func.min(models.PushSubscription.created_at)).where(
            models.PushSubscription.last_success_at.is_(None)
        )
    )
    if oldest_undelivered and now() - as_utc(oldest_undelivered) > timedelta(hours=1):
        logger.warning(
            "Push subscriptions exist but none has ever been delivered to — "
            "is the push_worker process running? See docs/mastodon_api.md."
        )

    return JSONResponse(content=_serialize_push_subscription(sub), status_code=200)


@router.get("/api/v1/push/subscription", response_model=None)
async def push_subscription_get(
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("push")),
) -> JSONResponse:
    access_token_row = await _get_access_token_row(db_session, token_info)
    sub = await _get_push_subscription(db_session, access_token_row.id)
    if sub is None:
        raise MastodonError(404, "not_found", "no push subscription for this token")

    return JSONResponse(content=_serialize_push_subscription(sub), status_code=200)


@router.put("/api/v1/push/subscription", response_model=None)
async def push_subscription_update(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("push")),
) -> JSONResponse:
    access_token_row = await _get_access_token_row(db_session, token_info)
    sub = await _get_push_subscription(db_session, access_token_row.id)
    if sub is None:
        raise MastodonError(404, "not_found", "no push subscription for this token")

    body = await _parse_push_body(request)
    data = body.get("data") or {}
    alerts_raw = data.get("alerts") or {}

    # Partial update: only data[alerts][*] and data[policy]. Endpoint/keys
    # never change here — a client that rotates them must POST a new
    # subscription instead.
    for field in _ALERT_FIELDS:
        if field in alerts_raw:
            setattr(sub, f"alert_{field}", _truthy(alerts_raw[field]))

    if "policy" in data:
        policy = str(data["policy"])
        if policy not in ("all", "followed", "follower", "none"):
            raise MastodonError(422, "validation_failed", "data.policy is invalid")
        sub.policy = policy

    await db_session.commit()

    return JSONResponse(content=_serialize_push_subscription(sub), status_code=200)


@router.delete("/api/v1/push/subscription", response_model=None)
async def push_subscription_delete(
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("push")),
) -> JSONResponse:
    access_token_row = await _get_access_token_row(db_session, token_info)
    await db_session.execute(
        delete(models.PushSubscription).where(
            models.PushSubscription.access_token_id == access_token_row.id
        )
    )
    await db_session.commit()
    return JSONResponse(content={}, status_code=200)


# --- Conversations ---------------------------------------------------------------
# Mastodon's DM inbox: one entry per `ap_context` thread of direct-visibility
# statuses. Thread grouping and serialization live in `app.mastodon.timelines`
# / `app.mastodon.serializers` — the streaming event pump needs them too.


def _safe_id_int(mastodon_id: str | None) -> int | None:
    if not mastodon_id:
        return None
    try:
        return int(mastodon_id)
    except ValueError:
        return None


@router.get("/api/v1/conversations", response_model=None)
async def conversations_list(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("read:statuses")),
) -> JSONResponse:
    params = pagination.parse_pagination(request)
    threads = await timelines.dm_threads(db_session)

    if (max_int := _safe_id_int(params.max_id)) is not None:
        threads = [t for t in threads if timelines.status_id_int(t[0]) < max_int]
    if (cursor_int := _safe_id_int(params.min_id or params.since_id)) is not None:
        threads = [t for t in threads if timelines.status_id_int(t[0]) > cursor_int]
    threads = threads[: params.limit]
    await serializers.prefetch_status_relations(
        db_session, [last for last, _, _ in threads]
    )

    serialized = [
        await serializers.serialize_conversation(db_session, last, actor_ids, unread)
        for last, actor_ids, unread in threads
    ]
    response = JSONResponse(content=serialized, status_code=200)
    link_header = pagination.build_link_header(
        request, [entity["id"] for entity in serialized]
    )
    if link_header:
        response.headers["Link"] = link_header
    return response


@router.post("/api/v1/conversations/{conversation_id}/read", response_model=None)
async def conversations_read(
    conversation_id: str,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:conversations")),
) -> JSONResponse:
    obj = await ids.get_object_by_mastodon_id(db_session, conversation_id)
    if obj is None or obj.ap_context is None:
        raise MastodonError(404, "not_found", "conversation not found")

    await db_session.execute(
        update(models.Notification)
        .where(
            models.Notification.notification_type == models.NotificationType.MENTION,
            models.Notification.inbox_object_id.in_(
                select(activitypub.models.InboxObject.id).where(
                    activitypub.models.InboxObject.ap_context == obj.ap_context,
                )
            ),
        )
        .values(is_new=False)
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()

    threads = await timelines.dm_threads(db_session)
    match = next((t for t in threads if t[0].ap_context == obj.ap_context), None)
    if match is None:
        raise MastodonError(404, "not_found", "conversation not found")
    last, actor_ids, _ = match
    return JSONResponse(
        content=await serializers.serialize_conversation(
            db_session, last, actor_ids, False
        ),
        status_code=200,
    )


# --- Single-user degradations ---------------------------------------------------
# Multi-user-only Mastodon features this single-user server has no data for.
# Empty collection (or harmless no-op) rather than 404, so clients render an
# empty state instead of an error. `follow_requests` stays here (not a real
# list) until PR-3 lands accept/reject — no point showing a request the
# client can't yet act on.


@router.get("/api/v1/lists", response_model=None)
async def lists_index(
    token_info: AccessTokenInfo = Depends(require_scope("read")),
) -> JSONResponse:
    return JSONResponse(content=[], status_code=200)


@router.get("/api/v1/filters", response_model=None)
async def filters_v1_index(
    token_info: AccessTokenInfo = Depends(require_scope("read")),
) -> JSONResponse:
    return JSONResponse(content=[], status_code=200)


@router.get("/api/v2/filters", response_model=None)
async def filters_v2_index(
    token_info: AccessTokenInfo = Depends(require_scope("read")),
) -> JSONResponse:
    return JSONResponse(content=[], status_code=200)


@router.get("/api/v1/suggestions", response_model=None)
async def suggestions_v1_index(
    token_info: AccessTokenInfo = Depends(require_scope("read")),
) -> JSONResponse:
    return JSONResponse(content=[], status_code=200)


@router.get("/api/v2/suggestions", response_model=None)
async def suggestions_v2_index(
    token_info: AccessTokenInfo = Depends(require_scope("read")),
) -> JSONResponse:
    return JSONResponse(content=[], status_code=200)


@router.get("/api/v1/endorsements", response_model=None)
async def endorsements_index(
    token_info: AccessTokenInfo = Depends(require_scope("read:accounts")),
) -> JSONResponse:
    # The index counterpart of `accounts_endorsements`. Endorsements are
    # multi-user social signalling with no meaning for one actor, but the
    # per-account route existing while this one 404s is the worst of both:
    # clients open the profile fine, then error on the featured-accounts
    # section.
    return JSONResponse(content=[], status_code=200)


@router.get("/api/v1/followed_tags", response_model=None)
async def followed_tags_index(
    token_info: AccessTokenInfo = Depends(require_scope("read:follows")),
) -> JSONResponse:
    # Following a hashtag isn't implemented (single-tag timelines are, see
    # `timelines_tag`). Empty rather than 404 so the client's followed-tags
    # screen shows an empty state instead of failing to open.
    return JSONResponse(content=[], status_code=200)


def _serialize_tag(tag: str) -> dict:
    """Mastodon's `Tag` entity for an already-normalized hashtag.

    There's no tag table to hold a numeric id, and nothing dereferences one,
    so the normalized name doubles as the (String-typed) id. `history` is
    always empty — no per-day usage is tracked. `following` is honestly
    `false`: see `followed_tags_index`. `featuring` is omitted rather than
    hardcoded, since it's 4.4 and we advertise 4.3.
    """
    return {
        "id": tag,
        "name": tag,
        "url": f"{config.BASE_URL}/t/{tag}",
        "history": [],
        "following": False,
    }


@router.get("/api/v1/tags/{tag_id}", response_model=None)
async def tags_show(
    tag_id: str,
) -> JSONResponse:
    # Clients fetch this when opening a hashtag, to render the header and
    # decide whether to show a follow button. Upstream Mastodon materializes
    # tags on demand and never 404s here, so neither do we — the tag having
    # no local posts is a matter for the timeline query, not this lookup.
    #
    # `tags/{id}/follow` and `/unfollow` stay unimplemented on purpose: with
    # no storage behind them they could only report success while persisting
    # nothing, which is the one thing this API surface doesn't do (see
    # features.md §4). `following: false` tells the client the truth instead.
    # `.strip()` on top of `normalize_tag` matches how `search` normalizes its
    # hashtag query, so the two Tag-emitting paths agree on the name as well
    # as the shape (pinned by a test).
    tag = timelines.normalize_tag(tag_id).strip()
    if not tag:
        raise MastodonError(404, "not_found", "not a valid hashtag")
    return JSONResponse(content=_serialize_tag(tag), status_code=200)


# /api/v1/blocks and /api/v1/mutes are real, non-stub lists (both are
# persisted on the actor row) — see the "Social graph" section below.


# follow_requests is a real, non-stub list — see the "Social graph" section
# below (PR-3), which also lands authorize/reject.


# Public, unauthenticated in real Mastodon too.


@router.get("/api/v1/featured_tags", response_model=None)
async def featured_tags_index(
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("read:accounts")),
) -> JSONResponse:
    return JSONResponse(
        content=await serializers.serialize_featured_tags(db_session),
        status_code=200,
    )


@router.get("/api/v1/directory", response_model=None)
async def directory_index() -> JSONResponse:
    return JSONResponse(content=[], status_code=200)


@router.get("/api/v1/trends/tags", response_model=None)
async def trends_tags() -> JSONResponse:
    return JSONResponse(content=[], status_code=200)


@router.get("/api/v1/trends/statuses", response_model=None)
async def trends_statuses() -> JSONResponse:
    return JSONResponse(content=[], status_code=200)


@router.get("/api/v1/trends/links", response_model=None)
async def trends_links() -> JSONResponse:
    return JSONResponse(content=[], status_code=200)


# --- Media -----------------------------------------------------------------
# No async processing state machine: `save_upload` (EXIF-strip, blurhash,
# thumbnail) runs inline, so every response here is the final, fully
# populated MediaAttachment — never Mastodon's `206`/still-processing shape.


def _parse_focus(value: str) -> tuple[float, float]:
    """Parse Mastodon's `focus` media param, e.g. "-0.5,0.7" -> (-0.5, 0.7)."""
    parts = value.split(",")
    if len(parts) != 2:
        raise ValueError("focus must be `x,y`")

    try:
        x, y = float(parts[0]), float(parts[1])
    except ValueError:
        raise ValueError("focus must be `x,y` floats")

    if not (-1.0 <= x <= 1.0 and -1.0 <= y <= 1.0):
        raise ValueError("focus x/y must be within [-1.0, 1.0]")

    return (x, y)


@router.post("/api/v2/media", response_model=None)
async def media_create(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:media")),
) -> JSONResponse:
    form = await request.form()
    file = form.get("file")
    if not isinstance(file, UploadFile):
        raise MastodonError(422, "validation_failed", "file is required")

    # request.form() always returns Starlette's base UploadFile, never
    # FastAPI's subclass (that only comes from `File(...)` dependency
    # injection) — save_upload only touches attributes both share.
    try:
        upload = await save_upload(db_session, cast(FastAPIUploadFile, file))
    except UploadTooLargeError as exc:
        raise MastodonError(
            422, "validation_failed", f"file exceeds the {exc.limit} byte limit"
        )
    except IncompatibleMediaError as exc:
        raise MastodonError(422, "validation_failed", exc.reason)
    if upload is None:
        raise MastodonError(422, "validation_failed", "unable to process upload")

    description = form.get("description")
    if description:
        upload.description = str(description)
        await db_session.commit()

    focus = form.get("focus")
    if focus:
        try:
            upload.focus_x, upload.focus_y = _parse_focus(str(focus))
        except ValueError as exc:
            raise MastodonError(422, "validation_failed", str(exc))
        await db_session.commit()

    return JSONResponse(content=serializers.serialize_upload(upload), status_code=200)


@router.post("/api/v1/media", response_model=None)
async def media_create_v1(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:media")),
) -> JSONResponse:
    return await media_create(request, db_session, token_info)


@router.get("/api/v1/media/{media_id}", response_model=None)
async def media_show(
    media_id: str,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("read:media")),
) -> JSONResponse:
    upload = await ids.get_upload_by_mastodon_id(db_session, media_id)
    if upload is None:
        raise MastodonError(404, "not_found", "media not found")

    return JSONResponse(content=serializers.serialize_upload(upload), status_code=200)


@router.put("/api/v1/media/{media_id}", response_model=None)
async def media_update(
    media_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:media")),
) -> JSONResponse:
    upload = await ids.get_upload_by_mastodon_id(db_session, media_id)
    if upload is None:
        raise MastodonError(404, "not_found", "media not found")

    form = await request.form()
    if "description" in form:
        description = form.get("description")
        upload.description = str(description) if description else None
        await db_session.commit()

    if "focus" in form:
        focus = form.get("focus")
        if focus:
            try:
                upload.focus_x, upload.focus_y = _parse_focus(str(focus))
            except ValueError as exc:
                raise MastodonError(422, "validation_failed", str(exc))
        else:
            upload.focus_x = upload.focus_y = None
        await db_session.commit()

    return JSONResponse(content=serializers.serialize_upload(upload), status_code=200)


# --- Status writes / interactions / polls -----------------------------------

_MASTODON_VISIBILITY_TO_AP = {
    "public": ap.VisibilityEnum.PUBLIC,
    "unlisted": ap.VisibilityEnum.UNLISTED,
    "private": ap.VisibilityEnum.FOLLOWERS_ONLY,
    "direct": ap.VisibilityEnum.DIRECT,
}

# In-process only (not persisted, not shared across workers) — enough to stop
# a client's retried POST from double-posting within this process's lifetime,
# without standing up a Redis-like store for a single-user server.
_IDEMPOTENCY_CACHE: dict[str, str] = {}

# Same idea for the scheduled variant, keyed to the queue row id rather than a
# status id — a retried POST must not queue the same post twice either.
_SCHEDULED_IDEMPOTENCY_CACHE: dict[str, int] = {}


def _form_list(form: Any, key: str) -> list[Any]:
    """The values of a repeated form field, in every spelling clients use.

    `key[]` is what the Mastodon docs show and what most clients send, a bare
    `key` is what a few send for a single value, and `key[0]`/`key[1]`… is what
    some HTTP libraries generate for a list. The indexed form matters most for
    poll options: unrecognized, the poll silently vanishes from the request and
    the status posts as a plain note instead of failing loudly.
    """
    if values := form.getlist(f"{key}[]"):
        return list(values)
    if values := form.getlist(key):
        return list(values)

    indexed = []
    for form_key in form.keys():
        if match := re.fullmatch(rf"{re.escape(key)}\[(\d+)\]", form_key):
            indexed.append((int(match.group(1)), form[form_key]))
    return [value for _, value in sorted(indexed)]


def _form_media_attributes(form: Any) -> list[dict[str, str]]:
    """`media_attributes[N][field]` (indexed objects, what clients normally
    send) or `media_attributes[][field]` (parallel arrays), parsed into a
    list of `{id, description, focus}` dicts, one per entry.
    """
    by_index: dict[int, dict[str, str]] = {}
    for form_key in form.keys():
        if match := re.fullmatch(r"media_attributes\[(\d+)\]\[(\w+)\]", form_key):
            index, field = int(match.group(1)), match.group(2)
            by_index.setdefault(index, {})[field] = form[form_key]
    if by_index:
        return [by_index[i] for i in sorted(by_index)]

    ids_ = form.getlist("media_attributes[][id]")
    if not ids_:
        return []
    descriptions = form.getlist("media_attributes[][description]")
    focuses = form.getlist("media_attributes[][focus]")
    entries = []
    for i, media_id in enumerate(ids_):
        entry = {"id": media_id}
        if i < len(descriptions):
            entry["description"] = descriptions[i]
        if i < len(focuses):
            entry["focus"] = focuses[i]
        entries.append(entry)
    return entries


class _StatusParams:
    """Normalizes the POST /api/v1/statuses body across content types.

    The Mastodon API accepts this endpoint as `multipart/form-data`,
    `application/x-www-form-urlencoded`, or `application/json` — clients
    disagree on which they use (e.g. Tusky sends JSON; Fedilab sends form
    data). Starlette's `Request.form()` silently returns an empty `FormData`
    for a JSON body rather than raising, which was turning every JSON-body
    post into a 422 "status is required".
    """

    def __init__(
        self, json_body: dict[str, Any] | None, form: Any, query: Any = None
    ) -> None:
        self._json = json_body
        self._form = form
        # Query string params, used only as a fallback for keys the body
        # doesn't carry: Rails (and therefore Mastodon) merges query and body
        # params, so some clients put write params in the URL instead of the
        # body — Ice Cubes votes with `POST /api/v1/polls/{id}/votes?choices[]=0`
        # and an empty body, which read as "choices is required" (422) here.
        self._query = query

    def get(self, key: str) -> Any:
        if self._json is not None:
            value = self._json.get(key)
        else:
            value = self._form.get(key)
        if value is None and self._query is not None:
            value = self._query.get(key)
        return value

    def get_bool(self, key: str) -> bool:
        value = self.get(key)
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("true", "1", "on")

    def get_list(self, key: str) -> list[Any]:
        if self._json is not None:
            value = self._json.get(key)
            values = list(value) if value else []
        else:
            values = _form_list(self._form, key)
        if not values and self._query is not None:
            values = _form_list(self._query, key)
        return values

    def has(self, key: str) -> bool:
        """Whether `key` was present in the body at all — distinct from being
        present-but-empty. Used for fields where an edit should only touch
        the existing value if the client actually sent something for it
        (e.g. `media_ids`, where absence must mean "leave attachments alone",
        not "clear them").
        """
        if self._json is not None:
            if key in self._json:
                return True
        elif f"{key}[]" in self._form or key in self._form:
            return True
        return self._query is not None and (
            f"{key}[]" in self._query or key in self._query
        )

    def get_poll_options(self) -> list[str]:
        if self._json is not None:
            options = (self._json.get("poll") or {}).get("options") or []
        else:
            options = _form_list(self._form, "poll[options]")
        if not options and self._query is not None:
            options = _form_list(self._query, "poll[options]")
        return [str(option) for option in options]

    def get_poll_multiple(self) -> bool:
        if self._json is not None:
            return bool((self._json.get("poll") or {}).get("multiple"))
        value = self._form.get("poll[multiple]")
        if value is None and self._query is not None:
            value = self._query.get("poll[multiple]")
        return str(value or "").lower() == "true"

    def get_poll_expires_in_seconds(self) -> int | None:
        if self._json is not None:
            value = (self._json.get("poll") or {}).get("expires_in")
        else:
            value = self._form.get("poll[expires_in]")
            if value is None and self._query is not None:
                value = self._query.get("poll[expires_in]")
        return int(str(value)) if value else None

    def get_media_attributes(self) -> dict[str, dict[str, str]]:
        """Per-attachment edits (`media_attributes[][id]`/`[description]`/
        `[focus]`), keyed by the id each entry describes. Clients reuse
        whichever id spelling `media_ids` uses for that same attachment, so
        the keys here are matched against `media_ids` as opaque strings
        rather than decoded independently.
        """
        if self._json is not None:
            raw = self._json.get("media_attributes") or []
            entries = [entry for entry in raw if isinstance(entry, dict)]
        else:
            entries = _form_media_attributes(self._form)
        return {
            str(entry["id"]): entry for entry in entries if entry.get("id") is not None
        }


async def _body_params(request: Request) -> _StatusParams:
    """Read a request body as either JSON or form data.

    Same content-type dance as POST /api/v1/statuses below — clients disagree
    on which encoding they use for POST bodies, whatever the endpoint. The
    query string is kept as a fallback for keys the body doesn't carry, since
    Mastodon (Rails) makes no distinction between the two.
    """
    query = request.query_params
    content_type, _, _ = request.headers.get("Content-Type", "").partition(";")
    if content_type.strip().lower() == "application/json":
        try:
            return _StatusParams(await request.json(), None, query)
        except ValueError:
            return _StatusParams({}, None, query)
    return _StatusParams(None, await request.form(), query)


def _parse_scheduled_at(params: _StatusParams) -> datetime | None:
    """The requested publication time, or None to post right now.

    Some clients send an empty string for "not scheduled" rather than omitting
    the field, hence the falsy check. Unlike upstream Mastodon, which refuses
    anything less than five minutes out, any future time is accepted here: a
    client that honours the five-minute rule still works, and a single-user
    server has no throughput reason to reject a post two minutes from now.
    """
    raw = params.get("scheduled_at")
    if not raw:
        return None

    try:
        scheduled_at = parse_isoformat(str(raw))
    except Exception:
        raise MastodonError(
            422, "validation_failed", "scheduled_at is not a valid datetime"
        ) from None

    if scheduled_at <= now():
        raise MastodonError(
            422, "validation_failed", "scheduled_at must be in the future"
        )
    return scheduled_at


async def _parse_compose_params(
    db_session: AsyncSession,
    params: _StatusParams,
    idempotency_key: str | None,
) -> scheduled_statuses.ComposeParams:
    """Validate a status-write body into the shared compose parameters.

    Both the immediate and the scheduled path go through here, so a scheduled
    post is rejected for the same reasons (unknown media, unknown parent, bad
    visibility, one-option poll) at request time rather than silently failing
    when it comes due.
    """
    content_value = params.get("status")
    content = str(content_value) if content_value is not None else ""
    content_warning_value = params.get("spoiler_text")
    content_warning = str(content_warning_value) if content_warning_value else None
    sensitive = params.get_bool("sensitive")

    media_ids = [str(media_id) for media_id in params.get_list("media_ids")]
    for media_id in media_ids:
        if await ids.get_upload_by_mastodon_id(db_session, media_id) is None:
            raise MastodonError(
                422, "validation_failed", f"unknown media id {media_id}"
            )

    # Mirrors the existing HTML new-post form (app/admin.py): a CW with no
    # body text but attached media becomes the visible content instead.
    if not content and content_warning and media_ids:
        content = content_warning
        sensitive = True
        content_warning = None

    if not content:
        raise MastodonError(422, "validation_failed", "status is required")

    in_reply_to_id = params.get("in_reply_to_id")
    if in_reply_to_id:
        in_reply_to_id = str(in_reply_to_id)
        if await ids.get_object_by_mastodon_id(db_session, in_reply_to_id) is None:
            raise MastodonError(422, "validation_failed", "in_reply_to_id not found")
    else:
        in_reply_to_id = None

    quote_id = params.get("quote_id")
    if quote_id:
        quote_id = str(quote_id)
        if await ids.get_object_by_mastodon_id(db_session, quote_id) is None:
            raise MastodonError(422, "validation_failed", "quote_id not found")
    else:
        quote_id = None

    visibility_param = str(params.get("visibility") or "public")
    visibility = _MASTODON_VISIBILITY_TO_AP.get(visibility_param)
    if visibility is None:
        raise MastodonError(422, "validation_failed", "invalid visibility")

    language_value = params.get("language")
    language = str(language_value) if language_value else None

    poll_options = params.get_poll_options()
    poll_expires_in = params.get_poll_expires_in_seconds() if poll_options else None
    if poll_options:
        # Enforce exactly what `/api/v1/instance` advertises, so a client that
        # respects the advertised limits never gets a surprise 422 and one that
        # ignores them gets a message naming the limit instead of a poll that
        # federates as something no other server would have accepted.
        if len(poll_options) < 2:
            raise MastodonError(
                422, "validation_failed", "poll must have at least 2 options"
            )
        if len(poll_options) > _POLL_MAX_OPTIONS:
            raise MastodonError(
                422,
                "validation_failed",
                f"poll must have at most {_POLL_MAX_OPTIONS} options",
            )
        if any(
            len(option) > _POLL_MAX_CHARACTERS_PER_OPTION for option in poll_options
        ):
            raise MastodonError(
                422,
                "validation_failed",
                "poll options must be at most "
                f"{_POLL_MAX_CHARACTERS_PER_OPTION} characters",
            )
        if poll_expires_in is not None and not (
            _POLL_MIN_EXPIRATION <= poll_expires_in <= _POLL_MAX_EXPIRATION
        ):
            raise MastodonError(
                422,
                "validation_failed",
                f"poll expires_in must be between {_POLL_MIN_EXPIRATION} and "
                f"{_POLL_MAX_EXPIRATION} seconds",
            )

    return scheduled_statuses.ComposeParams(
        content=content,
        content_warning=content_warning,
        sensitive=True if content_warning else sensitive,
        visibility=visibility,
        language=language,
        in_reply_to_id=in_reply_to_id,
        quote_id=quote_id,
        media_ids=media_ids,
        poll_options=poll_options,
        poll_multiple=params.get_poll_multiple() if poll_options else False,
        poll_expires_in=poll_expires_in,
        idempotency=idempotency_key,
    )


@router.post("/api/v1/statuses", response_model=None)
async def statuses_create(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:statuses")),
) -> JSONResponse:
    idempotency_key = request.headers.get("Idempotency-Key")
    cache_key = (
        f"{token_info.access_token}:{idempotency_key}" if idempotency_key else None
    )

    params = await _body_params(request)
    scheduled_at = _parse_scheduled_at(params)

    if cache_key and scheduled_at is None and cache_key in _IDEMPOTENCY_CACHE:
        cached = await ids.get_object_by_mastodon_id(
            db_session, _IDEMPOTENCY_CACHE[cache_key]
        )
        if cached is not None:
            return JSONResponse(
                content=await serializers.serialize_status(db_session, cached),
                status_code=200,
            )

    if cache_key and scheduled_at is not None:
        cached_id = _SCHEDULED_IDEMPOTENCY_CACHE.get(cache_key)
        if cached_id is not None:
            cached_row = await db_session.get(models.ScheduledStatus, cached_id)
            if cached_row is not None:
                return JSONResponse(
                    content=await serializers.serialize_scheduled_status(
                        db_session, cached_row
                    ),
                    status_code=200,
                )

    compose = await _parse_compose_params(db_session, params, idempotency_key)

    if scheduled_at is not None:
        scheduled_status = await scheduled_statuses.schedule(
            db_session, compose, scheduled_at
        )
        if cache_key and scheduled_status.id is not None:
            _SCHEDULED_IDEMPOTENCY_CACHE[cache_key] = scheduled_status.id
        return JSONResponse(
            content=await serializers.serialize_scheduled_status(
                db_session, scheduled_status
            ),
            status_code=200,
        )

    try:
        outbox_object = await scheduled_statuses.publish(db_session, compose)
    except scheduled_statuses.ScheduledStatusError as exc:
        # Only reachable if an upload or the parent status disappeared between
        # validation above and the send.
        raise MastodonError(422, "validation_failed", str(exc)) from exc

    status_id = ids.encode_outbox_id(outbox_object)
    if cache_key:
        _IDEMPOTENCY_CACHE[cache_key] = status_id

    # Re-fetch through the eager-loading helper: `outbox_object` as returned
    # by send_create doesn't have outbox_object_attachments loaded, and it's
    # already in the session's identity map (see ids.py's populate_existing).
    created = await ids.get_object_by_mastodon_id(db_session, status_id)
    assert created is not None
    return JSONResponse(
        content=await serializers.serialize_status(db_session, created),
        status_code=200,
    )


async def _resolve_edit_media(
    db_session: AsyncSession,
    obj: activitypub.models.OutboxObject,
    status_id: str,
    media_id: str,
) -> tuple[activitypub.models.Upload, str, str | None] | None:
    """Resolve one `media_ids` entry of an edit to `(upload, filename,
    current_alt)`.

    Accepts a bare Upload id — a freshly-uploaded, not-yet-attached file, the
    same as `POST /api/v1/statuses` — or the legacy `{status_id}-{index}` id
    that a client may still have cached from before attachments reported
    their real Upload id (see `serializers.serialize_media_attachment`).
    """
    prefix = f"{status_id}-"
    if media_id.startswith(prefix):
        try:
            index = int(media_id[len(prefix) :])
        except ValueError:
            return None
        rows = obj.outbox_object_attachments
        if not (0 <= index < len(rows)):
            return None
        row = rows[index]
        return row.upload, str(row.filename), row.alt

    upload = await ids.get_upload_by_mastodon_id(db_session, media_id)
    if upload is None:
        return None
    return upload, serializers.synthetic_filename(upload), upload.description


@router.put("/api/v1/statuses/{status_id}", response_model=None)
async def statuses_update(
    status_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:statuses")),
) -> JSONResponse:
    obj = await ids.get_object_by_mastodon_id(db_session, status_id)
    if obj is None or not isinstance(obj, activitypub.models.OutboxObject):
        raise MastodonError(404, "not_found", "status not found")

    content_type, _, _ = request.headers.get("Content-Type", "").partition(";")
    if content_type.strip().lower() == "application/json":
        params = _StatusParams(await request.json(), None)
    else:
        params = _StatusParams(None, await request.form())

    content_value = params.get("status")
    content = str(content_value) if content_value is not None else ""
    if not content:
        raise MastodonError(422, "validation_failed", "status is required")

    content_warning_value = params.get("spoiler_text")
    content_warning = str(content_warning_value) if content_warning_value else None
    sensitive = params.get_bool("sensitive")
    media_attributes = params.get_media_attributes()

    # `uploads` is only passed to send_update() when the request actually
    # touches attachments: `media_ids` was in the body, or `media_attributes`
    # alone was sent to edit alt text/focus on the existing set without
    # changing which files are attached. Otherwise attachments must be left
    # alone, which is send_update()'s default (_UNSET), not "clear them"
    # (None/[]).
    if params.has("media_ids"):
        media_ids = [str(media_id) for media_id in params.get_list("media_ids")]
    elif media_attributes:
        media_ids = [
            ids.encode_upload_id(row.upload) for row in obj.outbox_object_attachments
        ]
    else:
        media_ids = None

    send_update_kwargs: dict[str, Any] = {}
    if media_ids is not None:
        uploads = []
        for media_id in media_ids:
            resolved = await _resolve_edit_media(db_session, obj, status_id, media_id)
            if resolved is None:
                raise MastodonError(
                    422, "validation_failed", f"unknown media id {media_id}"
                )
            upload, filename, alt = resolved

            attributes = media_attributes.get(media_id)
            if attributes is not None:
                if "description" in attributes:
                    alt = str(attributes["description"]) or None
                if "focus" in attributes:
                    focus = attributes["focus"]
                    if focus:
                        try:
                            upload.focus_x, upload.focus_y = _parse_focus(str(focus))
                        except ValueError as exc:
                            raise MastodonError(
                                422, "validation_failed", str(exc)
                            ) from exc
                    else:
                        upload.focus_x = upload.focus_y = None

            uploads.append((upload, filename, alt))
        send_update_kwargs["uploads"] = uploads

    await send_update(
        db_session,
        ap_id=obj.ap_id,
        source=content,
        content_warning=content_warning,
        is_sensitive=sensitive,
        **send_update_kwargs,
    )

    updated = await ids.get_object_by_mastodon_id(db_session, status_id)
    assert updated is not None
    return JSONResponse(
        content=await serializers.serialize_status(db_session, updated),
        status_code=200,
    )


@router.delete("/api/v1/statuses/{status_id}", response_model=None)
async def statuses_delete(
    status_id: str,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:statuses")),
) -> JSONResponse:
    obj = await ids.get_object_by_mastodon_id(db_session, status_id)
    if obj is None or not isinstance(obj, activitypub.models.OutboxObject):
        raise MastodonError(404, "not_found", "status not found")

    # Capture the source text for client-side redraft before deleting —
    # matches Mastodon's DELETE response, which includes the original text.
    serialized = await serializers.serialize_status(db_session, obj)
    serialized["text"] = obj.source or ""

    await send_delete(db_session, obj.ap_id)

    return JSONResponse(content=serialized, status_code=200)


# --- Scheduled statuses --------------------------------------------------------
# `POST /api/v1/statuses` with `scheduled_at` queues the post here instead of
# sending it; the outgoing-activity worker publishes it when due (see
# app/scheduled_statuses.py). These endpoints are the client's view of that
# queue.


def _decode_scheduled_status_id(mastodon_id: str) -> int | None:
    # A single table, so the Mastodon id is just the row's own PK — no
    # dual-table encoding needed (same as notifications).
    try:
        return int(mastodon_id)
    except ValueError:
        return None


async def _resolve_scheduled_status_or_404(
    db_session: AsyncSession,
    scheduled_status_id: str,
) -> models.ScheduledStatus:
    internal_id = _decode_scheduled_status_id(scheduled_status_id)
    scheduled_status = (
        await db_session.get(models.ScheduledStatus, internal_id)
        if internal_id is not None
        else None
    )
    if scheduled_status is None:
        raise MastodonError(404, "not_found", "scheduled status not found")
    return scheduled_status


@router.get("/api/v1/scheduled_statuses", response_model=None)
async def scheduled_statuses_index(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("read:statuses")),
) -> JSONResponse:
    """The queue, newest-queued first.

    Ordered by id descending rather than by publication time: that's what
    Mastodon's id-based pagination returns, and it's what the shared `Link`
    header helper assumes. Clients sort by `scheduled_at` themselves.
    """
    params = pagination.parse_pagination(request)

    query = (
        select(models.ScheduledStatus)
        .order_by(models.ScheduledStatus.id.desc())
        .limit(params.limit)
    )
    if params.max_id:
        decoded = _decode_scheduled_status_id(params.max_id)
        if decoded is not None:
            query = query.where(models.ScheduledStatus.id < decoded)
    cursor = params.min_id or params.since_id
    if cursor:
        decoded = _decode_scheduled_status_id(cursor)
        if decoded is not None:
            query = query.where(models.ScheduledStatus.id > decoded)

    rows = list((await db_session.scalars(query)).all())
    await serializers.prefetch_scheduled_status_uploads(db_session, rows)
    serialized = [
        await serializers.serialize_scheduled_status(db_session, row) for row in rows
    ]

    response = JSONResponse(content=serialized, status_code=200)
    link_header = pagination.build_link_header(
        request, [entity["id"] for entity in serialized]
    )
    if link_header:
        response.headers["Link"] = link_header
    return response


@router.get("/api/v1/scheduled_statuses/{scheduled_status_id}", response_model=None)
async def scheduled_statuses_show(
    scheduled_status_id: str,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("read:statuses")),
) -> JSONResponse:
    scheduled_status = await _resolve_scheduled_status_or_404(
        db_session, scheduled_status_id
    )
    return JSONResponse(
        content=await serializers.serialize_scheduled_status(
            db_session, scheduled_status
        ),
        status_code=200,
    )


@router.put("/api/v1/scheduled_statuses/{scheduled_status_id}", response_model=None)
async def scheduled_statuses_update(
    scheduled_status_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:statuses")),
) -> JSONResponse:
    """Move a queued post's publication time.

    Only `scheduled_at` is editable, same as upstream Mastodon — the compose
    parameters are fixed once queued. Rescheduling also clears any recorded
    failure, so a post that gave up trying to publish gets another chance.
    """
    scheduled_status = await _resolve_scheduled_status_or_404(
        db_session, scheduled_status_id
    )

    params = await _body_params(request)
    scheduled_at = _parse_scheduled_at(params)
    if scheduled_at is None:
        raise MastodonError(422, "validation_failed", "scheduled_at is required")

    scheduled_statuses.reschedule(scheduled_status, scheduled_at)
    await db_session.commit()

    return JSONResponse(
        content=await serializers.serialize_scheduled_status(
            db_session, scheduled_status
        ),
        status_code=200,
    )


@router.delete("/api/v1/scheduled_statuses/{scheduled_status_id}", response_model=None)
async def scheduled_statuses_delete(
    scheduled_status_id: str,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:statuses")),
) -> JSONResponse:
    scheduled_status = await _resolve_scheduled_status_or_404(
        db_session, scheduled_status_id
    )
    await db_session.delete(scheduled_status)
    await db_session.commit()
    return JSONResponse(content={}, status_code=200)


@router.post("/api/v1/statuses/{status_id}/favourite", response_model=None)
async def statuses_favourite(
    status_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:favourites")),
) -> JSONResponse:
    obj = await _get_visible_status_or_404(request, db_session, status_id)
    await send_like(db_session, obj.ap_id)
    return JSONResponse(
        content=await serializers.serialize_status(db_session, obj), status_code=200
    )


@router.post("/api/v1/statuses/{status_id}/unfavourite", response_model=None)
async def statuses_unfavourite(
    status_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:favourites")),
) -> JSONResponse:
    obj = await _get_visible_status_or_404(request, db_session, status_id)
    like_ap_id = getattr(obj, "liked_via_outbox_object_ap_id", None)
    if like_ap_id:
        await send_undo(db_session, like_ap_id)
    return JSONResponse(
        content=await serializers.serialize_status(db_session, obj), status_code=200
    )


@router.post("/api/v1/statuses/{status_id}/reblog", response_model=None)
async def statuses_reblog(
    status_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:statuses")),
) -> JSONResponse:
    obj = await _get_visible_status_or_404(request, db_session, status_id)
    await send_announce(db_session, obj.ap_id)
    return JSONResponse(
        content=await serializers.serialize_status(db_session, obj), status_code=200
    )


@router.post("/api/v1/statuses/{status_id}/unreblog", response_model=None)
async def statuses_unreblog(
    status_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:statuses")),
) -> JSONResponse:
    obj = await _get_visible_status_or_404(request, db_session, status_id)
    announce_ap_id = getattr(obj, "announced_via_outbox_object_ap_id", None)
    if announce_ap_id:
        await send_undo(db_session, announce_ap_id)
    return JSONResponse(
        content=await serializers.serialize_status(db_session, obj), status_code=200
    )


@router.post("/api/v1/statuses/{status_id}/bookmark", response_model=None)
async def statuses_bookmark(
    status_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:bookmarks")),
) -> JSONResponse:
    obj = await _get_visible_status_or_404(request, db_session, status_id)
    # OutboxObject has no is_bookmarked column — bookmarking one's own status
    # is a no-op (matches the existing HTML bookmark action, which only ever
    # operates on InboxObject too).
    if isinstance(obj, activitypub.models.InboxObject):
        obj.is_bookmarked = True
        await db_session.commit()
    return JSONResponse(
        content=await serializers.serialize_status(db_session, obj), status_code=200
    )


@router.post("/api/v1/statuses/{status_id}/unbookmark", response_model=None)
async def statuses_unbookmark(
    status_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:bookmarks")),
) -> JSONResponse:
    obj = await _get_visible_status_or_404(request, db_session, status_id)
    if isinstance(obj, activitypub.models.InboxObject):
        obj.is_bookmarked = False
        await db_session.commit()
    return JSONResponse(
        content=await serializers.serialize_status(db_session, obj), status_code=200
    )


@router.post("/api/v1/statuses/{status_id}/pin", response_model=None)
async def statuses_pin(
    status_id: str,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:accounts")),
) -> JSONResponse:
    obj = await ids.get_object_by_mastodon_id(db_session, status_id)
    if obj is None or not isinstance(obj, activitypub.models.OutboxObject):
        raise MastodonError(
            422, "validation_failed", "only your own statuses can be pinned"
        )
    if not obj.is_pinned:
        pinned_count = await db_session.scalar(
            select(func.count(activitypub.models.OutboxObject.id)).where(
                activitypub.models.OutboxObject.is_pinned.is_(True)
            )
        )
        if pinned_count >= activitypub.models.MAX_PINNED_OBJECTS:
            raise MastodonError(
                422,
                "validation_failed",
                "You have already pinned the maximum number of statuses.",
            )
    obj.is_pinned = True
    await db_session.commit()
    return JSONResponse(
        content=await serializers.serialize_status(db_session, obj), status_code=200
    )


@router.post("/api/v1/statuses/{status_id}/unpin", response_model=None)
async def statuses_unpin(
    status_id: str,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:accounts")),
) -> JSONResponse:
    obj = await ids.get_object_by_mastodon_id(db_session, status_id)
    if obj is None or not isinstance(obj, activitypub.models.OutboxObject):
        raise MastodonError(
            422, "validation_failed", "only your own statuses can be unpinned"
        )
    obj.is_pinned = False
    await db_session.commit()
    return JSONResponse(
        content=await serializers.serialize_status(db_session, obj), status_code=200
    )


@router.post("/api/v1/statuses/{status_id}/mute", response_model=None)
async def statuses_mute(
    status_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:mutes")),
) -> JSONResponse:
    obj = await _get_visible_status_or_404(request, db_session, status_id)
    conversation = obj.conversation or obj.ap_id
    existing = await db_session.scalar(
        select(models.MutedConversation).where(
            models.MutedConversation.conversation == conversation
        )
    )
    if existing is None:
        db_session.add(models.MutedConversation(conversation=conversation))
        await db_session.commit()
    return JSONResponse(
        content=await serializers.serialize_status(db_session, obj), status_code=200
    )


@router.post("/api/v1/statuses/{status_id}/unmute", response_model=None)
async def statuses_unmute(
    status_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:mutes")),
) -> JSONResponse:
    obj = await _get_visible_status_or_404(request, db_session, status_id)
    conversation = obj.conversation or obj.ap_id
    await db_session.execute(
        delete(models.MutedConversation).where(
            models.MutedConversation.conversation == conversation
        )
    )
    await db_session.commit()
    return JSONResponse(
        content=await serializers.serialize_status(db_session, obj), status_code=200
    )


@router.get("/api/v1/polls/{poll_id}", response_model=None)
async def polls_show(
    poll_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    obj = await _get_visible_status_or_404(request, db_session, poll_id)
    poll = serializers.serialize_poll(obj, poll_id)
    if poll is None:
        raise MastodonError(404, "not_found", "poll not found")
    return JSONResponse(content=poll, status_code=200)


@router.post("/api/v1/polls/{poll_id}/votes", response_model=None)
async def polls_vote(
    poll_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:statuses")),
) -> JSONResponse:
    obj = await ids.get_object_by_mastodon_id(db_session, poll_id)
    if obj is None or not obj.poll_items:
        raise MastodonError(404, "not_found", "poll not found")
    if not isinstance(obj, activitypub.models.InboxObject):
        # Your own poll. Mastodon rejects this too, and `send_vote` addresses
        # the poll author's inbox, which for an outbox poll is us — a 422 says
        # what happened, where the previous 404 claimed the poll didn't exist.
        raise MastodonError(
            422, "validation_failed", "you cannot vote in your own poll"
        )
    if obj.is_poll_ended:
        raise MastodonError(422, "validation_failed", "poll has ended")
    if obj.voted_for_answers:
        # One vote per poll, as in Mastodon. Without this a client's re-vote
        # (or a double tap) delivers a second set of vote activities to the
        # poll's author and overwrites the recorded answers; the HTML UI
        # enforces the same rule by hiding the form once you've voted
        # (`can_vote` in app/templates/utils.html).
        raise MastodonError(422, "validation_failed", "you have already voted")

    # Same JSON-or-form dance as every other write endpoint: clients disagree,
    # and `request.form()` alone silently yields nothing for a JSON body, which
    # turned a JSON client's vote into "choices is required".
    choices = (await _body_params(request)).get_list("choices")
    if not choices:
        raise MastodonError(422, "validation_failed", "choices is required")
    if obj.is_one_of_poll and len(choices) > 1:
        raise MastodonError(
            422, "validation_failed", "this poll only allows a single choice"
        )

    try:
        # Deduplicated, order preserved: a client that sends the same index
        # twice would otherwise have two vote activities delivered for one
        # answer, double-counting it on the poll author's server.
        indices = list(dict.fromkeys(int(str(choice)) for choice in choices))
    except ValueError:
        raise MastodonError(422, "validation_failed", "invalid choice index")

    names = []
    for index in indices:
        if index < 0 or index >= len(obj.poll_items):
            raise MastodonError(422, "validation_failed", "invalid choice index")
        names.append(obj.poll_items[index].get("name", ""))

    await send_vote(db_session, in_reply_to=obj.ap_id, names=names)

    poll = serializers.serialize_poll(obj, poll_id)
    if poll is None:
        raise MastodonError(404, "not_found", "poll not found")
    return JSONResponse(content=poll, status_code=200)


@router.get("/api/v1/bookmarks", response_model=None)
async def bookmarks_index(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("read:bookmarks")),
) -> JSONResponse:
    params = pagination.parse_pagination(request)
    query = (
        select(activitypub.models.InboxObject)
        .where(
            activitypub.models.InboxObject.is_bookmarked.is_(True),
            activitypub.models.InboxObject.is_deleted.is_(False),
        )
        .options(joinedload(activitypub.models.InboxObject.actor))
        .order_by(activitypub.models.InboxObject.id.desc())
        .limit(params.limit)
    )
    if params.max_id:
        decoded = ids.decode_object_id_for_source(params.max_id, ids.ObjectSource.INBOX)
        if decoded is not None:
            query = query.where(activitypub.models.InboxObject.id < decoded)
    cursor = params.min_id or params.since_id
    if cursor:
        decoded = ids.decode_object_id_for_source(cursor, ids.ObjectSource.INBOX)
        if decoded is not None:
            query = query.where(activitypub.models.InboxObject.id > decoded)

    items = (await db_session.scalars(query)).unique().all()
    return await _respond_with_status_list(request, db_session, items)


@router.get("/api/v1/favourites", response_model=None)
async def favourites_index(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("read:favourites")),
) -> JSONResponse:
    params = pagination.parse_pagination(request)
    query = (
        select(activitypub.models.InboxObject)
        .where(
            activitypub.models.InboxObject.liked_via_outbox_object_ap_id.is_not(None),
            activitypub.models.InboxObject.is_deleted.is_(False),
        )
        .options(joinedload(activitypub.models.InboxObject.actor))
        .order_by(activitypub.models.InboxObject.id.desc())
        .limit(params.limit)
    )
    if params.max_id:
        decoded = ids.decode_object_id_for_source(params.max_id, ids.ObjectSource.INBOX)
        if decoded is not None:
            query = query.where(activitypub.models.InboxObject.id < decoded)
    cursor = params.min_id or params.since_id
    if cursor:
        decoded = ids.decode_object_id_for_source(cursor, ids.ObjectSource.INBOX)
        if decoded is not None:
            query = query.where(activitypub.models.InboxObject.id > decoded)

    items = (await db_session.scalars(query)).unique().all()
    return await _respond_with_status_list(request, db_session, items)


# --- Social graph -------------------------------------------------------------


async def _find_own_follow_activity(
    db_session: AsyncSession, actor_ap_id: str
) -> activitypub.models.OutboxObject | None:
    """Find OUR OWN Follow activity targeting `actor_ap_id` (pending or
    accepted — the `Following` row only exists once accepted, but the Follow
    activity itself exists as soon as it's sent). `send_undo` needs this
    activity's own ap_id, not the target actor's.
    """
    return (
        await db_session.scalars(
            select(activitypub.models.OutboxObject)
            .where(
                activitypub.models.OutboxObject.ap_type == "Follow",
                activitypub.models.OutboxObject.activity_object_ap_id == actor_ap_id,
                activitypub.models.OutboxObject.undone_by_outbox_object_id.is_(None),
                activitypub.models.OutboxObject.is_deleted.is_(False),
            )
            .order_by(activitypub.models.OutboxObject.id.desc())
        )
    ).first()


async def _resolve_account_or_404(
    db_session: AsyncSession, account_id: str
) -> activitypub.models.Actor:
    if account_id == ids.LOCAL_ACTOR_ID:
        raise MastodonError(422, "validation_failed", "cannot target yourself")
    actor = await ids.get_account_by_mastodon_id(db_session, account_id)
    if actor is None:
        raise MastodonError(404, "not_found", "account not found")
    return actor


@router.post("/api/v1/accounts/{account_id}/follow", response_model=None)
async def accounts_follow(
    account_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:follows")),
) -> JSONResponse:
    actor = await _resolve_account_or_404(db_session, account_id)
    params = await _body_params(request)

    existing_following = (
        await db_session.scalars(
            select(activitypub.models.Following).where(
                activitypub.models.Following.ap_actor_id == actor.ap_id
            )
        )
    ).one_or_none()
    is_new_follow = (
        existing_following is None
        and await _find_own_follow_activity(db_session, actor.ap_id) is None
    )

    # On a new follow an absent param takes Mastodon's default; on an
    # existing one an absent param leaves the flag unchanged.
    if params.has("reblogs") or is_new_follow:
        actor.are_announces_hidden_from_stream = not (
            params.get_bool("reblogs") if params.has("reblogs") else True
        )
    if params.has("notify") or is_new_follow:
        actor.are_new_posts_notified = (
            params.get_bool("notify") if params.has("notify") else False
        )

    if is_new_follow:
        await send_follow(db_session, actor.ap_id)
    else:
        await db_session.commit()

    return JSONResponse(
        content=await _relationship_for_actor(db_session, account_id, actor),
        status_code=200,
    )


@router.post("/api/v1/accounts/{account_id}/unfollow", response_model=None)
async def accounts_unfollow(
    account_id: str,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:follows")),
) -> JSONResponse:
    actor = await _resolve_account_or_404(db_session, account_id)
    follow_activity = await _find_own_follow_activity(db_session, actor.ap_id)
    if follow_activity is not None:
        await send_undo(db_session, follow_activity.ap_id)
    return JSONResponse(
        content=await _relationship_for_actor(db_session, account_id, actor),
        status_code=200,
    )


@router.post("/api/v1/accounts/{account_id}/remove_from_followers", response_model=None)
async def accounts_remove_from_followers(
    account_id: str,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:follows")),
) -> JSONResponse:
    """Drop a follower without blocking them.

    Sends a Reject of their original Follow, so their server stops delivering
    to us; they're free to follow again (this instance's
    `manually_approves_followers` setting decides whether that needs approval).
    """
    actor = await _resolve_account_or_404(db_session, account_id)
    await remove_follower(db_session, actor)
    return JSONResponse(
        content=await _relationship_for_actor(db_session, account_id, actor),
        status_code=200,
    )


@router.post("/api/v1/accounts/{account_id}/block", response_model=None)
async def accounts_block(
    account_id: str,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:blocks")),
) -> JSONResponse:
    actor = await _resolve_account_or_404(db_session, account_id)
    if not actor.is_blocked:
        await send_block(db_session, actor.ap_id)
    return JSONResponse(
        content=await _relationship_for_actor(db_session, account_id, actor),
        status_code=200,
    )


@router.post("/api/v1/accounts/{account_id}/unblock", response_model=None)
async def accounts_unblock(
    account_id: str,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:blocks")),
) -> JSONResponse:
    actor = await _resolve_account_or_404(db_session, account_id)
    if actor.is_blocked:
        await send_unblock(db_session, actor.ap_id)
    return JSONResponse(
        content=await _relationship_for_actor(db_session, account_id, actor),
        status_code=200,
    )


@router.get("/api/v1/blocks", response_model=None)
async def blocks_index(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("read:blocks")),
) -> JSONResponse:
    """Blocked accounts.

    Unlike /api/v1/mutes (a stub, no mute model), blocks are persisted as
    `Actor.is_blocked`, so this is a real list — clients need it to show and
    undo blocks.

    Ordered by account id descending (most recently *seen* actor first) rather
    than by block time: nothing records when the flag was set, and Mastodon's
    max_id/since_id contract needs the ordering to follow the id anyway.
    """
    params = pagination.parse_pagination(request)
    query = _apply_account_cursor(
        select(activitypub.models.Actor)
        .where(
            activitypub.models.Actor.is_blocked.is_(True),
            activitypub.models.Actor.is_deleted.is_(False),
        )
        .order_by(activitypub.models.Actor.id.desc())
        .limit(params.limit),
        params,
    )

    actors = (await db_session.scalars(query)).unique().all()
    return await _respond_with_account_list(request, db_session, actors)


@router.get("/api/v1/mutes", response_model=None)
async def mutes_index(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("read:mutes")),
) -> JSONResponse:
    """Muted accounts.

    Same shape as /api/v1/blocks, including the ordering caveat: the cursor
    follows the account id (first-seen order), not when the mute was set.
    Mutes that have already expired are left out.
    """
    params = pagination.parse_pagination(request)
    query = _apply_account_cursor(
        select(activitypub.models.Actor)
        .where(
            activitypub.models.Actor.id.in_(activitypub.models.muted_actor_ids()),
            activitypub.models.Actor.is_deleted.is_(False),
        )
        .order_by(activitypub.models.Actor.id.desc())
        .limit(params.limit),
        params,
    )

    actors = (await db_session.scalars(query)).unique().all()
    return await _respond_with_account_list(request, db_session, actors)


@router.post("/api/v1/accounts/{account_id}/mute", response_model=None)
async def accounts_mute(
    account_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:mutes")),
) -> JSONResponse:
    actor = await _resolve_account_or_404(db_session, account_id)
    params = await _body_params(request)

    # Mastodon's defaults: mute notifications too, and last until unmuted.
    notifications = (
        params.get_bool("notifications") if params.has("notifications") else True
    )
    raw_duration = params.get("duration")
    try:
        duration = int(str(raw_duration)) if raw_duration else 0
    except ValueError:
        raise MastodonError(422, "validation_failed", "duration must be a number")

    await mute_actor(db_session, actor, duration=duration, notifications=notifications)
    return JSONResponse(
        content=await _relationship_for_actor(db_session, account_id, actor),
        status_code=200,
    )


@router.post("/api/v1/accounts/{account_id}/unmute", response_model=None)
async def accounts_unmute(
    account_id: str,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:mutes")),
) -> JSONResponse:
    actor = await _resolve_account_or_404(db_session, account_id)
    if actor.is_muted:
        await unmute_actor(db_session, actor)
    return JSONResponse(
        content=await _relationship_for_actor(db_session, account_id, actor),
        status_code=200,
    )


@router.get("/api/v1/domain_blocks", response_model=None)
async def domain_blocks_index(
    token_info: AccessTokenInfo = Depends(require_scope("read:blocks")),
) -> JSONResponse:
    """Blocked domains.

    Read-only: `blocked_servers` is static TOML (`data/profile.toml`), not a
    DB table, so there's nothing for POST/DELETE to mutate yet. Mirrors real
    Mastodon's shape for this endpoint: a plain array of hostnames, sorted
    for a stable response (config order isn't meaningful).
    """
    return JSONResponse(
        content=sorted(
            blocked_server.hostname for blocked_server in config.CONFIG.blocked_servers
        ),
        status_code=200,
    )


@router.post("/api/v1/accounts/{account_id}/note", response_model=None)
async def accounts_note(
    account_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:accounts")),
) -> JSONResponse:
    actor = await _resolve_account_or_404(db_session, account_id)
    form = await request.form()
    comment = form.get("comment")

    # An empty/missing comment clears the note, matching Mastodon.
    actor.note = str(comment) if comment else None
    await db_session.commit()

    return JSONResponse(
        content=await _relationship_for_actor(db_session, account_id, actor),
        status_code=200,
    )


async def _pending_follower_notification(
    db_session: AsyncSession, actor: activitypub.models.Actor
) -> models.Notification | None:
    return (
        await db_session.scalars(
            select(models.Notification)
            .where(
                models.Notification.notification_type
                == models.NotificationType.PENDING_INCOMING_FOLLOWER,
                models.Notification.actor_id == actor.id,
                models.Notification.is_accepted.is_(None),
                models.Notification.is_rejected.is_(None),
            )
            .options(joinedload(models.Notification.actor))
            .order_by(models.Notification.id.desc())
        )
    ).first()


@router.get("/api/v1/follow_requests", response_model=None)
async def follow_requests_index(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("read:follows")),
) -> JSONResponse:
    params = pagination.parse_pagination(request)
    query = (
        select(models.Notification)
        .where(
            models.Notification.notification_type
            == models.NotificationType.PENDING_INCOMING_FOLLOWER,
            models.Notification.is_accepted.is_(None),
            models.Notification.is_rejected.is_(None),
        )
        .options(joinedload(models.Notification.actor))
        .order_by(models.Notification.id.desc())
        .limit(params.limit)
    )
    notifications = (await db_session.scalars(query)).unique().all()

    accounts = [
        await serializers.serialize_account(db_session, notif.actor)
        for notif in notifications
        if notif.actor is not None
    ]
    return JSONResponse(content=accounts, status_code=200)


@router.post("/api/v1/follow_requests/{account_id}/authorize", response_model=None)
async def follow_requests_authorize(
    account_id: str,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:follows")),
) -> JSONResponse:
    actor = await _resolve_account_or_404(db_session, account_id)
    notif = await _pending_follower_notification(db_session, actor)
    if notif is None or notif.id is None:
        raise MastodonError(404, "not_found", "follow request not found")

    await send_accept(db_session, notif.id)
    return JSONResponse(
        content=await _relationship_for_actor(db_session, account_id, actor),
        status_code=200,
    )


@router.post("/api/v1/follow_requests/{account_id}/reject", response_model=None)
async def follow_requests_reject(
    account_id: str,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:follows")),
) -> JSONResponse:
    actor = await _resolve_account_or_404(db_session, account_id)
    notif = await _pending_follower_notification(db_session, actor)
    if notif is None or notif.id is None:
        raise MastodonError(404, "not_found", "follow request not found")

    await send_reject(db_session, notif.id)
    return JSONResponse(
        content=await _relationship_for_actor(db_session, account_id, actor),
        status_code=200,
    )


# --- Search --------------------------------------------------------------------


def _query_pattern(query: str) -> str:
    """`query` as a `GLOB` substring pattern against already-normalized
    (NFC + casefolded) `search_text` columns -- see `app/utils/search_text.py`.
    `GLOB`, not `LIKE ... ESCAPE`: the FTS5 trigram tokenizer backing
    `*_search` only serves a query the planner marks `L0` (plain `LIKE`) or
    `G0` (`GLOB`); an `ESCAPE` clause silently drops it back to a full scan."""
    return glob_pattern(normalize(query))


async def _search_accounts(
    db_session: AsyncSession, query: str, limit: int
) -> list[dict]:
    # Matched through the `actor_search` FTS5 trigram index rather than
    # loading every cached actor to scan it here: that used to decode each
    # row's `ap_actor` JSON on the event loop, and with a single worker
    # process that stalled *every* concurrent request for the duration
    # (measured over 20k actors: ~860ms per call, ~640ms of it a hard loop
    # stall). Clients like Ice Cubes search on every keystroke, so those
    # stalls overlapped and the instance stopped responding altogether.
    pattern = _query_pattern(query.lstrip("@"))
    model = activitypub.models.Actor
    matches = (
        await db_session.scalars(
            select(model)
            .where(
                model.id.in_(
                    select(activitypub.models.ACTOR_SEARCH.c.rowid).where(
                        activitypub.models.matches_search(
                            activitypub.models.ACTOR_SEARCH, pattern
                        )
                    )
                )
            )
            # Insertion order, which is what `matches[:limit]` off an unordered
            # scan effectively returned before.
            .order_by(model.id)
            .limit(limit)
        )
    ).all()
    return [await serializers.serialize_account(db_session, actor) for actor in matches]


async def _search_statuses(
    db_session: AsyncSession, query: str, limit: int
) -> list[dict]:
    # Matched through the `inbox_search`/`outbox_search` FTS5 trigram
    # indexes, pushed into SQL rather than run over a page of hydrated ORM
    # objects. That both keeps the JSON decode off the event loop and drops
    # the old scan window: matching the newest 100 rows per box meant search
    # silently never found anything older.
    pattern = _query_pattern(query)
    inbox_items = await timelines.fetch_inbox_timeline_page(
        db_session,
        before=None,
        after=None,
        limit=limit,
        extra_where=(
            activitypub.models.InboxObject.visibility == ap.VisibilityEnum.PUBLIC,
            activitypub.models.InboxObject.id.in_(
                select(activitypub.models.INBOX_SEARCH.c.rowid).where(
                    activitypub.models.matches_search(
                        activitypub.models.INBOX_SEARCH, pattern
                    )
                )
            ),
        ),
    )
    outbox_items = await timelines.fetch_outbox_timeline_page(
        db_session,
        before=None,
        after=None,
        limit=limit,
        extra_where=(
            activitypub.models.OutboxObject.visibility == ap.VisibilityEnum.PUBLIC,
            activitypub.models.OutboxObject.id.in_(
                select(activitypub.models.OUTBOX_SEARCH.c.rowid).where(
                    activitypub.models.matches_search(
                        activitypub.models.OUTBOX_SEARCH, pattern
                    )
                )
            ),
        ),
    )
    combined: list[AnyboxObject] = [*inbox_items, *outbox_items]
    combined.sort(key=timelines.status_id_int, reverse=True)
    page = combined[:limit]
    await serializers.prefetch_status_relations(db_session, page)
    return [await serializers.serialize_status(db_session, obj) for obj in page]


# Long enough for a healthy remote to answer, short enough that a dead host
# can't pin the request: `lookup()` may webfinger and then fetch, each with
# httpx's own 5s timeout, and a client searching per keystroke fires this at
# every half-typed handle.
_RESOLVE_TIMEOUT = 5.0


async def _resolve_remote(db_session: AsyncSession, query: str):
    try:
        return await asyncio.wait_for(lookup(db_session, query), _RESOLVE_TIMEOUT)
    except Exception:
        # Network/parse failures (and the timeout above) just mean "nothing
        # resolved" — search must not 500, or hang, because a query isn't a
        # fetchable handle/URL.
        return None


@router.get("/api/v2/search", response_model=None)
async def search(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("read:search")),
) -> JSONResponse:
    query = (request.query_params.get("q") or "").strip()
    if not query:
        raise MastodonError(422, "validation_failed", "q is required")

    search_type = request.query_params.get("type")
    resolve = request.query_params.get("resolve") == "true"
    try:
        limit = min(max(int(request.query_params.get("limit", "20")), 1), 40)
    except ValueError:
        limit = 20

    accounts: list[dict] = []
    statuses: list[dict] = []
    hashtags: list[dict] = []

    if search_type in (None, "accounts"):
        accounts = await _search_accounts(db_session, query, limit)
    if search_type in (None, "statuses"):
        statuses = await _search_statuses(db_session, query, limit)
    if search_type in (None, "hashtags"):
        tag = query.lstrip("#").strip().lower()
        if tag:
            # No per-day usage history is tracked; this just confirms the
            # query looks like a taggable hashtag. Shares `_serialize_tag`
            # with `tags_show` so the entity shape can't drift between the
            # two places a Tag is emitted.
            hashtags = [_serialize_tag(tag)]

    need_accounts = search_type in (None, "accounts") and not accounts
    need_statuses = search_type in (None, "statuses") and not statuses
    if resolve and (need_accounts or need_statuses):
        resolved = await _resolve_remote(db_session, query)
        if isinstance(resolved, RemoteActor) and need_accounts:
            try:
                actor_row = await fetch_actor(db_session, resolved.ap_id)
            except Exception:
                actor_row = None
            if actor_row is not None:
                accounts = [await serializers.serialize_account(db_session, actor_row)]
        elif (
            isinstance(resolved, RemoteObject)
            and not isinstance(resolved, RemoteActor)
            and need_statuses
        ):
            cached = await get_anybox_object_by_ap_id(db_session, resolved.ap_id)
            if cached is None:
                cached = await save_object_to_inbox(db_session, resolved.ap_object)
                await db_session.commit()
            # A remote object's ap_id never matches our own BASE_URL, so
            # get_anybox_object_by_ap_id always resolves it via the inbox
            # path (see its implementation) — this is just narrowing that
            # for mypy, not a runtime possibility.
            if isinstance(cached, activitypub.models.InboxObject):
                # Re-fetch through the eager-loading helper (see PR-2b's
                # populate_existing fix) — `cached` may not have `.actor`
                # loaded.
                reloaded = await ids.get_object_by_mastodon_id(
                    db_session, ids.encode_inbox_id(cached)
                )
                if reloaded is not None:
                    statuses = [
                        await serializers.serialize_status(db_session, reloaded)
                    ]

    return JSONResponse(
        content={"accounts": accounts, "statuses": statuses, "hashtags": hashtags},
        status_code=200,
    )
