"""Mastodon client REST API — /api/v1 and /api/v2 endpoints.

Grown incrementally across build phases; see PLAN-0.md for the full map.
This module currently covers Phase 0's instance/meta surface, Phase 1a's
accounts/relationships surface, Phase 1b's timelines/statuses surface,
Phase 1c's notifications + read-degradation surface, Phase 2a's media
upload surface, Phase 2b's status-write surface, and Phase 3's social
graph + search surface.
"""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import cast
from urllib.parse import urlparse

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi import UploadFile as FastAPIUploadFile
from loguru import logger
from sqlalchemy import delete
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
from activitypub.ap_object import RemoteObject
from activitypub.boxes import AnyboxObject
from activitypub.boxes import ReplyTreeNode
from activitypub.boxes import get_anybox_object_by_ap_id
from activitypub.boxes import get_replies_tree
from activitypub.boxes import prefetch_actor_outbox
from activitypub.boxes import save_object_to_inbox
from activitypub.boxes import send_accept
from activitypub.boxes import send_announce
from activitypub.boxes import send_block
from activitypub.boxes import send_create
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
from app.database import AsyncSession
from app.database import get_db_session
from app.indieauth import AccessTokenInfo
from app.indieauth import check_access_token
from app.lookup import lookup
from app.mastodon import ids
from app.mastodon import pagination
from app.mastodon import serializers
from app.mastodon.errors import MastodonError
from app.mastodon.scopes import require_scope
from app.uploads import save_upload
from app.utils.datetime import as_utc
from app.utils.datetime import now
from app.utils.emoji import EMOJIS

router = APIRouter()
_TIMELINE_OBJECT_TYPES = ["Announce", "Article", "Note", "Page", "Question", "Video"]

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
        "showing_reblogs": True,
        "notifying": False,
        "followed_by": meta.is_follower if meta else False,
        "blocking": actor.is_blocked,
        "blocked_by": meta.has_blocked_local_actor if meta else False,
        # No mute model exists — always false, matching the /api/v1/mutes
        # stub (see PR-1c).
        "muting": False,
        "muting_notifications": False,
        "requested": meta.is_follow_request_sent if meta else False,
        "domain_blocking": False,
        "endorsed": False,
        "note": "",
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

    return JSONResponse(
        content=await serializers.serialize_account(db_session, actor),
        status_code=200,
    )


async def _respond_with_status_list(
    request: Request, db_session: AsyncSession, objects: list
) -> JSONResponse:
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
                activitypub.models.OutboxObject.ap_type.in_(_TIMELINE_OBJECT_TYPES),
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

    def _build_query() -> Any:
        query = (
            select(activitypub.models.InboxObject)
            .where(
                activitypub.models.InboxObject.ap_actor_id == actor.ap_id,
                activitypub.models.InboxObject.is_deleted.is_(False),
                activitypub.models.InboxObject.ap_type.in_(_TIMELINE_OBJECT_TYPES),
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
            logger.exception(f"Failed to backfill outbox for {actor.ap_id}")
            await db_session.rollback()
        else:
            items = (await db_session.scalars(_build_query())).unique().all()

    return await _respond_with_status_list(request, db_session, items)


_OUTBOX_BACKFILL_TTL = timedelta(hours=1)


def _should_backfill_outbox(actor: activitypub.models.Actor) -> bool:
    if actor.outbox_backfilled_at is None:
        return True
    return now() - as_utc(actor.outbox_backfilled_at) > _OUTBOX_BACKFILL_TTL


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

    tree = await get_replies_tree(db_session, obj, is_admin)
    found = _find_node_with_ancestors(tree, obj.ap_id, [])

    ancestor_nodes: list[ReplyTreeNode] = []
    descendant_nodes: list[ReplyTreeNode] = []
    if found is not None:
        requested_node, ancestor_nodes = found
        descendant_nodes = _flatten_descendants(requested_node)

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


def _status_id_int(obj: AnyboxObject) -> int:
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


async def _fetch_inbox_timeline_page(
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
            activitypub.models.InboxObject.ap_type.in_(_TIMELINE_OBJECT_TYPES),
            activitypub.models.InboxObject.is_hidden_from_stream.is_(False),
            activitypub.models.InboxObject.is_deleted.is_(False),
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


async def _fetch_outbox_timeline_page(
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
            activitypub.models.OutboxObject.ap_type.in_(_TIMELINE_OBJECT_TYPES),
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
    inbox_items = await _fetch_inbox_timeline_page(
        db_session, before=before, after=after, limit=params.limit
    )
    outbox_items = await _fetch_outbox_timeline_page(
        db_session, before=before, after=after, limit=params.limit
    )
    combined: list[AnyboxObject] = [*inbox_items, *outbox_items]
    merged = sorted(
        combined,
        key=_status_id_int,
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
        query = (
            select(activitypub.models.OutboxObject)
            .where(
                activitypub.models.OutboxObject.ap_type.in_(_TIMELINE_OBJECT_TYPES),
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
    inbox_items = await _fetch_inbox_timeline_page(
        db_session,
        before=before,
        after=after,
        limit=params.limit,
        extra_where=(
            activitypub.models.InboxObject.visibility == ap.VisibilityEnum.PUBLIC,
        ),
    )
    outbox_items = await _fetch_outbox_timeline_page(
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
        key=_status_id_int,
        reverse=True,
    )[: params.limit]

    return await _respond_with_status_list(request, db_session, merged)


@router.get("/api/v1/timelines/tag/{hashtag}", response_model=None)
async def timelines_tag(
    hashtag: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    params = pagination.parse_pagination(request)
    wanted = hashtag.lstrip("#").lower()

    # Hashtags live inside the ap_object JSON blob, not a queryable column, so
    # this scans a bounded recent-public-posts window and filters in Python
    # rather than pushing the predicate into SQL. Fine for a single-user
    # instance's post volume; not a real search index.
    before = await _resolve_cursor_published_at(db_session, params.max_id)
    after = await _resolve_cursor_published_at(
        db_session, params.min_id or params.since_id
    )
    scan_limit = max(params.limit * 5, 100)

    inbox_items = await _fetch_inbox_timeline_page(
        db_session,
        before=before,
        after=after,
        limit=scan_limit,
        extra_where=(
            activitypub.models.InboxObject.visibility == ap.VisibilityEnum.PUBLIC,
        ),
    )
    outbox_items = await _fetch_outbox_timeline_page(
        db_session,
        before=before,
        after=after,
        limit=scan_limit,
        extra_where=(
            activitypub.models.OutboxObject.visibility == ap.VisibilityEnum.PUBLIC,
        ),
    )

    def _has_tag(obj: AnyboxObject) -> bool:
        return any(
            tag.get("type") == "Hashtag"
            and tag.get("name", "").lstrip("#").lower() == wanted
            for tag in obj.tags
        )

    combined: list[AnyboxObject] = [*inbox_items, *outbox_items]
    candidates = [obj for obj in combined if _has_tag(obj)]
    merged = sorted(candidates, key=_status_id_int, reverse=True)[: params.limit]

    return await _respond_with_status_list(request, db_session, merged)


# --- Notifications -------------------------------------------------------------

# Only these carry a real Mastodon equivalent. Everything else (undo_*,
# webmention_*, block/unblock, unfollow, follow_request_accepted/rejected) has
# no matching Mastodon notification type, so it's filtered out entirely
# rather than surfaced with a made-up/incorrect `type`.
_NOTIFICATION_TYPE_MAP = {
    models.NotificationType.NEW_FOLLOWER: "follow",
    models.NotificationType.PENDING_INCOMING_FOLLOWER: "follow_request",
    models.NotificationType.LIKE: "favourite",
    models.NotificationType.ANNOUNCE: "reblog",
    models.NotificationType.MENTION: "mention",
    models.NotificationType.MOVE: "move",
}

_NOTIFICATION_OPTIONS = [
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


def _decode_notification_id(mastodon_id: str) -> int | None:
    # Notifications are a single table (unlike statuses/accounts), so the
    # Mastodon id is just the row's own PK — no dual-table encoding needed.
    try:
        return int(mastodon_id)
    except ValueError:
        return None


async def _serialize_notification(
    db_session: AsyncSession, notification: models.Notification
) -> dict | None:
    if notification.notification_type is None or notification.actor is None:
        return None

    mastodon_type = _NOTIFICATION_TYPE_MAP.get(notification.notification_type)
    if mastodon_type is None:
        return None

    created_at = notification.created_at or datetime.min.replace(tzinfo=timezone.utc)
    result = {
        "id": str(notification.id),
        "type": mastodon_type,
        "created_at": serializers.format_datetime(created_at),
        "account": await serializers.serialize_account(db_session, notification.actor),
    }

    target = notification.outbox_object or notification.inbox_object
    if target is not None:
        result["status"] = await serializers.serialize_status(db_session, target)

    return result


@router.get("/api/v1/notifications", response_model=None)
async def notifications_list(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("read:notifications")),
) -> JSONResponse:
    params = pagination.parse_pagination(request)
    include_types = set(request.query_params.getlist("types[]"))
    exclude_types = set(request.query_params.getlist("exclude_types[]"))

    allowed_internal_types = list(_NOTIFICATION_TYPE_MAP.keys())
    if include_types:
        allowed_internal_types = [
            t
            for t in allowed_internal_types
            if _NOTIFICATION_TYPE_MAP[t] in include_types
        ]
    if exclude_types:
        allowed_internal_types = [
            t
            for t in allowed_internal_types
            if _NOTIFICATION_TYPE_MAP[t] not in exclude_types
        ]

    query = (
        select(models.Notification)
        .where(models.Notification.notification_type.in_(allowed_internal_types))
        .options(*_NOTIFICATION_OPTIONS)
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
        if (entity := await _serialize_notification(db_session, notif)) is not None
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


@router.get("/api/v1/notifications/{notification_id}", response_model=None)
async def notifications_show(
    notification_id: str,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("read:notifications")),
) -> JSONResponse:
    internal_id = _decode_notification_id(notification_id)
    notification = (
        await db_session.get(
            models.Notification, internal_id, options=_NOTIFICATION_OPTIONS
        )
        if internal_id is not None
        else None
    )
    serialized = (
        await _serialize_notification(db_session, notification)
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


# --- Conversations ---------------------------------------------------------------
# Mastodon's DM inbox: one entry per `ap_context` thread of direct-visibility
# statuses. There's no dedicated "conversation" table, so threads are grouped
# the same way `app.admin.admin_direct_messages` builds the existing HTML view.


async def _dm_thread_unread_contexts(db_session: AsyncSession) -> set[str]:
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


async def _dm_threads(
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

    unread_contexts = await _dm_thread_unread_contexts(db_session)

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
            max(thread["objects"], key=_status_id_int),
            thread["actor_ids"],
            context in unread_contexts,
        )
        for context, thread in by_context.items()
    ]
    threads.sort(key=lambda item: _status_id_int(item[0]), reverse=True)
    return threads


async def _serialize_conversation(
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
        "accounts": [
            await serializers.serialize_account(db_session, actor) for actor in actors
        ],
        "last_status": await serializers.serialize_status(db_session, last),
    }


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
    threads = await _dm_threads(db_session)

    if (max_int := _safe_id_int(params.max_id)) is not None:
        threads = [t for t in threads if _status_id_int(t[0]) < max_int]
    if (cursor_int := _safe_id_int(params.min_id or params.since_id)) is not None:
        threads = [t for t in threads if _status_id_int(t[0]) > cursor_int]
    threads = threads[: params.limit]

    serialized = [
        await _serialize_conversation(db_session, last, actor_ids, unread)
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

    threads = await _dm_threads(db_session)
    match = next((t for t in threads if t[0].ap_context == obj.ap_context), None)
    if match is None:
        raise MastodonError(404, "not_found", "conversation not found")
    last, actor_ids, _ = match
    return JSONResponse(
        content=await _serialize_conversation(db_session, last, actor_ids, False),
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


@router.get("/api/v1/mutes", response_model=None)
async def mutes_index(
    token_info: AccessTokenInfo = Depends(require_scope("read")),
) -> JSONResponse:
    return JSONResponse(content=[], status_code=200)


# follow_requests is a real, non-stub list — see the "Social graph" section
# below (PR-3), which also lands authorize/reject.


# Public, unauthenticated in real Mastodon too.


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
    upload = await save_upload(db_session, cast(FastAPIUploadFile, file))
    if upload is None:
        raise MastodonError(422, "validation_failed", "unable to process upload")

    description = form.get("description")
    if description:
        upload.description = str(description)
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


class _StatusParams:
    """Normalizes the POST /api/v1/statuses body across content types.

    The Mastodon API accepts this endpoint as `multipart/form-data`,
    `application/x-www-form-urlencoded`, or `application/json` — clients
    disagree on which they use (e.g. Tusky sends JSON; Fedilab sends form
    data). Starlette's `Request.form()` silently returns an empty `FormData`
    for a JSON body rather than raising, which was turning every JSON-body
    post into a 422 "status is required".
    """

    def __init__(self, json_body: dict[str, Any] | None, form: Any) -> None:
        self._json = json_body
        self._form = form

    def get(self, key: str) -> Any:
        if self._json is not None:
            return self._json.get(key)
        return self._form.get(key)

    def get_bool(self, key: str) -> bool:
        value = self.get(key)
        if isinstance(value, bool):
            return value
        return str(value).lower() == "true"

    def get_list(self, key: str) -> list[Any]:
        if self._json is not None:
            value = self._json.get(key)
            return list(value) if value else []
        return self._form.getlist(f"{key}[]") or self._form.getlist(key)

    def has(self, key: str) -> bool:
        """Whether `key` was present in the body at all — distinct from being
        present-but-empty. Used for fields where an edit should only touch
        the existing value if the client actually sent something for it
        (e.g. `media_ids`, where absence must mean "leave attachments alone",
        not "clear them").
        """
        if self._json is not None:
            return key in self._json
        return f"{key}[]" in self._form or key in self._form

    def get_poll_options(self) -> list[str]:
        if self._json is not None:
            options = (self._json.get("poll") or {}).get("options") or []
        else:
            options = self._form.getlist("poll[options][]")
        return [str(option) for option in options]

    def get_poll_multiple(self) -> bool:
        if self._json is not None:
            return bool((self._json.get("poll") or {}).get("multiple"))
        return str(self._form.get("poll[multiple]", "")).lower() == "true"

    def get_poll_expires_in_seconds(self) -> int | None:
        if self._json is not None:
            value = (self._json.get("poll") or {}).get("expires_in")
        else:
            value = self._form.get("poll[expires_in]")
        return int(str(value)) if value else None


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
    if cache_key and cache_key in _IDEMPOTENCY_CACHE:
        cached = await ids.get_object_by_mastodon_id(
            db_session, _IDEMPOTENCY_CACHE[cache_key]
        )
        if cached is not None:
            return JSONResponse(
                content=await serializers.serialize_status(db_session, cached),
                status_code=200,
            )

    content_type, _, _ = request.headers.get("Content-Type", "").partition(";")
    if content_type.strip().lower() == "application/json":
        params = _StatusParams(await request.json(), None)
    else:
        params = _StatusParams(None, await request.form())

    content_value = params.get("status")
    content = str(content_value) if content_value is not None else ""
    content_warning_value = params.get("spoiler_text")
    content_warning = str(content_warning_value) if content_warning_value else None
    sensitive = params.get_bool("sensitive")

    media_ids = params.get_list("media_ids")
    uploads = []
    for media_id in media_ids:
        upload = await ids.get_upload_by_mastodon_id(db_session, str(media_id))
        if upload is None:
            raise MastodonError(
                422, "validation_failed", f"unknown media id {media_id}"
            )
        uploads.append(
            (upload, serializers.synthetic_filename(upload), upload.description)
        )

    # Mirrors the existing HTML new-post form (app/admin.py): a CW with no
    # body text but attached media becomes the visible content instead.
    if not content and content_warning and uploads:
        content = content_warning
        sensitive = True
        content_warning = None

    if not content:
        raise MastodonError(422, "validation_failed", "status is required")

    in_reply_to_id = params.get("in_reply_to_id")
    in_reply_to = None
    if in_reply_to_id:
        parent = await ids.get_object_by_mastodon_id(db_session, str(in_reply_to_id))
        if parent is None:
            raise MastodonError(422, "validation_failed", "in_reply_to_id not found")
        in_reply_to = parent.ap_id

    visibility_param = str(params.get("visibility") or "public")
    visibility = _MASTODON_VISIBILITY_TO_AP.get(visibility_param)
    if visibility is None:
        raise MastodonError(422, "validation_failed", "invalid visibility")

    language_value = params.get("language")
    language = str(language_value) if language_value else None

    ap_type = "Note"
    poll_type = None
    poll_answers = None
    poll_duration_in_minutes = None
    poll_options = params.get_poll_options()
    if poll_options:
        ap_type = "Question"
        poll_answers = poll_options
        if len(poll_answers) < 2:
            raise MastodonError(
                422, "validation_failed", "poll must have at least 2 options"
            )
        poll_type = "anyOf" if params.get_poll_multiple() else "oneOf"
        expires_in_seconds = params.get_poll_expires_in_seconds() or 3600
        # send_create takes whole minutes; never round a short-lived poll
        # down to 0 (which would mean "no expiration" / immediately expired).
        poll_duration_in_minutes = max(1, expires_in_seconds // 60)

    _, outbox_object = await send_create(
        db_session,
        ap_type=ap_type,
        source=content,
        uploads=uploads,
        in_reply_to=in_reply_to,
        visibility=visibility,
        content_warning=content_warning,
        is_sensitive=True if content_warning else sensitive,
        poll_type=poll_type,
        poll_answers=poll_answers,
        poll_duration_in_minutes=poll_duration_in_minutes,
        name=None,
        language=language,
    )

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

    # `uploads` is only passed to send_update() when `media_ids` was actually
    # in the request body — its absence must mean "leave attachments alone",
    # which is send_update()'s default (_UNSET), not "clear them" (None/[]).
    send_update_kwargs: dict[str, Any] = {}
    if params.has("media_ids"):
        uploads = []
        for media_id in params.get_list("media_ids"):
            upload = await ids.get_upload_by_mastodon_id(db_session, str(media_id))
            if upload is None:
                raise MastodonError(
                    422, "validation_failed", f"unknown media id {media_id}"
                )
            uploads.append(
                (upload, serializers.synthetic_filename(upload), upload.description)
            )
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
    # send_vote only supports voting on a remote (inbox) poll — matches the
    # existing HTML poll-vote action's own capability.
    obj = await ids.get_object_by_mastodon_id(db_session, poll_id)
    if (
        obj is None
        or not isinstance(obj, activitypub.models.InboxObject)
        or not obj.poll_items
    ):
        raise MastodonError(404, "not_found", "poll not found")
    if obj.is_poll_ended:
        raise MastodonError(422, "validation_failed", "poll has ended")

    form = await request.form()
    choices = form.getlist("choices[]") or form.getlist("choices")
    if not choices:
        raise MastodonError(422, "validation_failed", "choices is required")
    if obj.is_one_of_poll and len(choices) > 1:
        raise MastodonError(
            422, "validation_failed", "this poll only allows a single choice"
        )

    try:
        indices = [int(str(choice)) for choice in choices]
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
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:follows")),
) -> JSONResponse:
    actor = await _resolve_account_or_404(db_session, account_id)
    await send_follow(db_session, actor.ap_id)
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


@router.post("/api/v1/accounts/{account_id}/mute", response_model=None)
async def accounts_mute(
    account_id: str,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:mutes")),
) -> JSONResponse:
    # No mute model exists (see PR-1c's /api/v1/mutes stub) — no-op, always
    # returns muting:false.
    actor = await _resolve_account_or_404(db_session, account_id)
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
    return JSONResponse(
        content=await _relationship_for_actor(db_session, account_id, actor),
        status_code=200,
    )


@router.post("/api/v1/accounts/{account_id}/note", response_model=None)
async def accounts_note(
    account_id: str,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    token_info: AccessTokenInfo = Depends(require_scope("write:accounts")),
) -> JSONResponse:
    # Not persisted (no model for personal notes about an account) — echo
    # the submitted comment back for this response only, rather than
    # silently discarding it into an always-empty "note".
    actor = await _resolve_account_or_404(db_session, account_id)
    form = await request.form()
    comment = form.get("comment")

    relationship = await _relationship_for_actor(db_session, account_id, actor)
    relationship["note"] = str(comment) if comment else ""
    return JSONResponse(content=relationship, status_code=200)


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


async def _search_accounts(
    db_session: AsyncSession, query: str, limit: int
) -> list[dict]:
    # Same pragmatic approach as accounts/lookup (PR-1a): the single-user
    # instance's cached-actor table is small, so scan-then-filter in Python
    # rather than a DB-specific JSON query against ap_actor.
    query_lower = query.lstrip("@").lower()
    known_actors = (await db_session.scalars(select(activitypub.models.Actor))).all()
    matches = [
        actor
        for actor in known_actors
        if query_lower in actor.preferred_username.lower()
        or query_lower in actor.display_name.lower()
        or query_lower in actor.ap_id.lower()
    ]
    return [
        await serializers.serialize_account(db_session, actor)
        for actor in matches[:limit]
    ]


async def _search_statuses(
    db_session: AsyncSession, query: str, limit: int
) -> list[dict]:
    # Same bounded-scan-then-filter approach as timelines/tag (PR-1b) — there
    # is no full-text index (the outbox_fts table some code comments allude
    # to was never wired up: no migration creates it, nothing keeps it in
    # sync). Fine for a single-user instance's post volume.
    query_lower = query.lower()
    scan_limit = max(limit * 5, 100)
    inbox_items = await _fetch_inbox_timeline_page(
        db_session,
        before=None,
        after=None,
        limit=scan_limit,
        extra_where=(
            activitypub.models.InboxObject.visibility == ap.VisibilityEnum.PUBLIC,
        ),
    )
    outbox_items = await _fetch_outbox_timeline_page(
        db_session,
        before=None,
        after=None,
        limit=scan_limit,
        extra_where=(
            activitypub.models.OutboxObject.visibility == ap.VisibilityEnum.PUBLIC,
        ),
    )
    combined: list[AnyboxObject] = [*inbox_items, *outbox_items]
    matches = [
        obj for obj in combined if obj.content and query_lower in obj.content.lower()
    ]
    matches.sort(key=_status_id_int, reverse=True)
    return [
        await serializers.serialize_status(db_session, obj) for obj in matches[:limit]
    ]


async def _resolve_remote(db_session: AsyncSession, query: str):
    try:
        return await lookup(db_session, query)
    except Exception:
        # Network/parse failures just mean "nothing resolved" — search must
        # not 500 because a query isn't a fetchable handle/URL.
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
            # query looks like a taggable hashtag.
            hashtags = [
                {"name": tag, "url": f"{config.BASE_URL}/t/{tag}", "history": []}
            ]

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
