import re
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter
from fastapi import Cookie
from fastapi import Depends
from fastapi import Form
from fastapi import Request
from fastapi import UploadFile
from fastapi.exceptions import HTTPException
from fastapi.responses import RedirectResponse
from loguru import logger
from sqlalchemy import and_
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import joinedload

import activitypub.models
from activitypub import activitypub as ap
from activitypub import boxes
from activitypub.actor import LOCAL_ACTOR
from activitypub.actor import fetch_actor
from activitypub.actor import get_actors_metadata
from activitypub.actor import list_actors
from activitypub.actor import mute_actor
from activitypub.actor import unmute_actor
from activitypub.boxes import get_inbox_object_by_ap_id
from activitypub.boxes import get_outbox_object_by_ap_id
from activitypub.boxes import send_block
from activitypub.boxes import send_follow
from activitypub.boxes import send_unblock
from app import models
from app import templates
from app.config import EMOJIS
from app.config import SESSION_TIMEOUT
from app.config import generate_csrf_token
from app.config import session_serializer
from app.config import verify_csrf_token
from app.config import verify_password
from app.database import AsyncSession
from app.database import get_db_session
from app.i18n import gettext_default
from app.lookup import lookup
from app.templates import is_current_user_admin
from app.uploads import IncompatibleMediaError
from app.uploads import UploadTooLargeError
from app.uploads import delete_uploads
from app.uploads import save_upload
from app.utils import pagination
from app.utils.emoji import EMOJIS_BY_NAME
from app.utils.text import slugify
from app.utils.url import InvalidURLError


async def user_session_or_redirect(
    request: Request,
    session: str | None = Cookie(default=None),
) -> None:
    if request.method == "POST":
        form_data = await request.form()
        if "redirect_url" in form_data:
            redirect_url = str(form_data["redirect_url"])
        else:
            redirect_url = str(request.url_for("admin_stream"))
    else:
        redirect_url = str(request.url)

    _RedirectToLoginPage = HTTPException(
        status_code=302,
        headers={
            "Location": str(request.url_for("login"))
            + f"?redirect={quote(redirect_url)}"
        },
    )

    if not session:
        logger.info("No existing admin session")
        raise _RedirectToLoginPage

    try:
        loaded_session = session_serializer.loads(session, max_age=SESSION_TIMEOUT)
    except Exception:
        logger.exception("Failed to validate admin session")
        raise _RedirectToLoginPage

    if not loaded_session.get("is_logged_in"):
        logger.info(f"Admin session invalidated: {loaded_session}")
        raise _RedirectToLoginPage

    return None


router = APIRouter(
    dependencies=[Depends(user_session_or_redirect)],
)
unauthenticated_router = APIRouter()

_MAX_ALIAS_LENGTH = 200

# `_load_emojis()` populates these once at import time (app/config.py) and
# nothing mutates them afterwards, so the split/sort is done here rather than
# rebuilt on every compose/edit page render.
_EMOJI_PICKER_EMOJIS = EMOJIS.split(" ")
_EMOJI_PICKER_CUSTOM_EMOJIS = sorted(
    EMOJIS_BY_NAME.values(), key=lambda obj: obj["name"]
)


async def _normalize_alias(
    db_session: AsyncSession,
    raw: str | None,
    *,
    exclude_id: int | None = None,
) -> str | None:
    """Slugify a raw admin-submitted alias, or return None for an empty one.

    Raises a 422 for a value that slugifies to nothing, is too long, or
    clashes with another outbox object's alias (soft-deleted posts included --
    their alias stays reserved).
    """
    if not raw or not raw.strip():
        return None

    alias = slugify(raw)
    if not alias:
        raise HTTPException(
            status_code=422, detail=gettext_default("Error: invalid URL alias")
        )
    if len(alias) > _MAX_ALIAS_LENGTH:
        raise HTTPException(
            status_code=422, detail=gettext_default("Error: URL alias is too long")
        )

    # The UNIQUE constraint on `alias` isn't conditioned on `is_deleted`, so a
    # soft-deleted post still reserves its alias -- checked here too, so that
    # case surfaces as this friendly 422 rather than a raw IntegrityError.
    conflict_where = [
        activitypub.models.OutboxObject.alias == alias,
    ]
    if exclude_id is not None:
        conflict_where.append(activitypub.models.OutboxObject.id != exclude_id)

    existing = (
        await db_session.execute(
            select(activitypub.models.OutboxObject).where(*conflict_where)
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=422,
            detail=gettext_default("Error: URL alias %(alias)s is already used")
            % {"alias": alias},
        )

    return alias


@router.get("/lookup", response_model=None)
async def get_lookup(
    request: Request,
    query: str | None = None,
    db_session: AsyncSession = Depends(get_db_session),
) -> templates.TemplateResponse | RedirectResponse:
    error = None
    ap_object = None
    actors_metadata = {}
    actor_recommendations = await list_actors(db_session, 2500)
    if query:
        try:
            ap_object = await lookup(db_session, query)
        except httpx.TimeoutException:
            error = ap.FetchErrorTypeEnum.TIMEOUT
        except (ap.ObjectNotFoundError, ap.ObjectIsGoneError):
            error = ap.FetchErrorTypeEnum.NOT_FOUND
        except ap.ObjectUnavailableError:
            error = ap.FetchErrorTypeEnum.UNAUHTORIZED
        except (InvalidURLError, ap.NotAnObjectError):
            # Not a bug -- the query just isn't a URL or a resolvable
            # @user@domain handle, so it never reaches the network. No
            # `logger.exception` here, unlike the other branches: this is
            # expected user input, not a failure worth a traceback.
            error = ap.FetchErrorTypeEnum.INVALID_INPUT
        except Exception:
            logger.exception(f"Failed to lookup {query}")
            error = ap.FetchErrorTypeEnum.INTERNAL_ERROR
        else:
            if ap_object.ap_type in ap.ACTOR_TYPES:
                try:
                    await fetch_actor(
                        db_session, ap_object.ap_id, save_if_not_found=False
                    )
                except ap.ObjectNotFoundError:
                    pass
                else:
                    return RedirectResponse(
                        str(request.url_for("admin_profile"))
                        + f"?actor_id={ap_object.ap_id}",
                        status_code=302,
                    )

                actors_metadata = await get_actors_metadata(
                    db_session, [ap_object]  # type: ignore
                )
            else:
                # Check if the object is in the inbox
                requested_object = await boxes.get_anybox_object_by_ap_id(
                    db_session, ap_object.ap_id
                )
                if requested_object:
                    return RedirectResponse(
                        str(request.url_for("admin_object"))
                        + f"?ap_id={ap_object.ap_id}#"
                        + requested_object.permalink_id,
                        status_code=302,
                    )

                actors_metadata = await get_actors_metadata(
                    db_session, [ap_object.actor]  # type: ignore
                )

    return await templates.render_template(
        db_session,
        request,
        "lookup.html",
        {
            "query": query,
            "ap_object": ap_object,
            "actors_metadata": actors_metadata,
            "actor_recommendations": actor_recommendations,
            "error": error,
        },
    )


def _new_form_context(
    *,
    in_reply_to_object: "boxes.AnyboxObject | None",
    quoted_object: "boxes.AnyboxObject | None",
    in_reply_to: str | None,
    quote_of: str | None,
    content: str,
    content_warning: str | None,
    is_sensitive: bool = False,
    visibility: str | None,
    name: str | None = None,
    language: str | None = None,
    alias: str | None = None,
    ap_type: str | None = None,
    poll_type: str | None = None,
    poll_duration: str | None = None,
    poll_answers: list[str] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Build the template context shared by the compose form's GET and POST paths.

    Used both to render the blank form and, on a rejected submission, to
    re-render it with everything the user typed still in place.
    """
    return {
        "in_reply_to_object": in_reply_to_object,
        "quoted_object": quoted_object,
        "in_reply_to": in_reply_to,
        "quote_of": quote_of,
        "content": content,
        "content_warning": content_warning,
        "is_sensitive": is_sensitive,
        "visibility_choices": [
            (v.name, ap.VisibilityEnum.get_display_name(v)) for v in ap.VisibilityEnum
        ],
        "visibility": visibility,
        "name": name,
        "language": language,
        "alias": alias,
        "ap_type": ap_type,
        "poll_type": poll_type,
        "poll_duration": poll_duration,
        "poll_answers": poll_answers or [],
        "emojis": _EMOJI_PICKER_EMOJIS,
        "custom_emojis": _EMOJI_PICKER_CUSTOM_EMOJIS,
        "error": error,
    }


@router.get("/new", response_model=None)
async def admin_new(
    request: Request,
    query: str | None = None,
    in_reply_to: str | None = None,
    quote_of: str | None = None,
    with_content: str | None = None,
    with_visibility: str | None = None,
    db_session: AsyncSession = Depends(get_db_session),
) -> templates.TemplateResponse:
    content = ""
    content_warning = None
    quoted_object = None
    if quote_of:
        quoted_object = await boxes.get_anybox_object_by_ap_id(db_session, quote_of)
        if not quoted_object:
            logger.info(f"Saving unknwown object {quote_of}")
            raw_object = await ap.fetch(quote_of)
            await boxes.save_object_to_inbox(db_session, raw_object)
            await db_session.commit()
            quoted_object = await boxes.get_anybox_object_by_ap_id(db_session, quote_of)
        if not quoted_object:
            raise ValueError(f"Unknown object {quote_of=}")

    in_reply_to_object = None
    if in_reply_to:
        in_reply_to_object = await boxes.get_anybox_object_by_ap_id(
            db_session, in_reply_to
        )
        if not in_reply_to_object:
            logger.info(f"Saving unknwown object {in_reply_to}")
            raw_object = await ap.fetch(in_reply_to)
            await boxes.save_object_to_inbox(db_session, raw_object)
            await db_session.commit()
            in_reply_to_object = await boxes.get_anybox_object_by_ap_id(
                db_session, in_reply_to
            )

        # Add mentions to the initial note content
        if not in_reply_to_object:
            raise ValueError(f"Unknown object {in_reply_to=}")
        if in_reply_to_object.actor.ap_id != LOCAL_ACTOR.ap_id:
            content += f"{in_reply_to_object.actor.handle} "
        for tag in in_reply_to_object.tags:
            if tag.get("type") == "Mention" and tag["name"] != LOCAL_ACTOR.handle:
                try:
                    mentioned_actor = await fetch_actor(db_session, tag["href"])
                    content += f"{mentioned_actor.handle} "
                except Exception:
                    logger.exception(f"Failed to lookup {mentioned_actor}")

        # Copy the content warning if any
        if in_reply_to_object.summary:
            content_warning = in_reply_to_object.summary
    elif with_content:
        content += f"{with_content} "

    return await templates.render_template(
        db_session,
        request,
        "admin_new.html",
        _new_form_context(
            in_reply_to_object=in_reply_to_object,
            quoted_object=quoted_object,
            in_reply_to=in_reply_to,
            quote_of=quote_of,
            content=content,
            content_warning=content_warning,
            visibility=with_visibility,
        ),
    )


@router.get("/bookmarks", response_model=None)
async def admin_bookmarks(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> templates.TemplateResponse:
    # TODO: support pagination
    stream = (
        (
            await db_session.scalars(
                select(activitypub.models.InboxObject)
                .where(
                    activitypub.models.InboxObject.ap_type.in_(
                        ["Note", "Article", "Video", "Announce"]
                    ),
                    activitypub.models.InboxObject.is_bookmarked.is_(True),
                    activitypub.models.InboxObject.is_deleted.is_(False),
                )
                .options(
                    joinedload(activitypub.models.InboxObject.relates_to_inbox_object),
                    joinedload(
                        activitypub.models.InboxObject.relates_to_outbox_object
                    ).options(
                        joinedload(
                            activitypub.models.OutboxObject.outbox_object_attachments
                        ).options(
                            joinedload(activitypub.models.OutboxObjectAttachment.upload)
                        ),
                    ),
                    joinedload(activitypub.models.InboxObject.actor),
                )
                .order_by(activitypub.models.InboxObject.ap_published_at.desc())
                .limit(20)
            )
        )
        .unique()
        .all()
    )
    return await templates.render_template(
        db_session,
        request,
        "admin_stream.html",
        {
            "stream": stream,
        },
    )


@router.get("/blocks", response_model=None)
async def admin_blocks(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    cursor: str | None = None,
) -> templates.TemplateResponse:
    where = [
        activitypub.models.Actor.is_blocked.is_(True),
        activitypub.models.Actor.is_deleted.is_(False),
    ]
    if cursor:
        where.append(
            activitypub.models.Actor.created_at < pagination.decode_cursor(cursor)
        )

    page_size = 20
    blocks_count = await db_session.scalar(
        select(func.count(activitypub.models.Actor.id)).where(*where)
    )

    blocked_actors = (
        (
            await db_session.scalars(
                select(activitypub.models.Actor)
                .where(*where)
                .order_by(activitypub.models.Actor.created_at.desc())
                .limit(page_size)
            )
        )
        .unique()
        .all()
    )

    next_cursor = (
        pagination.encode_cursor(blocked_actors[-1].created_at)
        if blocked_actors and blocks_count > page_size
        else None
    )

    # display_actor() renders the unblock button off this metadata.
    actors_metadata = await get_actors_metadata(db_session, list(blocked_actors))

    return await templates.render_template(
        db_session,
        request,
        "admin_blocks.html",
        {
            "actors_metadata": actors_metadata,
            "blocked_actors": blocked_actors,
            "next_cursor": next_cursor,
        },
    )


@router.get("/mutes", response_model=None)
async def admin_mutes(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    cursor: str | None = None,
) -> templates.TemplateResponse:
    where = [
        activitypub.models.Actor.id.in_(activitypub.models.muted_actor_ids()),
        activitypub.models.Actor.is_deleted.is_(False),
    ]
    if cursor:
        where.append(
            activitypub.models.Actor.created_at < pagination.decode_cursor(cursor)
        )

    page_size = 20
    mutes_count = await db_session.scalar(
        select(func.count(activitypub.models.Actor.id)).where(*where)
    )

    muted_actors = (
        (
            await db_session.scalars(
                select(activitypub.models.Actor)
                .where(*where)
                .order_by(activitypub.models.Actor.created_at.desc())
                .limit(page_size)
            )
        )
        .unique()
        .all()
    )

    next_cursor = (
        pagination.encode_cursor(muted_actors[-1].created_at)
        if muted_actors and mutes_count > page_size
        else None
    )

    # display_actor() renders the unmute button off this metadata.
    actors_metadata = await get_actors_metadata(db_session, list(muted_actors))

    return await templates.render_template(
        db_session,
        request,
        "admin_mutes.html",
        {
            "actors_metadata": actors_metadata,
            "muted_actors": muted_actors,
            "next_cursor": next_cursor,
        },
    )


@router.get("/stream", response_model=None)
async def admin_stream(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    cursor: str | None = None,
) -> templates.TemplateResponse:
    where = [
        activitypub.models.InboxObject.is_hidden_from_stream.is_(False),
        activitypub.models.InboxObject.is_deleted.is_(False),
        # Keeping muted actors out of the stream is the whole point of a
        # mute; they stay reachable from their profile and the Mutes page.
        *activitypub.models.not_from_muted_actors(),
        *activitypub.models.not_hidden_announces(),
    ]
    if cursor:
        where.append(
            activitypub.models.InboxObject.ap_published_at
            < pagination.decode_cursor(cursor)
        )

    page_size = 20
    remaining_count = await db_session.scalar(
        select(func.count(activitypub.models.InboxObject.id)).where(*where)
    )
    q = select(activitypub.models.InboxObject).where(*where)

    inbox = (
        (
            await db_session.scalars(
                q.options(
                    joinedload(
                        activitypub.models.InboxObject.relates_to_inbox_object
                    ).options(joinedload(activitypub.models.InboxObject.actor)),
                    joinedload(
                        activitypub.models.InboxObject.relates_to_outbox_object
                    ).options(
                        joinedload(
                            activitypub.models.OutboxObject.outbox_object_attachments
                        ).options(
                            joinedload(activitypub.models.OutboxObjectAttachment.upload)
                        ),
                    ),
                    joinedload(activitypub.models.InboxObject.actor),
                )
                .order_by(activitypub.models.InboxObject.ap_published_at.desc())
                .limit(20)
            )
        )
        .unique()
        .all()
    )

    next_cursor = (
        pagination.encode_cursor(inbox[-1].ap_published_at)
        if inbox and remaining_count > page_size
        else None
    )

    actors_metadata = await get_actors_metadata(
        db_session,
        [
            inbox_object.actor
            for inbox_object in inbox
            if inbox_object.ap_type == "Follow"
        ],
    )

    return await templates.render_template(
        db_session,
        request,
        "admin_inbox.html",
        {
            "inbox": inbox,
            "actors_metadata": actors_metadata,
            "next_cursor": next_cursor,
            "show_filters": False,
        },
    )


@router.get("/inbox", response_model=None)
async def admin_inbox(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    filter_by: str | None = None,
    cursor: str | None = None,
) -> templates.TemplateResponse:
    where = [
        activitypub.models.InboxObject.ap_type.not_in(
            [
                "Accept",
                "Delete",
                "Create",
                "Update",
                "Undo",
                "Read",
                "Reject",
                "Add",
                "Remove",
                "EmojiReact",
            ]
        ),
        activitypub.models.InboxObject.is_deleted.is_(False),
        activitypub.models.InboxObject.is_transient.is_(False),
    ]
    if filter_by:
        where.append(activitypub.models.InboxObject.ap_type == filter_by)
    if cursor:
        where.append(
            activitypub.models.InboxObject.ap_published_at
            < pagination.decode_cursor(cursor)
        )

    page_size = 20
    remaining_count = await db_session.scalar(
        select(func.count(activitypub.models.InboxObject.id)).where(*where)
    )
    q = select(activitypub.models.InboxObject).where(*where)

    inbox = (
        (
            await db_session.scalars(
                q.options(
                    joinedload(
                        activitypub.models.InboxObject.relates_to_inbox_object
                    ).options(joinedload(activitypub.models.InboxObject.actor)),
                    joinedload(
                        activitypub.models.InboxObject.relates_to_outbox_object
                    ).options(
                        joinedload(
                            activitypub.models.OutboxObject.outbox_object_attachments
                        ).options(
                            joinedload(activitypub.models.OutboxObjectAttachment.upload)
                        ),
                    ),
                    joinedload(activitypub.models.InboxObject.actor),
                )
                .order_by(activitypub.models.InboxObject.ap_published_at.desc())
                .limit(20)
            )
        )
        .unique()
        .all()
    )

    next_cursor = (
        pagination.encode_cursor(inbox[-1].ap_published_at)
        if inbox and remaining_count > page_size
        else None
    )

    actors_metadata = await get_actors_metadata(
        db_session,
        [
            inbox_object.actor
            for inbox_object in inbox
            if inbox_object.ap_type == "Follow"
        ],
    )

    return await templates.render_template(
        db_session,
        request,
        "admin_inbox.html",
        {
            "inbox": inbox,
            "actors_metadata": actors_metadata,
            "next_cursor": next_cursor,
            "show_filters": True,
        },
    )


@router.get("/direct_messages", response_model=None)
async def admin_direct_messages(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    cursor: str | None = None,
) -> templates.TemplateResponse:
    # The process for building DMs thread is a bit compex in term of query
    # but it does not require an extra tables to index/manage threads

    inbox_convos = (
        (
            await db_session.execute(
                select(
                    activitypub.models.InboxObject.ap_context,
                    activitypub.models.InboxObject.actor_id,
                    func.count(1).label("count"),
                    func.max(activitypub.models.InboxObject.ap_published_at).label(
                        "most_recent_date"
                    ),
                )
                .where(
                    activitypub.models.InboxObject.visibility
                    == ap.VisibilityEnum.DIRECT,
                    activitypub.models.InboxObject.ap_context.is_not(None),
                    # Skip transient object like poll relies
                    activitypub.models.InboxObject.is_transient.is_(False),
                    activitypub.models.InboxObject.is_deleted.is_(False),
                )
                .group_by(
                    activitypub.models.InboxObject.ap_context,
                    activitypub.models.InboxObject.actor_id,
                )
            )
        )
        .unique()
        .all()
    )
    outbox_convos = (
        (
            await db_session.execute(
                select(
                    activitypub.models.OutboxObject.ap_context,
                    func.count(1).label("count"),
                    func.max(activitypub.models.OutboxObject.ap_published_at).label(
                        "most_recent_date"
                    ),
                )
                .where(
                    activitypub.models.OutboxObject.visibility
                    == ap.VisibilityEnum.DIRECT,
                    activitypub.models.OutboxObject.ap_context.is_not(None),
                    # Skip transient object like poll relies
                    activitypub.models.OutboxObject.is_transient.is_(False),
                    activitypub.models.OutboxObject.is_deleted.is_(False),
                )
                .group_by(activitypub.models.OutboxObject.ap_context)
            )
        )
        .unique()
        .all()
    )

    # Build a "threads index" by combining objects from the inbox and outbox
    convos = {}
    for inbox_convo in inbox_convos:
        if inbox_convo.ap_context not in convos:
            convos[inbox_convo.ap_context] = {
                "actor_ids": {inbox_convo.actor_id},
                "count": inbox_convo.count,
                "most_recent_from_inbox": inbox_convo.most_recent_date,
                "most_recent_from_outbox": datetime.min,
            }
        else:
            convos[inbox_convo.ap_context]["actor_ids"].add(inbox_convo.actor_id)
            convos[inbox_convo.ap_context]["count"] += inbox_convo.count
            convos[inbox_convo.ap_context]["most_recent_from_inbox"] = max(
                inbox_convo.most_recent_date,
                convos[inbox_convo.ap_context]["most_recent_from_inbox"],
            )

    for outbox_convo in outbox_convos:
        if outbox_convo.ap_context not in convos:
            convos[outbox_convo.ap_context] = {
                "actor_ids": set(),
                "count": outbox_convo.count,
                "most_recent_from_inbox": datetime.min,
                "most_recent_from_outbox": outbox_convo.most_recent_date,
            }
        else:
            convos[outbox_convo.ap_context]["count"] += outbox_convo.count
            convos[outbox_convo.ap_context]["most_recent_from_outbox"] = max(
                outbox_convo.most_recent_date,
                convos[outbox_convo.ap_context]["most_recent_from_outbox"],
            )

    # Fetch the latest object for each threads
    convos_with_last_from_inbox = []
    convos_with_last_from_outbox = []
    for context, convo in convos.items():
        if convo["most_recent_from_inbox"] > convo["most_recent_from_outbox"]:
            convos_with_last_from_inbox.append(
                and_(
                    activitypub.models.InboxObject.ap_context == context,
                    activitypub.models.InboxObject.ap_published_at
                    == convo["most_recent_from_inbox"],
                )
            )
        else:
            convos_with_last_from_outbox.append(
                and_(
                    activitypub.models.OutboxObject.ap_context == context,
                    activitypub.models.OutboxObject.ap_published_at
                    == convo["most_recent_from_outbox"],
                )
            )
    last_from_inbox = (
        (
            (
                await db_session.scalars(
                    select(activitypub.models.InboxObject)
                    .where(or_(*convos_with_last_from_inbox))
                    .options(
                        joinedload(activitypub.models.InboxObject.actor),
                    )
                )
            )
            .unique()
            .all()
        )
        if convos_with_last_from_inbox
        else []
    )
    last_from_outbox = (
        (
            (
                await db_session.scalars(
                    select(activitypub.models.OutboxObject)
                    .where(or_(*convos_with_last_from_outbox))
                    .options(
                        joinedload(
                            activitypub.models.OutboxObject.outbox_object_attachments
                        ).options(
                            joinedload(activitypub.models.OutboxObjectAttachment.upload)
                        ),
                    )
                )
            )
            .unique()
            .all()
        )
        if convos_with_last_from_outbox
        else []
    )

    # Build the template response
    threads = []
    for anybox_object in sorted(
        last_from_inbox + last_from_outbox,
        key=lambda x: x.ap_published_at,
        reverse=True,
    ):
        convo = convos[anybox_object.ap_context]
        actors = list(
            (
                await db_session.execute(
                    select(activitypub.models.Actor).where(
                        activitypub.models.Actor.id.in_(convo["actor_ids"])
                    )
                )
            ).scalars()
        )
        # If this message from outbox starts a thread with no replies, look
        # at the mentions
        if not actors and anybox_object.is_from_outbox:
            actors = (  # type: ignore
                await db_session.execute(
                    select(activitypub.models.Actor).where(
                        activitypub.models.Actor.ap_id.in_(
                            mention["href"]
                            for mention in anybox_object.tags
                            if mention["type"] == "Mention"
                        )
                    )
                )
            ).scalars()
        threads.append((anybox_object, convo, actors))

    return await templates.render_template(
        db_session,
        request,
        "admin_direct_messages.html",
        {
            "threads": threads,
        },
    )


@router.get("/outbox", response_model=None)
async def admin_outbox(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    filter_by: str | None = None,
    cursor: str | None = None,
) -> templates.TemplateResponse:
    where = [
        activitypub.models.OutboxObject.ap_type.not_in(["Accept", "Delete", "Update"]),
        activitypub.models.OutboxObject.is_deleted.is_(False),
        activitypub.models.OutboxObject.is_transient.is_(False),
    ]
    if filter_by:
        where.append(activitypub.models.OutboxObject.ap_type == filter_by)
    if cursor:
        where.append(
            activitypub.models.OutboxObject.ap_published_at
            < pagination.decode_cursor(cursor)
        )

    page_size = 20
    remaining_count = await db_session.scalar(
        select(func.count(activitypub.models.OutboxObject.id)).where(*where)
    )
    q = select(activitypub.models.OutboxObject).where(*where)

    outbox = (
        (
            await db_session.scalars(
                q.options(
                    joinedload(
                        activitypub.models.OutboxObject.relates_to_inbox_object
                    ).options(
                        joinedload(activitypub.models.InboxObject.actor),
                    ),
                    joinedload(
                        activitypub.models.OutboxObject.relates_to_outbox_object
                    ),
                    joinedload(activitypub.models.OutboxObject.relates_to_actor),
                    joinedload(
                        activitypub.models.OutboxObject.outbox_object_attachments
                    ).options(
                        joinedload(activitypub.models.OutboxObjectAttachment.upload)
                    ),
                )
                .order_by(activitypub.models.OutboxObject.ap_published_at.desc())
                .limit(page_size)
            )
        )
        .unique()
        .all()
    )

    next_cursor = (
        pagination.encode_cursor(outbox[-1].ap_published_at)
        if outbox and remaining_count > page_size
        else None
    )

    actors_metadata = await get_actors_metadata(
        db_session,
        [
            outbox_object.relates_to_actor
            for outbox_object in outbox
            if outbox_object.relates_to_actor
        ],
    )

    return await templates.render_template(
        db_session,
        request,
        "admin_outbox.html",
        {
            "actors_metadata": actors_metadata,
            "outbox": outbox,
            "next_cursor": next_cursor,
        },
    )


@router.get("/notifications", response_model=None)
async def get_notifications(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    cursor: str | None = None,
) -> templates.TemplateResponse:
    where = [
        models.notification_not_muted(),
        models.notification_not_in_muted_conversation(),
    ]
    if cursor:
        decoded_cursor = pagination.decode_cursor(cursor)
        where.append(models.Notification.created_at < decoded_cursor)

    page_size = 20
    remaining_count = await db_session.scalar(
        select(func.count(models.Notification.id)).where(*where)
    )

    notifications = (
        (
            await db_session.scalars(
                select(models.Notification)
                .where(*where)
                .options(
                    joinedload(models.Notification.actor),
                    joinedload(models.Notification.inbox_object).options(
                        joinedload(activitypub.models.InboxObject.actor)
                    ),
                    joinedload(models.Notification.outbox_object).options(
                        joinedload(
                            activitypub.models.OutboxObject.outbox_object_attachments
                        ).options(
                            joinedload(activitypub.models.OutboxObjectAttachment.upload)
                        ),
                    ),
                    joinedload(models.Notification.webmention),
                )
                .order_by(models.Notification.created_at.desc())
                .limit(page_size)
            )
        )
        .unique()
        .all()
    )
    actors_metadata = await get_actors_metadata(
        db_session, [notif.actor for notif in notifications if notif.actor]
    )
    more_unread_count = 0
    next_cursor = None

    if notifications and remaining_count > page_size:
        decoded_next_cursor = notifications[-1].created_at
        next_cursor = pagination.encode_cursor(decoded_next_cursor)

        # If on the "see more" page there's more unread notification, we want
        # to display it next to the link
        more_unread_count = await db_session.scalar(
            select(func.count(models.Notification.id)).where(
                models.Notification.is_new.is_(True),
                models.Notification.created_at < decoded_next_cursor,
                models.notification_not_muted(),
                models.notification_not_in_muted_conversation(),
            )
        )

    # Render the template before we change the new flag on notifications
    tpl_resp = await templates.render_template(
        db_session,
        request,
        "notifications.html",
        {
            "notifications": notifications,
            "actors_metadata": actors_metadata,
            "next_cursor": next_cursor,
            "more_unread_count": more_unread_count,
        },
    )

    if len({notif.id for notif in notifications if notif.is_new}):
        for notif in notifications:
            notif.is_new = False
        await db_session.commit()

    return tpl_resp


@router.get("/object", response_model=None)
async def admin_object(
    request: Request,
    ap_id: str,
    db_session: AsyncSession = Depends(get_db_session),
) -> templates.TemplateResponse:
    requested_object = await boxes.get_anybox_object_by_ap_id(db_session, ap_id)
    if not requested_object or requested_object.is_deleted:
        raise HTTPException(status_code=404)

    replies_tree = await boxes.get_replies_tree(
        db_session,
        requested_object,
        is_current_user_admin=True,
    )
    quoted_object = await boxes.get_quoted_object_for_display(
        db_session, requested_object
    )

    return await templates.render_template(
        db_session,
        request,
        "object.html",
        {"replies_tree": replies_tree, "quoted_object": quoted_object},
    )


@router.get("/profile", response_model=None)
async def admin_profile(
    request: Request,
    actor_id: str,
    cursor: str | None = None,
    db_session: AsyncSession = Depends(get_db_session),
) -> templates.TemplateResponse:
    # TODO: show featured/pinned
    actor = (
        await db_session.execute(
            select(activitypub.models.Actor).where(
                activitypub.models.Actor.ap_id == actor_id
            )
        )
    ).scalar_one_or_none()
    if not actor:
        raise HTTPException(status_code=404)

    actors_metadata = await get_actors_metadata(db_session, [actor])

    where = [
        activitypub.models.InboxObject.is_deleted.is_(False),
        activitypub.models.InboxObject.actor_id == actor.id,
        activitypub.models.InboxObject.ap_type.in_(
            ["Note", "Article", "Video", "Page", "Announce"]
        ),
    ]
    if cursor:
        decoded_cursor = pagination.decode_cursor(cursor)
        where.append(activitypub.models.InboxObject.ap_published_at < decoded_cursor)

    page_size = 20
    remaining_count = await db_session.scalar(
        select(func.count(activitypub.models.InboxObject.id)).where(*where)
    )

    inbox_objects = (
        (
            await db_session.scalars(
                select(activitypub.models.InboxObject)
                .where(*where)
                .options(
                    joinedload(
                        activitypub.models.InboxObject.relates_to_inbox_object
                    ).options(joinedload(activitypub.models.InboxObject.actor)),
                    joinedload(
                        activitypub.models.InboxObject.relates_to_outbox_object
                    ).options(
                        joinedload(
                            activitypub.models.OutboxObject.outbox_object_attachments
                        ).options(
                            joinedload(activitypub.models.OutboxObjectAttachment.upload)
                        ),
                    ),
                    joinedload(activitypub.models.InboxObject.actor),
                )
                .order_by(activitypub.models.InboxObject.ap_published_at.desc())
                .limit(page_size)
            )
        )
        .unique()
        .all()
    )

    next_cursor = (
        pagination.encode_cursor(inbox_objects[-1].created_at)
        if inbox_objects and remaining_count > page_size
        else None
    )

    return await templates.render_template(
        db_session,
        request,
        "admin_profile.html",
        {
            "actors_metadata": actors_metadata,
            "actor": actor,
            "inbox_objects": inbox_objects,
            "next_cursor": next_cursor,
        },
    )


@router.post("/actions/force_delete", response_model=None)
async def admin_actions_force_delete(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    ap_object_to_delete = await get_inbox_object_by_ap_id(db_session, ap_object_id)
    if not ap_object_to_delete:
        raise ValueError(f"Cannot find {ap_object_id}")

    logger.info(f"Deleting {ap_object_to_delete.ap_type}/{ap_object_to_delete.ap_id}")
    await boxes._revert_side_effect_for_deleted_object(
        db_session,
        None,
        ap_object_to_delete,
        None,
    )
    ap_object_to_delete.is_deleted = True
    await db_session.commit()
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/force_delete_webmention", response_model=None)
async def admin_actions_force_delete_webmention(
    request: Request,
    webmention_id: int = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    webmention = await boxes.get_webmention_by_id(db_session, webmention_id)
    if not webmention:
        raise ValueError(f"Cannot find {webmention_id}")
    if not webmention.outbox_object:
        raise ValueError(f"Missing related outbox object for {webmention_id}")

    # TODO: move this
    logger.info(f"Deleting {webmention_id}")
    webmention.is_deleted = True
    await db_session.flush()
    from app.webmentions import _handle_webmention_side_effects

    await _handle_webmention_side_effects(
        db_session, webmention, webmention.outbox_object
    )
    # Delete related notifications
    notif_deletion_result = await db_session.execute(
        delete(models.Notification)
        .where(models.Notification.webmention_id == webmention.id)
        .execution_options(synchronize_session=False)
    )
    logger.info(
        f"Deleted {notif_deletion_result.rowcount} notifications"  # type: ignore
    )
    await db_session.commit()
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/follow", response_model=None)
async def admin_actions_follow(
    request: Request,
    ap_actor_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    logger.info(f"Following {ap_actor_id}")
    await send_follow(db_session, ap_actor_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/block", response_model=None)
async def admin_actions_block(
    request: Request,
    ap_actor_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    await send_block(db_session, ap_actor_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/unblock", response_model=None)
async def admin_actions_unblock(
    request: Request,
    ap_actor_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    logger.info(f"Unblocking {ap_actor_id}")
    await send_unblock(db_session, ap_actor_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/mute", response_model=None)
async def admin_actions_mute(
    request: Request,
    ap_actor_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    logger.info(f"Muting {ap_actor_id}")
    actor = await fetch_actor(db_session, ap_actor_id)
    # Indefinite, notifications included — the same defaults a Mastodon
    # client gets. Timed mutes are only settable through the API.
    await mute_actor(db_session, actor)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/unmute", response_model=None)
async def admin_actions_unmute(
    request: Request,
    ap_actor_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    logger.info(f"Unmuting {ap_actor_id}")
    actor = await fetch_actor(db_session, ap_actor_id)
    await unmute_actor(db_session, actor)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/hide_announces", response_model=None)
async def admin_actions_hide_announces(
    request: Request,
    ap_actor_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    actor = await fetch_actor(db_session, ap_actor_id)
    actor.are_announces_hidden_from_stream = True
    await db_session.commit()
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/show_announces", response_model=None)
async def admin_actions_show_announces(
    request: Request,
    ap_actor_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    actor = await fetch_actor(db_session, ap_actor_id)
    actor.are_announces_hidden_from_stream = False
    await db_session.commit()
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/notify_on", response_model=None)
async def admin_actions_notify_on(
    request: Request,
    ap_actor_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    actor = await fetch_actor(db_session, ap_actor_id)
    actor.are_new_posts_notified = True
    await db_session.commit()
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/notify_off", response_model=None)
async def admin_actions_notify_off(
    request: Request,
    ap_actor_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    actor = await fetch_actor(db_session, ap_actor_id)
    actor.are_new_posts_notified = False
    await db_session.commit()
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/delete", response_model=None)
async def admin_actions_delete(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    await boxes.send_delete(db_session, ap_object_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/accept_incoming_follow", response_model=None)
async def admin_actions_accept_incoming_follow(
    request: Request,
    notification_id: int = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    await boxes.send_accept(db_session, notification_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/reject_incoming_follow", response_model=None)
async def admin_actions_reject_incoming_follow(
    request: Request,
    notification_id: int = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    await boxes.send_reject(db_session, notification_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/accept_incoming_quote_request", response_model=None)
async def admin_actions_accept_incoming_quote_request(
    request: Request,
    notification_id: int = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    await boxes.send_quote_accept(db_session, notification_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/reject_incoming_quote_request", response_model=None)
async def admin_actions_reject_incoming_quote_request(
    request: Request,
    notification_id: int = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    await boxes.send_quote_reject(db_session, notification_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/revoke_quote", response_model=None)
async def admin_actions_revoke_quote(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    await boxes.send_quote_revoke(db_session, ap_object_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/like", response_model=None)
async def admin_actions_like(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    await boxes.send_like(db_session, ap_object_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/undo", response_model=None)
async def admin_actions_undo(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    await boxes.send_undo(db_session, ap_object_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/announce", response_model=None)
async def admin_actions_announce(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    await boxes.send_announce(db_session, ap_object_id)
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/bookmark", response_model=None)
async def admin_actions_bookmark(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    inbox_object = await get_inbox_object_by_ap_id(db_session, ap_object_id)
    if not inbox_object:
        logger.info(f"Saving unknwown object {ap_object_id}")
        raw_object = await ap.fetch(ap_object_id)
        inbox_object = await boxes.save_object_to_inbox(db_session, raw_object)
    inbox_object.is_bookmarked = True
    await db_session.commit()
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/unbookmark", response_model=None)
async def admin_actions_unbookmark(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    inbox_object = await get_inbox_object_by_ap_id(db_session, ap_object_id)
    if not inbox_object:
        raise ValueError("Should never happen")
    inbox_object.is_bookmarked = False
    await db_session.commit()
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/fetch_replies", response_model=None)
async def admin_actions_fetch_replies(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    requested_object = await boxes.get_anybox_object_by_ap_id(db_session, ap_object_id)
    if requested_object:
        await boxes.fetch_replies(db_session, requested_object)
        await db_session.commit()
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/pin", response_model=None)
async def admin_actions_pin(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    outbox_object = await get_outbox_object_by_ap_id(db_session, ap_object_id)
    if not outbox_object:
        raise ValueError("Should never happen")
    if not outbox_object.is_pinned:
        pinned_count = await db_session.scalar(
            select(func.count(activitypub.models.OutboxObject.id)).where(
                activitypub.models.OutboxObject.is_pinned.is_(True)
            )
        )
        if pinned_count >= activitypub.models.MAX_PINNED_OBJECTS:
            raise HTTPException(
                status_code=400, detail="Maximum number of pinned posts reached"
            )
    outbox_object.is_pinned = True
    await db_session.commit()
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/actions/unpin", response_model=None)
async def admin_actions_unpin(
    request: Request,
    ap_object_id: str = Form(),
    redirect_url: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    outbox_object = await get_outbox_object_by_ap_id(db_session, ap_object_id)
    if not outbox_object:
        raise ValueError("Should never happen")
    outbox_object.is_pinned = False
    await db_session.commit()
    return RedirectResponse(redirect_url, status_code=302)


# Loose BCP 47 language tag check (e.g. "en", "pt-BR", "zh-Hant"); enough to
# keep malformed values out of the ActivityPub language maps.
_LANGUAGE_CODE_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


@router.post("/actions/new", response_model=None)
async def admin_actions_new(
    request: Request,
    files: list[UploadFile] = [],
    content: str | None = Form(None),
    in_reply_to: str | None = Form(None),
    quote_of: str | None = Form(None),
    content_warning: str | None = Form(None),
    is_sensitive: bool = Form(False),
    visibility: str = Form(),
    post_type: str = Form("Note", alias="type"),
    poll_type: str | None = Form(None),
    poll_duration: str | None = Form(None),
    poll_answer_1: str | None = Form(None),
    poll_answer_2: str | None = Form(None),
    poll_answer_3: str | None = Form(None),
    poll_answer_4: str | None = Form(None),
    name: str | None = Form(None),
    language: str | None = Form(None),
    alias: str | None = Form(None),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> templates.TemplateResponse | RedirectResponse:
    poll_answers_submitted = [
        answer
        for answer in [poll_answer_1, poll_answer_2, poll_answer_3, poll_answer_4]
        if answer
    ]

    # Snapshot what was actually typed: the Mastodon-style CW/content swap
    # below rewrites both, and a form re-rendered after that point must show
    # the user their own input, not the swapped version.
    submitted_content = content
    submitted_content_warning = content_warning
    submitted_is_sensitive = is_sensitive

    # Uploads created by this request (as opposed to dedup-reused existing
    # ones) -- rolled back below if the request ends up rejected.
    created_uploads: list[activitypub.models.Upload] = []

    async def _rerender(error: str) -> templates.TemplateResponse:
        # `send_create` only flushes its new outbox object (boxes.py:152) and
        # does not commit until much later, after recipients/webmentions are
        # computed -- without this rollback, deleting the uploads below could
        # commit a half-built post (attachments, but no recipients/delivery).
        # Both calls no-op for rejections that happen before any of that.
        await db_session.rollback()
        await delete_uploads(db_session, created_uploads)

        # The reply-to/quoted previews are resolved here rather than up front:
        # `get_anybox_object_by_ap_id` is a multi-joinedload query, and the
        # happy path never renders them, so eager-loading would tax every
        # successful reply/quote to serve the rare rejected one.
        in_reply_to_object = None
        if in_reply_to:
            in_reply_to_object = await boxes.get_anybox_object_by_ap_id(
                db_session, in_reply_to
            )
        quoted_object = None
        if quote_of:
            quoted_object = await boxes.get_anybox_object_by_ap_id(db_session, quote_of)

        return await templates.render_template(
            db_session,
            request,
            "admin_new.html",
            _new_form_context(
                in_reply_to_object=in_reply_to_object,
                quoted_object=quoted_object,
                in_reply_to=in_reply_to,
                quote_of=quote_of,
                content=submitted_content or "",
                content_warning=submitted_content_warning,
                is_sensitive=submitted_is_sensitive,
                visibility=visibility,
                name=name,
                language=language,
                alias=alias,
                ap_type=post_type,
                poll_type=poll_type,
                poll_duration=poll_duration,
                poll_answers=poll_answers_submitted,
                error=error,
            ),
            status_code=422,
        )

    if not content and not content_warning:
        return await _rerender(gettext_default("Error: object must have a content"))

    try:
        new_alias = await _normalize_alias(db_session, alias)
    except HTTPException as exc:
        return await _rerender(str(exc.detail))

    # Optional Mastodon-style post language (BCP 47); empty means unset (None).
    language = (language or "").strip() or None
    if language and not _LANGUAGE_CODE_RE.match(language):
        return await _rerender(gettext_default("Error: invalid language code"))

    # Do like Mastodon, if there's only a CW with no content and some attachments,
    # swap the CW and the content
    if not content and content_warning and len(files) >= 1:
        content = content_warning
        is_sensitive = True
        content_warning = None

    if not content:
        return await _rerender(gettext_default("Error: object must have a content"))

    if post_type == "Article" and not name:
        return await _rerender(gettext_default("Error: an article must have a title"))

    if visibility not in ap.VisibilityEnum.__members__:
        return await _rerender(gettext_default("Error: invalid visibility"))
    ap_visibility = ap.VisibilityEnum[visibility]

    # Same local lookup send_create does (boxes.py:849), run before anything
    # is uploaded so an unresolvable parent is the form's own 422 instead of
    # a ValueError 500 with the upload batch already committed.
    if in_reply_to and not await boxes.get_anybox_object_by_ap_id(
        db_session, in_reply_to
    ):
        return await _rerender(
            gettext_default("Error: unable to find the object being replied to")
        )

    ap_type = "Note"

    poll_duration_in_minutes = None
    poll_answers = None
    if poll_type:
        ap_type = "Question"
        poll_answers = poll_answers_submitted

        if not poll_answers or len(poll_answers) < 2:
            return await _rerender(
                gettext_default("Error: a poll must have at least 2 answers")
            )

        try:
            poll_duration_in_minutes = int(poll_duration or "1440")
        except ValueError:
            return await _rerender(gettext_default("Error: invalid poll duration"))
    elif name:
        ap_type = "Article"

    # XXX: for some reason, no files restuls in an empty single file
    uploads = []
    if len(files) >= 1:
        raw_form_data = await request.form()
        # `alt_<n>` is numbered by new.js over the files the *browser* holds, so
        # count only the entries that carry a filename -- an empty part (see the
        # XXX above) must not consume an index and shift every alt text by one.
        alt_index = 0
        for f in files:
            if f.filename is not None and f.filename != "":
                try:
                    upload = await save_upload(db_session, f, created=created_uploads)
                except UploadTooLargeError as exc:
                    return await _rerender(
                        f"{gettext_default('Error: file is too large')} "
                        f"({exc.limit} bytes max) -- "
                        f"{gettext_default('files must be re-selected before trying again')}"
                    )
                except IncompatibleMediaError as exc:
                    return await _rerender(
                        f"{gettext_default('Error: unable to process upload')}: "
                        f"{exc.reason} -- "
                        f"{gettext_default('files must be re-selected before trying again')}"
                    )
                if upload is not None:
                    alt = raw_form_data.get(f"alt_{alt_index}")
                    alt_index += 1
                    uploads.append(
                        (
                            upload,
                            f.filename,
                            str(alt) if alt is not None else None,
                        )
                    )
                else:
                    return await _rerender(
                        gettext_default("Error: Unable to process upload")
                    )

    try:
        public_id, _ = await boxes.send_create(
            db_session,
            ap_type=ap_type,
            source=content,
            uploads=uploads,
            in_reply_to=in_reply_to or None,
            visibility=ap_visibility,
            content_warning=content_warning or None,
            is_sensitive=True if content_warning else is_sensitive,
            poll_type=poll_type,
            poll_answers=poll_answers,
            poll_duration_in_minutes=poll_duration_in_minutes,
            name=name,
            language=language,
            quote_of=quote_of or None,
            alias=new_alias,
        )
    except ValueError:
        logger.exception("Failed to create post")
        return await _rerender(gettext_default("Error: unable to create the post"))

    return RedirectResponse(
        request.url_for("outbox_by_public_id", public_id=public_id),
        status_code=302,
    )


async def _get_editable_outbox_object(
    db_session: AsyncSession, public_id: str
) -> activitypub.models.OutboxObject:
    maybe_object = (
        (
            await db_session.execute(
                select(activitypub.models.OutboxObject).where(
                    activitypub.models.OutboxObject.public_id == public_id,
                    activitypub.models.OutboxObject.is_deleted.is_(False),
                )
            )
        )
        .unique()
        .scalar_one_or_none()
    )
    if not maybe_object:
        raise HTTPException(status_code=404)
    return maybe_object


@router.get("/edit_text/{public_id}", response_model=None)
async def admin_edit_text(
    request: Request,
    public_id: str,
    db_session: AsyncSession = Depends(get_db_session),
) -> templates.TemplateResponse | RedirectResponse:
    maybe_object = await _get_editable_outbox_object(db_session, public_id)

    return await templates.render_template(
        db_session,
        request,
        "admin_edit_text.html",
        {
            "public_id": public_id,
            "content": maybe_object.source,
            "content_warning": maybe_object.summary,
            "is_sensitive": maybe_object.sensitive,
            "outbox_object": maybe_object,
            "emojis": _EMOJI_PICKER_EMOJIS,
            "custom_emojis": _EMOJI_PICKER_CUSTOM_EMOJIS,
        },
    )


@router.post("/actions/edit_text/{public_id}", response_model=None)
async def admin_actions_edit_text(
    request: Request,
    public_id: str,
    content: str | None = Form(None),
    name: str | None = Form(None),
    alias: str | None = Form(None),
    content_warning: str | None = Form(None),
    is_sensitive: bool = Form(False),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> templates.TemplateResponse | RedirectResponse:
    maybe_object = await _get_editable_outbox_object(db_session, public_id)

    async def _rerender(error: str) -> templates.TemplateResponse:
        return await templates.render_template(
            db_session,
            request,
            "admin_edit_text.html",
            {
                "public_id": public_id,
                "content": content,
                "content_warning": content_warning,
                "is_sensitive": is_sensitive,
                "outbox_object": maybe_object,
                "emojis": _EMOJI_PICKER_EMOJIS,
                "custom_emojis": _EMOJI_PICKER_CUSTOM_EMOJIS,
                "error": error,
            },
            status_code=422,
        )

    if not content:
        return await _rerender(gettext_default("Error: object must have a content"))

    try:
        new_alias = await _normalize_alias(
            db_session, alias, exclude_id=maybe_object.id
        )
    except HTTPException as exc:
        return await _rerender(str(exc.detail))

    # A CW always implies sensitive, mirroring the compose form's rule.
    is_sensitive = True if content_warning else is_sensitive

    alias_changed = new_alias != maybe_object.alias
    object_changed = (
        content != maybe_object.source
        or (name or None) != maybe_object.name
        or (content_warning or None) != maybe_object.summary
        or is_sensitive != maybe_object.sensitive
    )

    if object_changed:
        # Set the alias first: send_update rebuilds the note with
        # `"url": outbox_object.url`, so the property picks the new alias up.
        # Don't rewrite ap_object here -- send_update snapshots the current
        # one into `revisions`, and we want that snapshot to hold the *old*
        # url.
        if alias_changed:
            maybe_object.alias = new_alias
        await boxes.send_update(
            db_session,
            ap_id=maybe_object.ap_id,
            source=content,
            name=name,
            content_warning=content_warning or None,
            is_sensitive=is_sensitive,
        )
    elif alias_changed:
        await boxes.set_outbox_object_alias(db_session, maybe_object, new_alias)

    return RedirectResponse(maybe_object.url, status_code=302)  # type: ignore


@router.get("/edit_history/{public_id}", response_model=None)
async def admin_edit_history(
    request: Request,
    public_id: str,
    db_session: AsyncSession = Depends(get_db_session),
) -> templates.TemplateResponse:
    maybe_object = (
        (
            await db_session.execute(
                select(activitypub.models.OutboxObject).where(
                    activitypub.models.OutboxObject.public_id == public_id,
                    activitypub.models.OutboxObject.is_deleted.is_(False),
                )
            )
        )
        .unique()
        .scalar_one_or_none()
    )
    if not maybe_object:
        raise HTTPException(status_code=404)

    return await templates.render_template(
        db_session,
        request,
        "admin_edit_history.html",
        {
            "outbox_object": maybe_object,
            "revisions": maybe_object.revisions or [],
        },
    )


@router.post("/actions/vote", response_model=None)
async def admin_actions_vote(
    request: Request,
    redirect_url: str = Form(),
    in_reply_to: str = Form(),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    form_data = await request.form()
    names = list(map(lambda data: str(data), form_data.getlist("name")))
    logger.info(f"{names=}")
    await boxes.send_vote(
        db_session,
        in_reply_to=in_reply_to,
        names=names,
    )
    return RedirectResponse(redirect_url, status_code=302)


@unauthenticated_router.get("/login", response_model=None)
async def login(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> templates.TemplateResponse | RedirectResponse:
    if is_current_user_admin(request):
        return RedirectResponse(request.url_for("admin_stream"), status_code=302)

    return await templates.render_template(
        db_session,
        request,
        "login.html",
        {
            "csrf_token": generate_csrf_token(),
            "redirect": request.query_params.get("redirect", ""),
        },
    )


@unauthenticated_router.post("/login", response_model=None)
async def login_validation(
    request: Request,
    password: str = Form(),
    redirect: str | None = Form(None),
    csrf_check: None = Depends(verify_csrf_token),
    db_session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse | templates.TemplateResponse:
    if not verify_password(password):
        logger.warning("Invalid password")
        return await templates.render_template(
            db_session,
            request,
            "login.html",
            {
                "error": "Invalid password",
                "csrf_token": generate_csrf_token(),
                "redirect": request.query_params.get("redirect", ""),
            },
            status_code=403,
        )

    resp = RedirectResponse(
        redirect or request.url_for("admin_stream"), status_code=302
    )
    resp.set_cookie("session", session_serializer.dumps({"is_logged_in": True}))  # type: ignore  # noqa: E501

    return resp


@router.get("/logout", response_model=None)
async def logout(
    request: Request,
) -> RedirectResponse:
    resp = RedirectResponse(request.url_for("index"), status_code=302)
    resp.set_cookie("session", session_serializer.dumps({"is_logged_in": False}))  # type: ignore  # noqa: E501
    return resp
