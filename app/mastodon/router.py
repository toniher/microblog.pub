"""Mastodon client REST API — /api/v1 and /api/v2 endpoints.

Grown incrementally across build phases; see PLAN-0.md for the full map.
This module currently covers Phase 0's instance/meta surface and Phase 1a's
accounts/relationships surface.
"""

from datetime import datetime
from datetime import timezone
from urllib.parse import urlparse

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from starlette.responses import JSONResponse

import activitypub.models
from activitypub.actor import get_actors_metadata
from app import config
from app.database import AsyncSession
from app.database import get_db_session
from app.indieauth import AccessTokenInfo
from app.mastodon import ids
from app.mastodon import pagination
from app.mastodon import serializers
from app.mastodon.errors import MastodonError
from app.mastodon.scopes import require_scope
from app.utils.emoji import EMOJIS

router = APIRouter()

# Advisory client hints only — nothing here is enforced server-side. The
# backend has no hard cap on note/article length, so max_characters is set
# generously rather than mirroring Mastodon's 500.
_INSTANCE_CONFIGURATION = {
    "statuses": {
        "max_characters": 100_000,
        "max_media_attachments": 4,
        "characters_reserved_per_url": 23,
    },
    "media_attachments": {
        "supported_mime_types": [
            "image/jpeg",
            "image/png",
            "image/gif",
            "image/webp",
        ],
        "image_size_limit": 10_485_760,
        "image_matrix_limit": 16_777_216,
        "video_size_limit": 41_943_040,
        "video_frame_rate_limit": 60,
        "video_matrix_limit": 2_304_000,
    },
    "polls": {
        "max_options": 4,
        "max_characters_per_option": 100,
        "min_expiration": 300,
        "max_expiration": 2_629_746,
    },
}

# Mirrors pyproject.toml's [tool.poetry].repository; not parsed dynamically
# since it's a cosmetic field only shown on some clients' "about" screens.
_SOURCE_URL = "https://github.com/toniher/microblog.pub"


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
            "version": f"{config.VERSION} (compatible; microblogpub {config.VERSION})",
            # No `streaming_api` key: the streaming API isn't implemented, so
            # clients fall back to polling (see PLAN-0.md).
            "urls": {},
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
            "configuration": _INSTANCE_CONFIGURATION,
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
            "version": f"{config.VERSION} (compatible; microblogpub {config.VERSION})",
            "source_url": _SOURCE_URL,
            "description": config.CONFIG.summary,
            "usage": {"users": {"active_month": 1}},
            "thumbnail": {
                "url": thumbnail_url,
                "blurhash": None,
                "versions": {},
            },
            "languages": [config.LANGUAGE_CODE],
            "configuration": _INSTANCE_CONFIGURATION,
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


# Markers aren't persisted (no schema for cross-device read-position sync
# yet); GET always reports none saved, POST accepts and echoes back what was
# sent without storing it, so well-behaved clients don't error out.
_MARKER_TIMELINES = ("home", "notifications")


@router.get("/api/v1/markers", response_model=None)
async def get_markers(
    token_info: AccessTokenInfo = Depends(require_scope("read:statuses")),
) -> JSONResponse:
    return JSONResponse(content={}, status_code=200)


@router.post("/api/v1/markers", response_model=None)
async def post_markers(
    request: Request,
    token_info: AccessTokenInfo = Depends(require_scope("write:statuses")),
) -> JSONResponse:
    form_data = await request.form()
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    content = {}
    for timeline in _MARKER_TIMELINES:
        last_read_id = form_data.get(f"{timeline}[last_read_id]")
        if last_read_id is not None:
            content[timeline] = {
                "last_read_id": str(last_read_id),
                "version": 1,
                "updated_at": now_iso,
            }

    return JSONResponse(content=content, status_code=200)


# --- Accounts + relationships -----------------------------------------------
# Static-path routes (verify_credentials/relationships/lookup) are registered
# before the dynamic "/{account_id}" ones below so FastAPI doesn't try to
# match them as an account id.


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
        "follow_requests_count": 0,
    }
    return JSONResponse(content=account, status_code=200)


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
                {
                    "id": ids.LOCAL_ACTOR_ID,
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
            meta = metadata.get(actor.ap_id)
            relationships.append(
                {
                    "id": raw_id,
                    "following": meta.is_following if meta else False,
                    "showing_reblogs": True,
                    "notifying": False,
                    "followed_by": meta.is_follower if meta else False,
                    "blocking": actor.is_blocked,
                    "blocked_by": meta.has_blocked_local_actor if meta else False,
                    "muting": False,
                    "muting_notifications": False,
                    "requested": meta.is_follow_request_sent if meta else False,
                    "domain_blocking": False,
                    "endorsed": False,
                    "note": "",
                }
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

    return JSONResponse(
        content=await serializers.serialize_account(db_session, actor),
        status_code=200,
    )


async def _paginated_actor_list(
    request: Request,
    db_session: AsyncSession,
    *,
    model: type[activitypub.models.Follower] | type[activitypub.models.Following],
    join_column,
) -> JSONResponse:
    params = pagination.parse_pagination(request)
    query = (
        select(model)
        .join(activitypub.models.Actor, join_column == activitypub.models.Actor.id)
        .options(joinedload(model.actor))
        .order_by(activitypub.models.Actor.id.desc())
        .limit(params.limit)
    )
    if params.max_id:
        decoded = ids.decode_account_id(params.max_id)
        if decoded is not None:
            query = query.where(activitypub.models.Actor.id < decoded)

    cursor = params.min_id or params.since_id
    if cursor:
        decoded = ids.decode_account_id(cursor)
        if decoded is not None:
            query = query.where(activitypub.models.Actor.id > decoded)

    rows = (await db_session.scalars(query)).unique().all()
    accounts = [
        await serializers.serialize_account(db_session, row.actor) for row in rows
    ]

    response = JSONResponse(content=accounts, status_code=200)
    link_header = pagination.build_link_header(
        request, [account["id"] for account in accounts]
    )
    if link_header:
        response.headers["Link"] = link_header
    return response


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
