"""Actions related to the AP inbox/outbox."""

import asyncio
import datetime
import html
import time
import uuid
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

import fastapi
import httpx
from loguru import logger
from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

import activitypub.models
from activitypub import activitypub as ap
from activitypub.actor import LOCAL_ACTOR
from activitypub.actor import Actor
from activitypub.actor import RemoteActor
from activitypub.actor import fetch_actor
from activitypub.actor import save_actor
from activitypub.actor import update_actor_if_needed
from activitypub.ap_object import RemoteObject
from activitypub.outgoing_activities import new_outgoing_activity

# TODO: this app.models is mostly used for WebMention (which is not ActivityPub AFAK).
# This may contradict be related info: https://www.w3.org/TR/social-web-protocols/#delivery-interop
# It should be easy to create a non-hard-bonding to other protocols (i.e., event-based.)
# TODO: What can we refactor in the library from these imports and config?
from app import config
from app import ldsig
from app import models
from app.config import BASE_URL
from app.config import ID
from app.config import MANUALLY_APPROVES_FOLLOWERS
from app.config import set_moved_to
from app.config import stream_visibility_callback
from app.customization import ObjectInfo
from app.database import AsyncSession
from app.source import dedup_tags
from app.source import markdownify
from app.uploads import upload_to_attachment
from app.utils import opengraph
from app.utils import webmentions
from app.utils.datetime import as_utc
from app.utils.datetime import now
from app.utils.datetime import parse_isoformat
from app.utils.facepile import WebmentionReply
from app.utils.text import slugify
from app.utils.url import is_hostname_blocked

AnyboxObject = activitypub.models.InboxObject | activitypub.models.OutboxObject


def is_notification_enabled(notification_type: models.NotificationType) -> bool:
    """Checks if a given notification type is enabled."""
    if notification_type.value in (
        "pending_incoming_follower",
        "pending_incoming_quote_request",
    ):
        # These cannot be disabled as it would prevent manually reviewing
        # follow/quote requests.
        return True
    if notification_type.value in config.CONFIG.disabled_notifications:
        return False
    return True


def allocate_outbox_id() -> str:
    return uuid.uuid4().hex


def outbox_object_id(outbox_id) -> str:
    return f"{BASE_URL}/o/{outbox_id}"


async def fetch_outbox(
    db_session: AsyncSession,
    object_type=["Note"],  # TODO: convert to ap_object.!!!
    public_only=True,
    posts_limit=20,
) -> list[activitypub.models.OutboxObject]:
    # Default restrictions unless the request is authenticated with an access token
    # TODO Copied code from app.main... does it make sense to only restrict types when PUBLIC?
    restricted_where = [
        activitypub.models.OutboxObject.visibility == ap.VisibilityEnum.PUBLIC,
        activitypub.models.OutboxObject.ap_type.in_(object_type),
    ]

    # By design, we only show the last 20 public activities in the oubox
    stmt = (
        select(activitypub.models.OutboxObject)
        .where(
            activitypub.models.OutboxObject.is_deleted.is_(False),
            *([] if not public_only else restricted_where),
        )
        .order_by(activitypub.models.OutboxObject.ap_published_at.desc())
        .limit(posts_limit)
    )
    result = await db_session.scalars(stmt)
    outbox_objects = result.all()

    return outbox_objects


async def save_outbox_object(
    db_session: AsyncSession,
    public_id: str,
    raw_object: ap.RawObject,
    relates_to_inbox_object_id: int | None = None,
    relates_to_outbox_object_id: int | None = None,
    relates_to_actor_id: int | None = None,
    source: str | None = None,
    is_transient: bool = False,
    conversation: str | None = None,
    slug: str | None = None,
) -> activitypub.models.OutboxObject:
    ro = await RemoteObject.from_raw_object(raw_object)

    outbox_object = activitypub.models.OutboxObject(
        public_id=public_id,
        ap_type=ro.ap_type,
        ap_id=ro.ap_id,
        ap_context=ro.ap_context,
        ap_object=ro.ap_object,
        visibility=ro.visibility,
        og_meta=await opengraph.og_meta_from_note(db_session, ro),
        relates_to_inbox_object_id=relates_to_inbox_object_id,
        relates_to_outbox_object_id=relates_to_outbox_object_id,
        relates_to_actor_id=relates_to_actor_id,
        activity_object_ap_id=ro.activity_object_ap_id,
        is_hidden_from_homepage=True if ro.in_reply_to else False,
        source=source,
        is_transient=is_transient,
        conversation=conversation,
        slug=slug,
    )
    db_session.add(outbox_object)
    await db_session.flush()
    await db_session.refresh(outbox_object)

    return outbox_object


async def send_unblock(db_session: AsyncSession, ap_actor_id: str) -> None:
    actor = await fetch_actor(db_session, ap_actor_id)

    block_activity = (
        await db_session.scalars(
            select(activitypub.models.OutboxObject).where(
                activitypub.models.OutboxObject.activity_object_ap_id == actor.ap_id,
                activitypub.models.OutboxObject.is_deleted.is_(False),
            )
        )
    ).one_or_none()
    if not block_activity:
        raise ValueError(f"No Block activity for {ap_actor_id}")

    await _send_undo(db_session, block_activity.ap_id)

    await db_session.commit()


async def send_block(db_session: AsyncSession, ap_actor_id: str) -> None:
    logger.info(f"Blocking {ap_actor_id}")
    actor = await fetch_actor(db_session, ap_actor_id)
    actor.is_blocked = True

    # 1. Unfollow the actor
    following = (
        await db_session.scalars(
            select(activitypub.models.Following)
            .options(joinedload(activitypub.models.Following.outbox_object))
            .where(
                activitypub.models.Following.ap_actor_id == actor.ap_id,
            )
        )
    ).one_or_none()
    if following:
        await _send_undo(db_session, following.outbox_object.ap_id)

    # 2. If the blocked actor is a follower, reject the follow request
    follower = (
        await db_session.scalars(
            select(activitypub.models.Follower)
            .options(joinedload(activitypub.models.Follower.inbox_object))
            .where(
                activitypub.models.Follower.ap_actor_id == actor.ap_id,
            )
        )
    ).one_or_none()
    if follower:
        await _send_reject(db_session, actor, follower.inbox_object)
        await db_session.delete(follower)

    # 3. Send a block
    block_id = allocate_outbox_id()
    block = {
        "@context": ap.AS_EXTENDED_CTX,
        "id": outbox_object_id(block_id),
        "type": "Block",
        "actor": LOCAL_ACTOR.ap_id,
        "object": actor.ap_id,
    }
    outbox_object = await save_outbox_object(
        db_session,
        block_id,
        block,
    )
    if not outbox_object.id:
        raise ValueError("Should never happen")

    await new_outgoing_activity(db_session, actor.inbox_url, outbox_object.id)

    # 4. Create a notification
    if is_notification_enabled(models.NotificationType.BLOCK):
        notif = models.Notification(
            notification_type=models.NotificationType.BLOCK,
            actor_id=actor.id,
            outbox_object_id=outbox_object.id,
        )
        db_session.add(notif)

    await db_session.commit()


async def send_delete(db_session: AsyncSession, ap_object_id: str) -> None:
    outbox_object_to_delete = await get_outbox_object_by_ap_id(db_session, ap_object_id)
    if not outbox_object_to_delete:
        raise ValueError(f"{ap_object_id} not found in the outbox")

    delete_id = allocate_outbox_id()
    # FIXME addressing
    delete = {
        "@context": ap.AS_EXTENDED_CTX,
        "id": outbox_object_id(delete_id),
        "type": "Delete",
        "actor": ID,
        "object": {
            "type": "Tombstone",
            "id": ap_object_id,
        },
    }
    outbox_object = await save_outbox_object(
        db_session,
        delete_id,
        delete,
        relates_to_outbox_object_id=outbox_object_to_delete.id,
    )
    if not outbox_object.id:
        raise ValueError("Should never happen")

    outbox_object_to_delete.is_deleted = True
    await db_session.flush()

    # Compute the original recipients
    recipients = await _compute_recipients(
        db_session, outbox_object_to_delete.ap_object
    )
    for rcp in recipients:
        await new_outgoing_activity(db_session, rcp, outbox_object.id)

    # Revert side effects
    if outbox_object_to_delete.in_reply_to:
        replied_object = await get_anybox_object_by_ap_id(
            db_session, outbox_object_to_delete.in_reply_to
        )
        if replied_object:
            if replied_object.is_from_outbox:
                # Different helper here because we also count webmentions
                new_replies_count = await _get_outbox_replies_count(
                    db_session, replied_object  # type: ignore
                )
            else:
                new_replies_count = await _get_replies_count(
                    db_session, replied_object.ap_id
                )

            replied_object.replies_count = new_replies_count
        else:
            logger.info(f"{outbox_object_to_delete.in_reply_to} not found")

    await db_session.commit()


# Ceiling on a single quote-related fetch. The inbox worker already caps a
# whole activity at 60s, but that is 4x SQLite's `busy_timeout`, and these
# fetches happen with the writer lock held -- see `_process_inbound_quote`.
_QUOTE_FETCH_TIMEOUT_SECONDS = 5.0


def _quote_wire_fields(quoted_ap_id: str) -> dict[str, Any]:
    """The wire-format keys advertising a quote: FEP-044f's `quote` plus the
    legacy aliases (Fedibird/Mastodon/Misskey). Emitting all of them means a
    quote still shows up (as a quote, not just the `RE:` link) on servers
    that only understand one.
    """
    return {
        "quote": quoted_ap_id,
        "quoteUri": quoted_ap_id,
        "quoteUrl": quoted_ap_id,
        "_misskey_quote": quoted_ap_id,
    }


def _quote_tag(quoted_ap_id: str) -> ap.RawObject:
    """FEP-e232 `tag` Link entry, the alias Misskey/Akkoma actually look at."""
    return {
        "type": "Link",
        "mediaType": f'application/ld+json; profile="{ap.AS_CTX}"',
        "rel": ap.MISSKEY_QUOTE_TAG_REL,
        "href": quoted_ap_id,
    }


def _quote_reply_link_html(quoted_url: str) -> str:
    """A visible `RE: <url>` link, appended to the content -- the same
    degrade-to-a-link behavior Mastodon uses, so a quote posted from here
    still makes sense on a server that understands none of the above.

    `quoted_url` comes from a remote object's `url`/`id` (or an admin-supplied
    `quote_of`), so it's escaped and scheme-checked before being embedded in
    raw HTML: unescaped, a hostile value could break out of the `href`
    attribute, and an unchecked scheme could turn it into a `javascript:` link.
    """
    if not quoted_url.startswith(("http://", "https://")):
        return ""
    safe_url = html.escape(quoted_url, quote=True)
    return (
        '<p><span class="quote-inline">RE: '
        f'<a href="{safe_url}" rel="noopener">{safe_url}</a></span></p>'
    )


def _quote_interaction_policy(visibility: ap.VisibilityEnum) -> ap.RawObject:
    """Advertise who's auto-approved to quote a post, from `quote_policy`
    (`_handle_quote_request_activity` is what actually enforces it for
    requests *we* receive, and this must stay consistent with it).

    `automaticApproval` only ever names the local followers collection, the
    public collection, or (for "manual"/"nobody", where nobody but the
    author is ever auto-approved) the author's own id -- never an empty
    list, which `_remote_quote_policy_forbids` relies on to tell "nobody"
    apart from "no policy published at all".
    """
    policy = config.CONFIG.quote_policy
    if policy == "public" and visibility in (
        ap.VisibilityEnum.PUBLIC,
        ap.VisibilityEnum.UNLISTED,
    ):
        automatic_approval = [ap.AS_PUBLIC]
    elif policy == "followers":
        automatic_approval = [f"{BASE_URL}/followers"]
    else:
        automatic_approval = [ID]

    return {
        "interactionPolicy": {"canQuote": {"automaticApproval": automatic_approval}}
    }


def _remote_quote_policy_forbids(quoted_object: AnyboxObject) -> bool:
    """True when the quoted object's own `interactionPolicy` names only its
    author as auto-approved, i.e. nobody else can ever be auto-approved to
    quote it. Sending a `QuoteRequest` in that case can still get a manual
    approval on servers that support it, but on this instance's own inbound
    handling (see `_handle_quote_request_activity`) an equivalent request
    would always be rejected -- so fail the compose here instead of sending
    a request that (for us, and for any GoToSocial-alike with the same
    policy) is never going anywhere.
    """
    policy = quoted_object.ap_object.get("interactionPolicy")
    if not isinstance(policy, dict):
        return False

    can_quote = policy.get("canQuote")
    if not isinstance(can_quote, dict):
        return False

    automatic_approval = ap.as_list(can_quote.get("automaticApproval") or [])
    if not automatic_approval:
        return False

    approved_ap_ids = {ap.get_id(item) for item in automatic_approval}
    return approved_ap_ids == {quoted_object.ap_actor_id}


async def _resolve_quoted_object(
    db_session: AsyncSession, quote_of: str
) -> AnyboxObject:
    """Fetch-then-reload idiom from `send_like`: an unknown quoted object is
    saved to the inbox and re-queried rather than used in place, since
    lazy-loading its actor mid-transaction fails under the async engine.
    """
    quoted_object = await get_anybox_object_by_ap_id(db_session, quote_of)
    if not quoted_object:
        logger.info(f"Saving unknwown object {quote_of}")
        raw_object = await ap.fetch(quote_of)
        await save_object_to_inbox(db_session, raw_object)
        await db_session.commit()
        quoted_object = await get_anybox_object_by_ap_id(db_session, quote_of)
        if not quoted_object:
            raise ValueError("Should never happen")

    return quoted_object


async def _mint_quote_authorization(
    db_session: AsyncSession,
    quoting_object_ap_id: str,
    quoted_object_ap_id: str,
    relates_to_inbox_object_id: int | None = None,
) -> activitypub.models.OutboxObject:
    """Mint a `QuoteAuthorization` (FEP-044f's "stamp"). Reuses OutboxObject
    rather than a dedicated table, which makes it servable at its own
    `/o/{public_id}` for free.

    The spec forbids embedding the quoting/quoted objects in the stamp
    (information leakage), so both stay bare AP ids.
    """
    stamp_id = allocate_outbox_id()
    stamp = {
        "@context": ap.AS_EXTENDED_CTX,
        "id": outbox_object_id(stamp_id),
        "type": "QuoteAuthorization",
        "attributedTo": ID,
        "interactingObject": quoting_object_ap_id,
        "interactionTarget": quoted_object_ap_id,
    }
    stamp_object = await save_outbox_object(
        db_session,
        stamp_id,
        stamp,
        relates_to_inbox_object_id=relates_to_inbox_object_id,
    )
    if not stamp_object.id:
        raise ValueError("Should never happen")

    # Not a reply, so save_outbox_object would default this to False; a
    # stamp is metadata, not something to show on the homepage.
    stamp_object.is_hidden_from_homepage = True

    return stamp_object


async def send_quote_request(
    db_session: AsyncSession,
    quote_outbox_object: activitypub.models.OutboxObject,
    quoted_object: activitypub.models.InboxObject,
) -> None:
    """Ask the quoted post's author for a `QuoteAuthorization`, modelled on
    `send_like`: a single-inbox delivery, no `_compute_recipients`.
    """
    quote_request_id = allocate_outbox_id()
    quote_request = {
        "@context": ap.AS_EXTENDED_CTX,
        "id": outbox_object_id(quote_request_id),
        "type": "QuoteRequest",
        "actor": ID,
        "object": quoted_object.ap_id,
        "instrument": quote_outbox_object.ap_id,
    }
    outbox_object = await save_outbox_object(
        db_session,
        quote_request_id,
        quote_request,
        # So the Accept/Reject handler (which resolves `relates_to_outbox_object`
        # from the *QuoteRequest's own ap_id*, addressed by the incoming Accept)
        # can navigate back to the quote post.
        relates_to_outbox_object_id=quote_outbox_object.id,
    )
    if not outbox_object.id:
        raise ValueError("Should never happen")

    await new_outgoing_activity(
        db_session, quoted_object.actor.inbox_url, outbox_object.id
    )


async def send_like(db_session: AsyncSession, ap_object_id: str) -> None:
    inbox_object = await get_inbox_object_by_ap_id(db_session, ap_object_id)
    if not inbox_object:
        logger.info(f"Saving unknwown object {ap_object_id}")
        raw_object = await ap.fetch(ap.get_id(ap_object_id))
        await save_object_to_inbox(db_session, raw_object)
        await db_session.commit()
        # XXX: we need to reload it as lazy-loading the actor will fail
        # (asyncio SQLAlchemy issue)
        inbox_object = await get_inbox_object_by_ap_id(db_session, ap_object_id)
        if not inbox_object:
            raise ValueError("Should never happen")

    like_id = allocate_outbox_id()
    like = {
        "@context": ap.AS_CTX,
        "id": outbox_object_id(like_id),
        "type": "Like",
        "actor": ID,
        "object": ap_object_id,
    }
    outbox_object = await save_outbox_object(
        db_session, like_id, like, relates_to_inbox_object_id=inbox_object.id
    )
    if not outbox_object.id:
        raise ValueError("Should never happen")

    inbox_object.liked_via_outbox_object_ap_id = outbox_object.ap_id

    await new_outgoing_activity(
        db_session, inbox_object.actor.inbox_url, outbox_object.id
    )
    await db_session.commit()


async def send_announce(db_session: AsyncSession, ap_object_id: str) -> None:
    inbox_object = await get_inbox_object_by_ap_id(db_session, ap_object_id)
    if not inbox_object:
        logger.info(f"Saving unknwown object {ap_object_id}")
        raw_object = await ap.fetch(ap.get_id(ap_object_id))
        await save_object_to_inbox(db_session, raw_object)
        await db_session.commit()
        # XXX: we need to reload it as lazy-loading the actor will fail
        # (asyncio SQLAlchemy issue)
        inbox_object = await get_inbox_object_by_ap_id(db_session, ap_object_id)
        if not inbox_object:
            raise ValueError("Should never happen")

    if inbox_object.visibility not in [
        ap.VisibilityEnum.PUBLIC,
        ap.VisibilityEnum.UNLISTED,
    ]:
        raise ValueError("Cannot announce non-public object")

    announce_id = allocate_outbox_id()
    announce = {
        "@context": ap.AS_CTX,
        "id": outbox_object_id(announce_id),
        "type": "Announce",
        "actor": ID,
        "object": ap_object_id,
        "to": [ap.AS_PUBLIC],
        "cc": [
            f"{BASE_URL}/followers",
            inbox_object.ap_actor_id,
        ],
    }
    outbox_object = await save_outbox_object(
        db_session, announce_id, announce, relates_to_inbox_object_id=inbox_object.id
    )
    if not outbox_object.id:
        raise ValueError("Should never happen")

    inbox_object.announced_via_outbox_object_ap_id = outbox_object.ap_id

    recipients = await _compute_recipients(db_session, announce)
    for rcp in recipients:
        await new_outgoing_activity(db_session, rcp, outbox_object.id)

    await db_session.commit()


async def send_follow(db_session: AsyncSession, ap_actor_id: str) -> None:
    await _send_follow(db_session, ap_actor_id)
    await db_session.commit()


async def _send_follow(db_session: AsyncSession, ap_actor_id: str) -> None:
    actor = await fetch_actor(db_session, ap_actor_id)

    follow_id = allocate_outbox_id()
    follow = {
        "@context": ap.AS_CTX,
        "id": outbox_object_id(follow_id),
        "type": "Follow",
        "actor": ID,
        "object": ap_actor_id,
    }

    outbox_object = await save_outbox_object(
        db_session, follow_id, follow, relates_to_actor_id=actor.id
    )
    if not outbox_object.id:
        raise ValueError("Should never happen")

    await new_outgoing_activity(db_session, actor.inbox_url, outbox_object.id)

    # Caller should commit


async def send_undo(db_session: AsyncSession, ap_object_id: str) -> None:
    await _send_undo(db_session, ap_object_id)
    await db_session.commit()


async def _send_undo(db_session: AsyncSession, ap_object_id: str) -> None:
    outbox_object_to_undo = await get_outbox_object_by_ap_id(db_session, ap_object_id)
    if not outbox_object_to_undo:
        raise ValueError(f"{ap_object_id} not found in the outbox")

    if outbox_object_to_undo.ap_type not in ["Follow", "Like", "Announce", "Block"]:
        raise ValueError(
            f"Cannot build Undo for {outbox_object_to_undo.ap_type} activity"
        )

    undo_id = allocate_outbox_id()
    undo = {
        "@context": ap.AS_CTX,
        "id": outbox_object_id(undo_id),
        "type": "Undo",
        "actor": ID,
        "object": ap.remove_context(outbox_object_to_undo.ap_object),
    }

    outbox_object = await save_outbox_object(
        db_session,
        undo_id,
        undo,
        relates_to_outbox_object_id=outbox_object_to_undo.id,
    )
    if not outbox_object.id:
        raise ValueError("Should never happen")

    outbox_object_to_undo.undone_by_outbox_object_id = outbox_object.id
    outbox_object_to_undo.is_deleted = True

    if outbox_object_to_undo.ap_type == "Follow":
        if not outbox_object_to_undo.activity_object_ap_id:
            raise ValueError("Should never happen")
        followed_actor = await fetch_actor(
            db_session, outbox_object_to_undo.activity_object_ap_id
        )
        await new_outgoing_activity(
            db_session,
            followed_actor.inbox_url,
            outbox_object.id,
        )
        # Also remove the follow from the following collection
        await db_session.execute(
            delete(activitypub.models.Following).where(
                activitypub.models.Following.ap_actor_id == followed_actor.ap_id
            )
        )
    elif outbox_object_to_undo.ap_type == "Like":
        liked_object_ap_id = outbox_object_to_undo.activity_object_ap_id
        if not liked_object_ap_id:
            raise ValueError("Should never happen")
        liked_object = await get_inbox_object_by_ap_id(db_session, liked_object_ap_id)
        if not liked_object:
            raise ValueError(f"Cannot find liked object {liked_object_ap_id}")
        liked_object.liked_via_outbox_object_ap_id = None

        # Send the Undo to the liked object's actor
        await new_outgoing_activity(
            db_session,
            liked_object.actor.inbox_url,  # type: ignore
            outbox_object.id,
        )
    elif outbox_object_to_undo.ap_type == "Announce":
        announced_object_ap_id = outbox_object_to_undo.activity_object_ap_id
        if not announced_object_ap_id:
            raise ValueError("Should never happen")
        announced_object = await get_inbox_object_by_ap_id(
            db_session, announced_object_ap_id
        )
        if not announced_object:
            raise ValueError(f"Cannot find announced object {announced_object_ap_id}")
        announced_object.announced_via_outbox_object_ap_id = None

        # Send the Undo to the original recipients
        recipients = await _compute_recipients(
            db_session, outbox_object_to_undo.ap_object
        )
        for rcp in recipients:
            await new_outgoing_activity(db_session, rcp, outbox_object.id)
    elif outbox_object_to_undo.ap_type == "Block":
        if not outbox_object_to_undo.activity_object_ap_id:
            raise ValueError(f"Invalid block activity {outbox_object_to_undo.ap_id}")

        # Send the Undo to the blocked actor
        blocked_actor = await fetch_actor(
            db_session, outbox_object_to_undo.activity_object_ap_id
        )

        blocked_actor.is_blocked = False

        await new_outgoing_activity(
            db_session,
            blocked_actor.inbox_url,  # type: ignore
            outbox_object.id,
        )

        if is_notification_enabled(models.NotificationType.UNBLOCK):
            notif = models.Notification(
                notification_type=models.NotificationType.UNBLOCK,
                actor_id=blocked_actor.id,
                outbox_object_id=outbox_object.id,
            )
            db_session.add(notif)

    else:
        raise ValueError("Should never happen")

    # called should commit


async def fetch_conversation_root(
    db_session: AsyncSession,
    obj: AnyboxObject | RemoteObject,
    is_root: bool = False,
    depth: int = 0,
) -> str:
    """Some softwares do not set the context/conversation field (like Misskey).
    This means we have to track conversation ourselves. To do so, we fetch
    the root of the conversation and either:
     - use the context field if set
     - or build a custom conversation ID
    """
    logger.info(f"Fetching convo root for ap_id={obj.ap_id}/{depth=}")
    if obj.ap_context:
        return obj.ap_context

    if not obj.in_reply_to or is_root or depth > 10:
        # Use the root AP ID if there'no context
        return f"microblogpub:root:{obj.ap_id}"
    else:
        in_reply_to_object: AnyboxObject | RemoteObject | None = (
            await get_anybox_object_by_ap_id(db_session, obj.in_reply_to)
        )
        if not in_reply_to_object:
            try:
                raw_reply = await ap.fetch(ap.get_id(obj.in_reply_to))
                raw_reply_actor = await fetch_actor(
                    db_session, ap.get_actor_id(raw_reply)
                )
                in_reply_to_object = RemoteObject(raw_reply, actor=raw_reply_actor)
            except (
                ap.FetchError,
                ap.NotAnObjectError,
            ):
                return await fetch_conversation_root(
                    db_session, obj, is_root=True, depth=depth + 1
                )
            except httpx.HTTPStatusError as http_status_error:
                if 400 <= http_status_error.response.status_code < 500:
                    # We may not have access, in this case consider if root
                    return await fetch_conversation_root(
                        db_session, obj, is_root=True, depth=depth + 1
                    )
                else:
                    raise

        return await fetch_conversation_root(
            db_session, in_reply_to_object, depth=depth + 1
        )


async def send_move(
    db_session: AsyncSession,
    target: str,
) -> None:
    move_id = allocate_outbox_id()
    obj = {
        "@context": ap.AS_CTX,
        "type": "Move",
        "id": outbox_object_id(move_id),
        "actor": LOCAL_ACTOR.ap_id,
        "object": LOCAL_ACTOR.ap_id,
        "target": target,
    }

    outbox_object = await save_outbox_object(db_session, move_id, obj)
    if not outbox_object.id:
        raise ValueError("Should never happen")

    recipients = await _get_followers_recipients(db_session)
    for rcp in recipients:
        await new_outgoing_activity(db_session, rcp, outbox_object.id)

    # Store the moved to in order to update the profile
    set_moved_to(target)

    await db_session.commit()


async def send_self_destruct(db_session: AsyncSession) -> None:
    delete_id = allocate_outbox_id()
    delete = {
        "@context": ap.AS_EXTENDED_CTX,
        "id": outbox_object_id(delete_id),
        "type": "Delete",
        "actor": ID,
        "object": ID,
        "to": [ap.AS_PUBLIC],
    }
    outbox_object = await save_outbox_object(
        db_session,
        delete_id,
        delete,
    )
    if not outbox_object.id:
        raise ValueError("Should never happen")

    recipients = await compute_all_known_recipients(db_session)
    for rcp in recipients:
        await new_outgoing_activity(db_session, rcp, outbox_object.id)

    await db_session.commit()


async def send_create(
    db_session: AsyncSession,
    ap_type: str,
    source: str,
    uploads: list[tuple[activitypub.models.Upload, str, str | None]],
    in_reply_to: str | None,
    visibility: ap.VisibilityEnum,
    content_warning: str | None = None,
    is_sensitive: bool = False,
    poll_type: str | None = None,
    poll_answers: list[str] | None = None,
    poll_duration_in_minutes: int | None = None,
    name: str | None = None,
    language: str | None = None,
    quote_of: str | None = None,
) -> tuple[str, activitypub.models.OutboxObject]:
    note_id = allocate_outbox_id()
    published = now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
    context = f"{ID}/contexts/" + uuid.uuid4().hex
    conversation = context
    content, tags, mentioned_actors = await markdownify(db_session, source)
    attachments = []

    in_reply_to_object: AnyboxObject | None = None
    if in_reply_to:
        in_reply_to_object = await get_anybox_object_by_ap_id(db_session, in_reply_to)
        if not in_reply_to_object:
            raise ValueError(f"Invalid in reply to {in_reply_to=}")
        if not in_reply_to_object.ap_context:
            logger.warning(f"Replied object {in_reply_to} has no context")
            try:
                conversation = await fetch_conversation_root(
                    db_session,
                    in_reply_to_object,
                )
            except Exception:
                logger.exception(f"Failed to fetch convo root {in_reply_to}")
        else:
            context = in_reply_to_object.ap_context
            conversation = in_reply_to_object.ap_context

    quoted_object: AnyboxObject | None = None
    quote_state: str | None = None
    if quote_of:
        quoted_object = await _resolve_quoted_object(db_session, quote_of)
        if not quoted_object.is_from_outbox and _remote_quote_policy_forbids(
            quoted_object
        ):
            raise ValueError(f"{quote_of} does not allow being quoted")
        quote_state = "accepted" if quoted_object.is_from_outbox else "pending"
        content += _quote_reply_link_html(quoted_object.url or quoted_object.ap_id)

    for upload, filename, alt_text in uploads:
        attachments.append(upload_to_attachment(upload, filename, alt_text))

    to = []
    cc = []
    mentioned_actor_ap_ids = [actor.ap_id for actor in mentioned_actors]
    if visibility == ap.VisibilityEnum.PUBLIC:
        to = [ap.AS_PUBLIC]
        cc = [f"{BASE_URL}/followers"] + mentioned_actor_ap_ids
    elif visibility == ap.VisibilityEnum.UNLISTED:
        to = [f"{BASE_URL}/followers"]
        cc = [ap.AS_PUBLIC] + mentioned_actor_ap_ids
    elif visibility == ap.VisibilityEnum.FOLLOWERS_ONLY:
        to = [f"{BASE_URL}/followers"]
        cc = mentioned_actor_ap_ids
    elif visibility == ap.VisibilityEnum.DIRECT:
        to = mentioned_actor_ap_ids
        cc = []
    else:
        raise ValueError(f"Unhandled visibility {visibility}")

    slug = None
    url = outbox_object_id(note_id)

    extra_obj_attrs = {}
    if ap_type == "Question":
        if not poll_answers or len(poll_answers) < 2:
            raise ValueError("Question must have at least 2 possible answers")

        if not poll_type:
            raise ValueError("Mising poll_type")

        if not poll_duration_in_minutes:
            raise ValueError("Missing poll_duration_in_minutes")

        extra_obj_attrs = {
            "votersCount": 0,
            "endTime": (now() + timedelta(minutes=poll_duration_in_minutes))
            .isoformat()
            .replace("+00:00", "Z"),
            poll_type: [
                {
                    "type": "Note",
                    "name": answer,
                    "replies": {"type": "Collection", "totalItems": 0},
                }
                for answer in poll_answers
            ],
        }
    elif ap_type == "Article":
        if not name:
            raise ValueError("Article must have a name")

        slug = slugify(name)
        url = f"{BASE_URL}/articles/{note_id[:7]}/{slug}"
        extra_obj_attrs = {"name": name}

    # Mastodon-style per-post language: expose the natural-language properties
    # as language maps so remote servers know the content language. Absent when
    # no language is set.
    lang_maps: dict[str, dict[str, str]] = {}
    if language:
        lang_maps["contentMap"] = {language: content}
        if content_warning:
            lang_maps["summaryMap"] = {language: content_warning}
        if ap_type == "Article" and name:
            lang_maps["nameMap"] = {language: name}

    tag_list = dedup_tags(tags)
    quote_wire_fields: dict[str, Any] = {}
    if quoted_object:
        tag_list = tag_list + [_quote_tag(quoted_object.ap_id)]
        quote_wire_fields = _quote_wire_fields(quoted_object.ap_id)

    obj = {
        "@context": ap.AS_EXTENDED_CTX,
        "type": ap_type,
        "id": outbox_object_id(note_id),
        "attributedTo": ID,
        "content": content,
        "to": to,
        "cc": cc,
        "published": published,
        "context": context,
        "conversation": context,
        "url": url,
        "tag": tag_list,
        "summary": content_warning,
        "inReplyTo": in_reply_to,
        "sensitive": is_sensitive,
        "attachment": attachments,
        **extra_obj_attrs,  # type: ignore
        **lang_maps,  # type: ignore
        **quote_wire_fields,  # type: ignore
        **_quote_interaction_policy(visibility),  # type: ignore
    }
    outbox_object = await save_outbox_object(
        db_session,
        note_id,
        obj,
        source=source,
        conversation=conversation,
        slug=slug,
    )
    if not outbox_object.id:
        raise ValueError("Should never happen")

    if quoted_object:
        outbox_object.quote_ap_id = quoted_object.ap_id
        outbox_object.quote_state = quote_state
        if quoted_object.is_from_outbox:
            # Quoting our own post: no consent to ask, mint the stamp locally.
            stamp_object = await _mint_quote_authorization(
                db_session,
                quoting_object_ap_id=outbox_object.ap_id,
                quoted_object_ap_id=quoted_object.ap_id,
            )
            outbox_object.quote_authorization_ap_id = stamp_object.ap_id
            updated_note = dict(outbox_object.ap_object)
            updated_note["quoteAuthorization"] = stamp_object.ap_id
            outbox_object.ap_object = updated_note

    for tag in tags:
        if tag["type"] == "Hashtag":
            tagged_object = activitypub.models.TaggedOutboxObject(
                tag=tag["name"][1:].lower(),
                outbox_object_id=outbox_object.id,
            )
            db_session.add(tagged_object)

    for upload, filename, alt in uploads:
        outbox_object_attachment = activitypub.models.OutboxObjectAttachment(
            filename=filename,
            alt=alt,
            outbox_object_id=outbox_object.id,
            upload_id=upload.id,
        )
        db_session.add(outbox_object_attachment)

    if quoted_object and isinstance(quoted_object, activitypub.models.InboxObject):
        await send_quote_request(db_session, outbox_object, quoted_object)

    recipients = await _compute_recipients(db_session, obj)
    for rcp in recipients:
        await new_outgoing_activity(db_session, rcp, outbox_object.id)

    # If the note is public, check if we need to send any webmentions
    if visibility == ap.VisibilityEnum.PUBLIC:
        possible_targets = await opengraph.external_urls(db_session, outbox_object)
        logger.info(f"webmentions possible targert {possible_targets}")
        for target in possible_targets:
            webmention_endpoint = await webmentions.discover_webmention_endpoint(target)
            logger.info(f"{target=} {webmention_endpoint=}")
            if webmention_endpoint:
                await new_outgoing_activity(
                    db_session,
                    webmention_endpoint,
                    outbox_object_id=outbox_object.id,
                    webmention_target=target,
                )

    await db_session.commit()

    # Refresh the replies counter if needed
    if in_reply_to_object:
        new_replies_count = await _get_replies_count(
            db_session, in_reply_to_object.ap_id
        )
        if in_reply_to_object.is_from_outbox:
            await db_session.execute(
                update(activitypub.models.OutboxObject)
                .where(
                    activitypub.models.OutboxObject.ap_id == in_reply_to_object.ap_id,
                )
                .values(replies_count=new_replies_count)
            )
        elif in_reply_to_object.is_from_inbox:
            await db_session.execute(
                update(activitypub.models.InboxObject)
                .where(
                    activitypub.models.InboxObject.ap_id == in_reply_to_object.ap_id,
                )
                .values(replies_count=new_replies_count)
            )

    await db_session.commit()

    return note_id, outbox_object


async def send_vote(
    db_session: AsyncSession,
    in_reply_to: str,
    names: list[str],
) -> str:
    logger.info(f"Send vote {names}")
    published = now().replace(microsecond=0).isoformat().replace("+00:00", "Z")

    in_reply_to_object = await get_inbox_object_by_ap_id(db_session, in_reply_to)
    if not in_reply_to_object:
        raise ValueError(f"Invalid in reply to {in_reply_to=}")
    if not in_reply_to_object.ap_context:
        raise ValueError("Object has no context")
    context = in_reply_to_object.ap_context

    # ensure the name are valid

    # Save the answers
    in_reply_to_object.voted_for_answers = names

    to = [in_reply_to_object.actor.ap_id]

    for name in names:
        vote_id = allocate_outbox_id()
        note = {
            "@context": ap.AS_EXTENDED_CTX,
            "type": "Note",
            "id": outbox_object_id(vote_id),
            "attributedTo": ID,
            "name": name,
            "to": to,
            "cc": [],
            "published": published,
            "context": context,
            "conversation": context,
            "url": outbox_object_id(vote_id),
            "inReplyTo": in_reply_to,
        }
        outbox_object = await save_outbox_object(
            db_session, vote_id, note, is_transient=True
        )
        if not outbox_object.id:
            raise ValueError("Should never happen")

        recipients = await _compute_recipients(db_session, note)
        for rcp in recipients:
            await new_outgoing_activity(db_session, rcp, outbox_object.id)

    # commit db session
    await db_session.commit()

    return vote_id


# Sentinel distinguishing "not passed, keep the existing value" from an
# explicit `None`/falsy override — send_update() is shared by the admin web UI
# (app/admin.py, only ever changes source/name) and the Mastodon API edit
# endpoint (app/mastodon/router.py, always resends the full edit form), so the
# default must mean "unchanged", not "cleared".
_UNSET = object()


async def send_update(
    db_session: AsyncSession,
    ap_id: str,
    source: str,
    name: str | None = None,
    content_warning: Any = _UNSET,
    is_sensitive: Any = _UNSET,
    uploads: Any = _UNSET,
    *,
    _commit: bool = True,
) -> str:
    outbox_object = await get_outbox_object_by_ap_id(db_session, ap_id)
    if not outbox_object:
        raise ValueError(f"{ap_id} not found")

    # Copy rather than mutate outbox_object.revisions in place: appending to
    # the same list object SQLAlchemy already holds as the column's current
    # value means old/new compare equal on flush (same identity), so the
    # reassignment below is silently skipped and every edit after the first
    # is dropped from history.
    revisions = list(outbox_object.revisions or [])
    revisions.append(
        {
            "ap_object": outbox_object.ap_object,
            "source": outbox_object.source,
            "updated": (
                outbox_object.ap_object.get("updated")
                or outbox_object.ap_object.get("published")
            ),
        }
    )

    updated = now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
    content, tags, mentioned_actors = await markdownify(db_session, source)

    resolved_content_warning = (
        outbox_object.summary if content_warning is _UNSET else content_warning
    )
    resolved_is_sensitive = (
        outbox_object.sensitive if is_sensitive is _UNSET else bool(is_sensitive)
    )

    if uploads is _UNSET:
        attachments = outbox_object.ap_object.get("attachment") or []
    else:
        await db_session.execute(
            delete(activitypub.models.OutboxObjectAttachment).where(
                activitypub.models.OutboxObjectAttachment.outbox_object_id
                == outbox_object.id
            )
        )
        attachments = []
        for upload, filename, alt_text in uploads or []:
            attachments.append(upload_to_attachment(upload, filename, alt_text))
            db_session.add(
                activitypub.models.OutboxObjectAttachment(
                    filename=filename,
                    alt=alt_text,
                    outbox_object_id=outbox_object.id,
                    upload_id=upload.id,
                )
            )

    # send_create() spreads extra keys (poll answers, language maps, and the
    # quote fields below) into the note it builds; this rebuilds the note as a
    # fresh literal dict, so anything not re-added here is silently dropped on
    # the first edit. Quote fields are re-added below -- the poll/language-map
    # fields have the same latent gap, left alone as out of scope here.
    tag_list = dedup_tags(tags)
    quote_wire_fields: dict[str, Any] = {}
    if outbox_object.quote_ap_id:
        tag_list = tag_list + [_quote_tag(outbox_object.quote_ap_id)]
        quote_wire_fields = _quote_wire_fields(outbox_object.quote_ap_id)
        if outbox_object.quote_authorization_ap_id:
            quote_wire_fields["quoteAuthorization"] = (
                outbox_object.quote_authorization_ap_id
            )
        content += _quote_reply_link_html(outbox_object.quote_ap_id)

    note = {
        "@context": ap.AS_EXTENDED_CTX,
        "type": outbox_object.ap_type,
        "id": outbox_object.ap_id,
        "attributedTo": ID,
        "content": content,
        "to": outbox_object.ap_object["to"],
        "cc": outbox_object.ap_object["cc"],
        "published": outbox_object.ap_object["published"],
        "context": outbox_object.ap_context,
        "conversation": outbox_object.ap_context,
        "url": outbox_object.url,
        "tag": tag_list,
        "summary": resolved_content_warning,
        "inReplyTo": outbox_object.in_reply_to,
        "sensitive": resolved_is_sensitive,
        "attachment": attachments,
        "updated": updated,
        **quote_wire_fields,  # type: ignore
        **_quote_interaction_policy(outbox_object.visibility),  # type: ignore
    }
    if outbox_object.ap_type == "Article" and name:
        note["name"] = name

    outbox_object.ap_object = note
    outbox_object.source = source
    outbox_object.revisions = revisions

    await db_session.execute(
        delete(activitypub.models.TaggedOutboxObject).where(
            activitypub.models.TaggedOutboxObject.outbox_object_id == outbox_object.id
        )
    )
    for tag in tags:
        if tag["type"] == "Hashtag":
            tagged_object = activitypub.models.TaggedOutboxObject(
                tag=tag["name"][1:].lower(),
                outbox_object_id=outbox_object.id,
            )
            db_session.add(tagged_object)

    recipients = await _compute_recipients(db_session, note)
    for rcp in recipients:
        await new_outgoing_activity(db_session, rcp, outbox_object.id)

    # If the note is public, check if we need to send any webmentions
    if outbox_object.visibility == ap.VisibilityEnum.PUBLIC:

        possible_targets = await opengraph.external_urls(db_session, outbox_object)
        logger.info(f"webmentions possible targert {possible_targets}")
        for target in possible_targets:
            webmention_endpoint = await webmentions.discover_webmention_endpoint(target)
            logger.info(f"{target=} {webmention_endpoint=}")
            if webmention_endpoint:
                await new_outgoing_activity(
                    db_session,
                    webmention_endpoint,
                    outbox_object_id=outbox_object.id,
                    webmention_target=target,
                )

    if _commit:
        await db_session.commit()
    return outbox_object.public_id  # type: ignore


async def _compute_recipients(
    db_session: AsyncSession, ap_object: ap.RawObject
) -> set[str]:
    _recipients = []
    for field in ["to", "cc", "bto", "bcc"]:
        if field in ap_object:
            _recipients.extend(ap.as_list(ap_object[field]))

    recipients = set()
    logger.info(f"{_recipients}")
    for r in _recipients:
        if r in [ap.AS_PUBLIC, ID]:
            continue

        # If we got a local collection, assume it's a collection of actors
        if r.startswith(BASE_URL):
            for actor in await fetch_actor_collection(db_session, r):
                recipients.add(actor.shared_inbox_url)

            continue

        # Is it a known actor?
        known_actor = (
            await db_session.execute(
                select(activitypub.models.Actor).where(
                    activitypub.models.Actor.ap_id == r
                )
            )
        ).scalar_one_or_none()  # type: ignore
        if known_actor:
            recipients.add(known_actor.shared_inbox_url)
            continue

        # Fetch the object
        raw_object = await ap.fetch(r)
        if raw_object.get("type") in ap.ACTOR_TYPES:
            saved_actor = await save_actor(db_session, raw_object)
            recipients.add(saved_actor.shared_inbox_url)
        else:
            # Assume it's a collection of actors
            for raw_actor in await ap.parse_collection(payload=raw_object):
                actor = RemoteActor(raw_actor)
                recipients.add(actor.shared_inbox_url)

    return recipients


async def compute_all_known_recipients(db_session: AsyncSession) -> set[str]:
    return {
        actor.shared_inbox_url or actor.inbox_url
        for actor in (
            await db_session.scalars(
                select(activitypub.models.Actor).where(
                    activitypub.models.Actor.is_deleted.is_(False)
                )
            )
        ).all()
    }


async def _get_following(
    db_session: AsyncSession,
) -> list[activitypub.models.Following]:
    return (
        (
            await db_session.scalars(
                select(activitypub.models.Following).options(
                    joinedload(activitypub.models.Following.actor)
                )
            )
        )
        .unique()
        .all()
    )


async def _get_followers(db_session: AsyncSession) -> list[activitypub.models.Follower]:
    return (
        (
            await db_session.scalars(
                select(activitypub.models.Follower).options(
                    joinedload(activitypub.models.Follower.actor)
                )
            )
        )
        .unique()
        .all()
    )


async def _get_followers_recipients(
    db_session: AsyncSession,
    skip_actors: list[activitypub.models.Actor] | None = None,
) -> set[str]:
    """Returns all the recipients from the local follower collection."""
    actor_ap_ids_to_skip = []
    if skip_actors:
        actor_ap_ids_to_skip = [actor.ap_id for actor in skip_actors]

    followers = await _get_followers(db_session)
    return {
        follower.actor.shared_inbox_url  # type: ignore
        for follower in followers
        if follower.actor.ap_id not in actor_ap_ids_to_skip
    }


async def get_notification_by_id(
    db_session: AsyncSession, notification_id: int
) -> models.Notification | None:
    return (
        await db_session.execute(
            select(models.Notification)
            .where(models.Notification.id == notification_id)
            .options(
                joinedload(models.Notification.inbox_object).options(
                    joinedload(activitypub.models.InboxObject.actor)
                ),
                joinedload(models.Notification.outbox_object),
            )
        )
    ).scalar_one_or_none()  # type: ignore


async def get_inbox_object_by_ap_id(
    db_session: AsyncSession, ap_id: str
) -> activitypub.models.InboxObject | None:
    return (
        await db_session.execute(
            select(activitypub.models.InboxObject)
            .where(activitypub.models.InboxObject.ap_id == ap_id)
            .options(
                joinedload(activitypub.models.InboxObject.actor),
                joinedload(activitypub.models.InboxObject.relates_to_inbox_object),
                joinedload(activitypub.models.InboxObject.relates_to_outbox_object),
            )
        )
    ).scalar_one_or_none()  # type: ignore


async def get_inbox_delete_for_activity_object_ap_id(
    db_session: AsyncSession, activity_object_ap_id: str
) -> activitypub.models.InboxObject | None:
    return (
        await db_session.execute(
            select(activitypub.models.InboxObject)
            .where(
                activitypub.models.InboxObject.ap_type == "Delete",
                activitypub.models.InboxObject.activity_object_ap_id
                == activity_object_ap_id,
            )
            .options(
                joinedload(activitypub.models.InboxObject.actor),
                joinedload(activitypub.models.InboxObject.relates_to_inbox_object),
                joinedload(activitypub.models.InboxObject.relates_to_outbox_object),
            )
        )
    ).scalar_one_or_none()  # type: ignore


async def get_outbox_object_by_ap_id(
    db_session: AsyncSession, ap_id: str
) -> activitypub.models.OutboxObject | None:
    return (
        (
            await db_session.execute(
                select(activitypub.models.OutboxObject)
                .where(activitypub.models.OutboxObject.ap_id == ap_id)
                .options(
                    joinedload(
                        activitypub.models.OutboxObject.outbox_object_attachments
                    ).options(
                        joinedload(activitypub.models.OutboxObjectAttachment.upload)
                    ),
                    joinedload(
                        activitypub.models.OutboxObject.relates_to_inbox_object
                    ).options(
                        joinedload(activitypub.models.InboxObject.actor),
                    ),
                    joinedload(
                        activitypub.models.OutboxObject.relates_to_outbox_object
                    ).options(
                        joinedload(
                            activitypub.models.OutboxObject.outbox_object_attachments
                        ).options(
                            joinedload(activitypub.models.OutboxObjectAttachment.upload)
                        ),
                    ),
                )
            )
        )
        .unique()
        .scalar_one_or_none()
    )  # type: ignore


async def get_outbox_object_by_slug_and_short_id(
    db_session: AsyncSession,
    slug: str,
    short_id: str,
) -> activitypub.models.OutboxObject | None:
    return (
        (
            await db_session.execute(
                select(activitypub.models.OutboxObject)
                .options(
                    joinedload(
                        activitypub.models.OutboxObject.outbox_object_attachments
                    ).options(
                        joinedload(activitypub.models.OutboxObjectAttachment.upload)
                    )
                )
                .where(
                    activitypub.models.OutboxObject.public_id.like(f"{short_id}%"),
                    activitypub.models.OutboxObject.slug == slug,
                    activitypub.models.OutboxObject.is_deleted.is_(False),
                )
            )
        )
        .unique()
        .scalar_one_or_none()
    )


async def get_anybox_object_by_ap_id(
    db_session: AsyncSession, ap_id: str
) -> AnyboxObject | None:
    if ap_id.startswith(BASE_URL):
        return await get_outbox_object_by_ap_id(db_session, ap_id)
    else:
        return await get_inbox_object_by_ap_id(db_session, ap_id)


async def get_quoted_object_for_display(
    db_session: AsyncSession, obj: AnyboxObject
) -> AnyboxObject | None:
    """The quoted object, only when the quote is authorized.

    An unverified, pending or rejected quote shows nothing beyond the `RE:`
    link already in the content, so there's nothing to fetch for those.
    """
    if not obj.quote_ap_id:
        return None

    is_authorized = (
        obj.quote_state == "accepted"
        if isinstance(obj, activitypub.models.OutboxObject)
        else bool(obj.quote_is_verified)
    )
    if not is_authorized:
        return None

    return await get_anybox_object_by_ap_id(db_session, obj.quote_ap_id)


async def get_inbox_objects_by_ap_ids(
    db_session: AsyncSession, ap_ids: Collection[str]
) -> list[activitypub.models.InboxObject]:
    """Batched `get_inbox_object_by_ap_id`.

    The eager-load options must stay in sync with the single-object getter:
    callers of the batched form expect the same relationships to be loaded,
    and under the async session a missed `joinedload` is not a slow lazy
    SELECT but a `MissingGreenlet` at attribute access.
    """
    if not ap_ids:
        return []
    return list(
        (
            await db_session.scalars(
                select(activitypub.models.InboxObject)
                .where(activitypub.models.InboxObject.ap_id.in_(ap_ids))
                .options(
                    joinedload(activitypub.models.InboxObject.actor),
                    joinedload(activitypub.models.InboxObject.relates_to_inbox_object),
                    joinedload(activitypub.models.InboxObject.relates_to_outbox_object),
                )
            )
        )
        .unique()
        .all()
    )


async def get_outbox_objects_by_ap_ids(
    db_session: AsyncSession, ap_ids: Collection[str]
) -> list[activitypub.models.OutboxObject]:
    """Batched `get_outbox_object_by_ap_id` — see the note there on options."""
    if not ap_ids:
        return []
    return list(
        (
            await db_session.scalars(
                select(activitypub.models.OutboxObject)
                .where(activitypub.models.OutboxObject.ap_id.in_(ap_ids))
                .options(
                    joinedload(
                        activitypub.models.OutboxObject.outbox_object_attachments
                    ).options(
                        joinedload(activitypub.models.OutboxObjectAttachment.upload)
                    ),
                    joinedload(
                        activitypub.models.OutboxObject.relates_to_inbox_object
                    ).options(
                        joinedload(activitypub.models.InboxObject.actor),
                    ),
                    joinedload(
                        activitypub.models.OutboxObject.relates_to_outbox_object
                    ).options(
                        joinedload(
                            activitypub.models.OutboxObject.outbox_object_attachments
                        ).options(
                            joinedload(activitypub.models.OutboxObjectAttachment.upload)
                        ),
                    ),
                )
            )
        )
        .unique()
        .all()
    )


async def get_anybox_objects_by_ap_ids(
    db_session: AsyncSession, ap_ids: Collection[str]
) -> list[AnyboxObject]:
    """Batched `get_anybox_object_by_ap_id`.

    Splits on the same `BASE_URL` rule as the single-object variant, so the
    inbox/outbox routing lives in exactly one place.
    """
    local = {ap_id for ap_id in ap_ids if ap_id.startswith(BASE_URL)}
    remote = {ap_id for ap_id in ap_ids if not ap_id.startswith(BASE_URL)}
    objects: list[AnyboxObject] = []
    objects.extend(await get_outbox_objects_by_ap_ids(db_session, local))
    objects.extend(await get_inbox_objects_by_ap_ids(db_session, remote))
    return objects


async def get_webmention_by_id(
    db_session: AsyncSession, webmention_id: int
) -> models.Webmention | None:
    return (
        await db_session.execute(
            select(models.Webmention)
            .where(models.Webmention.id == webmention_id)
            .options(
                joinedload(models.Webmention.outbox_object),
            )
        )
    ).scalar_one_or_none()  # type: ignore


async def _handle_delete_activity(
    db_session: AsyncSession,
    from_actor: activitypub.models.Actor,
    delete_activity: activitypub.models.InboxObject,
    relates_to_inbox_object: activitypub.models.InboxObject | None,
    forwarded_by_actor: activitypub.models.Actor | None,
) -> None:
    ap_object_to_delete: (
        activitypub.models.InboxObject | activitypub.models.Actor | None
    ) = None
    if relates_to_inbox_object:
        ap_object_to_delete = relates_to_inbox_object
    elif delete_activity.activity_object_ap_id:
        # If it's not a Delete for an inbox object, it may be related to
        # an actor
        try:
            ap_object_to_delete = await fetch_actor(
                db_session,
                delete_activity.activity_object_ap_id,
                save_if_not_found=False,
            )
        except ap.ObjectNotFoundError:
            pass

    if ap_object_to_delete is None or not ap_object_to_delete.is_from_db:
        if await _handle_quote_authorization_delete(
            db_session, from_actor, delete_activity
        ):
            return
        logger.info(
            "Received Delete for an unknown object "
            f"{delete_activity.activity_object_ap_id}"
        )
        return

    if isinstance(ap_object_to_delete, activitypub.models.InboxObject):
        if from_actor.ap_id != ap_object_to_delete.actor.ap_id:
            logger.warning(
                "Actor mismatch between the activity and the object: "
                f"{from_actor.ap_id}/{ap_object_to_delete.actor.ap_id}"
            )
            return

        logger.info(
            f"Deleting {ap_object_to_delete.ap_type}/{ap_object_to_delete.ap_id}"
        )
        await _revert_side_effect_for_deleted_object(
            db_session,
            delete_activity,
            ap_object_to_delete,
            forwarded_by_actor,
        )
        ap_object_to_delete.is_deleted = True
    elif isinstance(ap_object_to_delete, activitypub.models.Actor):
        if from_actor.ap_id != ap_object_to_delete.ap_id:
            logger.warning(
                "Actor mismatch between the activity and the object: "
                f"{from_actor.ap_id}/{ap_object_to_delete.ap_id}"
            )
            return

        logger.info(f"Deleting actor {ap_object_to_delete.ap_id}")
        follower = (
            await db_session.scalars(
                select(activitypub.models.Follower).where(
                    activitypub.models.Follower.ap_actor_id
                    == ap_object_to_delete.ap_id,
                )
            )
        ).one_or_none()
        if follower:
            logger.info("Removing actor from follower")
            await db_session.delete(follower)

            # Also mark Follow activities for this actor as deleted
            follow_activities = (
                await db_session.scalars(
                    select(activitypub.models.OutboxObject).where(
                        activitypub.models.OutboxObject.ap_type == "Follow",
                        activitypub.models.OutboxObject.relates_to_actor_id
                        == ap_object_to_delete.id,
                        activitypub.models.OutboxObject.is_deleted.is_(False),
                    )
                )
            ).all()
            for follow_activity in follow_activities:
                logger.info(
                    f"Marking Follow activity {follow_activity.ap_id} as deleted"
                )
                follow_activity.is_deleted = True

        following = (
            await db_session.scalars(
                select(activitypub.models.Following).where(
                    activitypub.models.Following.ap_actor_id
                    == ap_object_to_delete.ap_id,
                )
            )
        ).one_or_none()
        if following:
            logger.info("Removing actor from following")
            await db_session.delete(following)

        # Mark the actor as deleted
        ap_object_to_delete.is_deleted = True

        inbox_objects = (
            await db_session.scalars(
                select(activitypub.models.InboxObject).where(
                    activitypub.models.InboxObject.actor_id == ap_object_to_delete.id,
                    activitypub.models.InboxObject.is_deleted.is_(False),
                )
            )
        ).all()
        logger.info(f"Deleting {len(inbox_objects)} objects")
        for inbox_object in inbox_objects:
            await _revert_side_effect_for_deleted_object(
                db_session,
                delete_activity,
                inbox_object,
                forwarded_by_actor=None,
            )
            inbox_object.is_deleted = True
    else:
        raise ValueError("Should never happen")

    await db_session.flush()


async def _get_replies_count(
    db_session: AsyncSession,
    replied_object_ap_id: str,
) -> int:
    return (
        await db_session.scalar(
            select(func.count(activitypub.models.InboxObject.id)).where(
                activitypub.models.in_reply_to_expr(
                    activitypub.models.InboxObject.ap_object
                )
                == replied_object_ap_id,
                activitypub.models.InboxObject.is_deleted.is_(False),
            )
        )
    ) + (
        await db_session.scalar(
            select(func.count(activitypub.models.OutboxObject.id)).where(
                activitypub.models.in_reply_to_expr(
                    activitypub.models.OutboxObject.ap_object
                )
                == replied_object_ap_id,
                activitypub.models.OutboxObject.is_deleted.is_(False),
            )
        )
    )


async def _get_quotes_count(
    db_session: AsyncSession,
    quoted_object_ap_id: str,
) -> int:
    """Authorized remote quotes of one of our posts.

    Recomputed rather than incremented, like `_get_replies_count`: a counter
    that is only ever bumped drifts upward for good the first time a quoting
    post is deleted. Inbound-only by design (a self-quote is authorized but
    not a *remote* quote), which keeps this to a single lookup on
    `ix_inbox_quote_ap_id`.
    """
    return await db_session.scalar(
        select(func.count(activitypub.models.InboxObject.id)).where(
            activitypub.models.InboxObject.quote_ap_id == quoted_object_ap_id,
            activitypub.models.InboxObject.quote_is_verified.is_(True),
            activitypub.models.InboxObject.is_deleted.is_(False),
        )
    )


async def _get_outbox_replies_count(
    db_session: AsyncSession,
    outbox_object: activitypub.models.OutboxObject,
) -> int:
    return (await _get_replies_count(db_session, outbox_object.ap_id)) + (
        await db_session.scalar(
            select(func.count(models.Webmention.id)).where(
                models.Webmention.is_deleted.is_(False),
                models.Webmention.outbox_object_id == outbox_object.id,
                models.Webmention.webmention_type == models.WebmentionType.REPLY,
            )
        )
    )


async def _get_outbox_likes_count(
    db_session: AsyncSession,
    outbox_object: activitypub.models.OutboxObject,
) -> int:
    return (
        await db_session.scalar(
            select(func.count(activitypub.models.InboxObject.id)).where(
                activitypub.models.InboxObject.ap_type == "Like",
                activitypub.models.InboxObject.relates_to_outbox_object_id
                == outbox_object.id,
                activitypub.models.InboxObject.is_deleted.is_(False),
            )
        )
    ) + (
        await db_session.scalar(
            select(func.count(models.Webmention.id)).where(
                models.Webmention.is_deleted.is_(False),
                models.Webmention.outbox_object_id == outbox_object.id,
                models.Webmention.webmention_type == models.WebmentionType.LIKE,
            )
        )
    )


async def _get_outbox_announces_count(
    db_session: AsyncSession,
    outbox_object: activitypub.models.OutboxObject,
) -> int:
    return (
        await db_session.scalar(
            select(func.count(activitypub.models.InboxObject.id)).where(
                activitypub.models.InboxObject.ap_type == "Announce",
                activitypub.models.InboxObject.relates_to_outbox_object_id
                == outbox_object.id,
                activitypub.models.InboxObject.is_deleted.is_(False),
            )
        )
    ) + (
        await db_session.scalar(
            select(func.count(models.Webmention.id)).where(
                models.Webmention.is_deleted.is_(False),
                models.Webmention.outbox_object_id == outbox_object.id,
                models.Webmention.webmention_type == models.WebmentionType.REPOST,
            )
        )
    )


# Number of direct replies inlined in an object's `replies` collection.
# Mastodon reads at most 5 entries of a discovered status' collection to resolve
# the thread around it; 20 leaves some headroom without turning the object into
# a page dump. The collection is unpaginated, like `/featured`.
REPLIES_COLLECTION_LIMIT = 20

# The object types that carry the interaction collections. Activities served
# from the outbox (`Announce`…) must not get them.
_OBJECT_TYPES_WITH_COLLECTIONS = ["Note", "Article", "Question"]


async def fetch_direct_replies_ap_ids(
    db_session: AsyncSession,
    outbox_object: activitypub.models.OutboxObject,
    limit: int = REPLIES_COLLECTION_LIMIT,
) -> list[str]:
    """AP IDs of the public direct replies to a local object.

    Both boxes are queried: a remote server resolving a thread around a post it
    just discovered wants the replies that live on other instances too, not only
    the local self-replies.

    `inReplyTo` has no column, so this matches on the JSON the same way
    `_get_replies_count` does — which also means it only sees the string form of
    `inReplyTo` (what Mastodon and friends send).
    """
    replies: list[AnyboxObject] = []
    allowed_visibility = [ap.VisibilityEnum.PUBLIC, ap.VisibilityEnum.UNLISTED]

    replies.extend(
        (
            await db_session.scalars(
                select(activitypub.models.InboxObject)
                .where(
                    activitypub.models.in_reply_to_expr(
                        activitypub.models.InboxObject.ap_object
                    )
                    == outbox_object.ap_id,
                    activitypub.models.InboxObject.ap_type.in_(
                        ["Note", "Page", "Article", "Question"]
                    ),
                    activitypub.models.InboxObject.is_deleted.is_(False),
                    activitypub.models.InboxObject.visibility.in_(allowed_visibility),
                )
                .order_by(activitypub.models.InboxObject.ap_published_at.asc())
                .limit(limit)
            )
        ).all()
    )

    replies.extend(
        (
            await db_session.scalars(
                select(activitypub.models.OutboxObject)
                .where(
                    activitypub.models.in_reply_to_expr(
                        activitypub.models.OutboxObject.ap_object
                    )
                    == outbox_object.ap_id,
                    activitypub.models.OutboxObject.ap_type.in_(
                        ["Note", "Page", "Article", "Question"]
                    ),
                    activitypub.models.OutboxObject.is_deleted.is_(False),
                    activitypub.models.OutboxObject.visibility.in_(allowed_visibility),
                )
                .order_by(activitypub.models.OutboxObject.ap_published_at.asc())
                .limit(limit)
            )
        ).all()
    )

    replies.sort(key=lambda reply: reply.ap_published_at)  # type: ignore

    # Poll votes are replies carrying a bare `name` and no content; they are not
    # part of the thread.
    return [reply.ap_id for reply in replies if reply.content][:limit]


def with_interaction_collections(
    outbox_object: activitypub.models.OutboxObject,
    replies_ap_ids: list[str] | None = None,
) -> ap.RawObject:
    """The object as served over AP, with the `replies`/`likes`/`shares`
    collections a remote server expects on a status.

    Computed at serve time rather than stored in `ap_object`: the counts move
    with every interaction, and `send_update` rebuilds the note from scratch on
    every edit. Mastodon reads the `replies` collection of a status it
    cold-discovers (via a boost, a search or a link) specifically to resolve the
    surrounding thread, so this is what makes that possible.

    `replies_ap_ids` is `None` for collection listings, where inlining the
    replies of every item would cost a query per item: the collection is then
    only advertised (id + `totalItems`), and a remote server that cares
    dereferences it.

    `totalItems` comes from the denormalized counters, i.e. the same numbers the
    client API reports — which include the webmention likes/reposts/replies that
    have no AP identity to list.
    """
    if outbox_object.ap_type not in _OBJECT_TYPES_WITH_COLLECTIONS:
        return outbox_object.ap_object

    ap_id = outbox_object.ap_id
    replies: ap.RawObject = {
        "id": f"{ap_id}/replies",
        "type": "Collection",
        "totalItems": outbox_object.replies_count,
    }
    if replies_ap_ids is not None:
        replies["items"] = replies_ap_ids

    return {
        **outbox_object.ap_object,
        "replies": replies,
        # Counts only, no items: matches what Mastodon serves, and keeps the
        # facepiles out of a machine-readable collection.
        "likes": {
            "id": f"{ap_id}/likes",
            "type": "Collection",
            "totalItems": outbox_object.likes_count,
        },
        "shares": {
            "id": f"{ap_id}/shares",
            "type": "Collection",
            "totalItems": outbox_object.announces_count,
        },
    }


async def fetch_ap_object_with_collections(
    db_session: AsyncSession,
    outbox_object: activitypub.models.OutboxObject,
) -> ap.RawObject:
    """`with_interaction_collections` with the `replies` items inlined."""
    if outbox_object.ap_type not in _OBJECT_TYPES_WITH_COLLECTIONS:
        return outbox_object.ap_object

    return with_interaction_collections(
        outbox_object,
        replies_ap_ids=await fetch_direct_replies_ap_ids(db_session, outbox_object),
    )


async def _revert_side_effect_for_deleted_object(
    db_session: AsyncSession,
    delete_activity: activitypub.models.InboxObject | None,
    deleted_ap_object: activitypub.models.InboxObject,
    forwarded_by_actor: activitypub.models.Actor | None,
) -> None:
    is_delete_needs_to_be_forwarded = False

    # Delete related notifications
    notif_deletion_result = await db_session.execute(
        delete(models.Notification)
        .where(models.Notification.inbox_object_id == deleted_ap_object.id)
        .execution_options(synchronize_session=False)
    )
    logger.info(
        f"Deleted {notif_deletion_result.rowcount} notifications"  # type: ignore
    )

    # Decrement/refresh the replies counter if needed
    if deleted_ap_object.in_reply_to:
        replied_object = await get_anybox_object_by_ap_id(
            db_session,
            deleted_ap_object.in_reply_to,
        )
        if replied_object:
            if replied_object.is_from_outbox:
                # It's a local reply that was likely forwarded, the Delete
                # also needs to be forwarded
                is_delete_needs_to_be_forwarded = True

                new_replies_count = await _get_outbox_replies_count(
                    db_session, replied_object  # type: ignore
                )

                await db_session.execute(
                    update(activitypub.models.OutboxObject)
                    .where(
                        activitypub.models.OutboxObject.id == replied_object.id,
                    )
                    .values(replies_count=new_replies_count - 1)
                )
            else:
                new_replies_count = await _get_replies_count(
                    db_session, replied_object.ap_id
                )

                await db_session.execute(
                    update(activitypub.models.InboxObject)
                    .where(
                        activitypub.models.InboxObject.id == replied_object.id,
                    )
                    .values(replies_count=new_replies_count - 1)
                )

    # Same for the quotes counter. `is_deleted` is set by the caller *after*
    # this runs, so the deleted quote is still counted here -- hence the -1,
    # matching the replies/likes/announces blocks above.
    if deleted_ap_object.quote_ap_id and deleted_ap_object.quote_is_verified:
        quoted_object = await get_outbox_object_by_ap_id(
            db_session,
            deleted_ap_object.quote_ap_id,
        )
        if quoted_object:
            quotes_count = await _get_quotes_count(
                db_session, deleted_ap_object.quote_ap_id
            )
            await db_session.execute(
                update(activitypub.models.OutboxObject)
                .where(
                    activitypub.models.OutboxObject.id == quoted_object.id,
                )
                .values(quotes_count=quotes_count - 1)
            )

    if deleted_ap_object.ap_type == "Like" and deleted_ap_object.activity_object_ap_id:
        related_object = await get_outbox_object_by_ap_id(
            db_session,
            deleted_ap_object.activity_object_ap_id,
        )
        if related_object:
            if related_object.is_from_outbox:
                likes_count = await _get_outbox_likes_count(db_session, related_object)
                await db_session.execute(
                    update(activitypub.models.OutboxObject)
                    .where(
                        activitypub.models.OutboxObject.id == related_object.id,
                    )
                    .values(likes_count=likes_count - 1)
                )
    elif (
        deleted_ap_object.ap_type == "Announce"
        and deleted_ap_object.activity_object_ap_id
    ):
        related_object = await get_outbox_object_by_ap_id(
            db_session,
            deleted_ap_object.activity_object_ap_id,
        )
        if related_object:
            if related_object.is_from_outbox:
                announces_count = await _get_outbox_announces_count(
                    db_session, related_object
                )
                await db_session.execute(
                    update(activitypub.models.OutboxObject)
                    .where(
                        activitypub.models.OutboxObject.id == related_object.id,
                    )
                    .values(announces_count=announces_count - 1)
                )

    # Delete any Like/Announce
    await db_session.execute(
        update(activitypub.models.OutboxObject)
        .where(
            activitypub.models.OutboxObject.activity_object_ap_id
            == deleted_ap_object.ap_id,
        )
        .values(is_deleted=True)
    )

    # If it's a local replies, it was forwarded, so we also need to forward
    # the Delete activity if possible
    if (
        delete_activity
        and delete_activity.activity_object_ap_id == deleted_ap_object.ap_id
        and delete_activity.has_ld_signature
        and is_delete_needs_to_be_forwarded
    ):
        logger.info("Forwarding Delete activity as it's a local reply")

        # Don't forward to the forwarding actor and the original Delete actor
        skip_actors = [delete_activity.actor]
        if forwarded_by_actor:
            skip_actors.append(forwarded_by_actor)
        recipients = await _get_followers_recipients(
            db_session,
            skip_actors=skip_actors,
        )
        for rcp in recipients:
            await new_outgoing_activity(
                db_session,
                rcp,
                outbox_object_id=None,
                inbox_object_id=delete_activity.id,
            )


async def _handle_follow_follow_activity(
    db_session: AsyncSession,
    from_actor: activitypub.models.Actor,
    follow_activity: activitypub.models.InboxObject,
) -> None:
    if follow_activity.activity_object_ap_id != LOCAL_ACTOR.ap_id:
        logger.warning(
            f"Dropping Follow activity for {follow_activity.activity_object_ap_id}"
        )
        await db_session.delete(follow_activity)
        return

    if MANUALLY_APPROVES_FOLLOWERS:
        notif = models.Notification(
            notification_type=models.NotificationType.PENDING_INCOMING_FOLLOWER,
            actor_id=from_actor.id,
            inbox_object_id=follow_activity.id,
        )
        db_session.add(notif)
        return None

    await _send_accept(db_session, from_actor, follow_activity)


async def _get_incoming_follow_from_notification_id(
    db_session: AsyncSession,
    notification_id: int,
) -> tuple[models.Notification, activitypub.models.InboxObject]:
    notif = await get_notification_by_id(db_session, notification_id)
    if notif is None:
        raise ValueError(f"Notification {notification_id=} not found")

    if notif.inbox_object is None:
        raise ValueError("Should never happen")

    if ap_type := notif.inbox_object.ap_type != "Follow":
        raise ValueError(f"Unexpected {ap_type=}")

    return notif, notif.inbox_object


async def send_accept(
    db_session: AsyncSession,
    notification_id: int,
) -> None:
    notif, incoming_follow_request = await _get_incoming_follow_from_notification_id(
        db_session, notification_id
    )

    await _send_accept(
        db_session, incoming_follow_request.actor, incoming_follow_request
    )
    notif.is_accepted = True

    await db_session.commit()


async def _send_accept(
    db_session: AsyncSession,
    from_actor: activitypub.models.Actor,
    inbox_object: activitypub.models.InboxObject,
) -> None:

    follower = activitypub.models.Follower(
        actor_id=from_actor.id,
        inbox_object_id=inbox_object.id,
        ap_actor_id=from_actor.ap_id,
    )
    try:
        db_session.add(follower)
        await db_session.flush()
    except IntegrityError:
        pass  # TODO update the existing followe

    # Reply with an Accept
    reply_id = allocate_outbox_id()
    reply = {
        "@context": ap.AS_CTX,
        "id": outbox_object_id(reply_id),
        "type": "Accept",
        "actor": ID,
        "object": inbox_object.ap_id,
    }
    outbox_activity = await save_outbox_object(
        db_session, reply_id, reply, relates_to_inbox_object_id=inbox_object.id
    )
    if not outbox_activity.id:
        raise ValueError("Should never happen")
    await new_outgoing_activity(db_session, from_actor.inbox_url, outbox_activity.id)

    if is_notification_enabled(models.NotificationType.NEW_FOLLOWER):
        notif = models.Notification(
            notification_type=models.NotificationType.NEW_FOLLOWER,
            actor_id=from_actor.id,
        )
        db_session.add(notif)


async def send_reject(
    db_session: AsyncSession,
    notification_id: int,
) -> None:
    notif, incoming_follow_request = await _get_incoming_follow_from_notification_id(
        db_session, notification_id
    )

    await _send_reject(
        db_session, incoming_follow_request.actor, incoming_follow_request
    )
    notif.is_rejected = True
    await db_session.commit()


async def _send_reject(
    db_session: AsyncSession,
    from_actor: activitypub.models.Actor,
    inbox_object: activitypub.models.InboxObject,
) -> None:
    # Reply with an Accept
    reply_id = allocate_outbox_id()
    reply = {
        "@context": ap.AS_CTX,
        "id": outbox_object_id(reply_id),
        "type": "Reject",
        "actor": ID,
        "object": inbox_object.ap_id,
    }
    outbox_activity = await save_outbox_object(
        db_session, reply_id, reply, relates_to_inbox_object_id=inbox_object.id
    )
    if not outbox_activity.id:
        raise ValueError("Should never happen")
    await new_outgoing_activity(db_session, from_actor.inbox_url, outbox_activity.id)

    if is_notification_enabled(models.NotificationType.REJECTED_FOLLOWER):
        notif = models.Notification(
            notification_type=models.NotificationType.REJECTED_FOLLOWER,
            actor_id=from_actor.id,
        )
        db_session.add(notif)


async def _handle_quote_authorization_delete(
    db_session: AsyncSession,
    from_actor: activitypub.models.Actor,
    delete_activity: activitypub.models.InboxObject,
) -> bool:
    """FEP-044f quote revocation: a `Delete` naming a `QuoteAuthorization`
    stamp. Never stored in our inbox/outbox by id (the stamp is verified
    by-value and discarded), so it always falls through the normal Delete
    resolution -- this is that fallback. Returns True when the Delete was
    consumed as a revocation, so the caller doesn't log it as unknown.
    """
    stamp_ap_id = delete_activity.activity_object_ap_id
    if not stamp_ap_id or stamp_ap_id.startswith(BASE_URL):
        # A remote can only revoke a stamp *it* minted; a local stamp is only
        # ever revoked by the owner, via `send_quote_revoke`.
        return False

    # Case 1: one of our own posts quotes theirs, and they're revoking the
    # grant. The sender must be the quoted post's own author -- resolved
    # independently, exactly as `_handle_quote_request_accept_or_reject`
    # does -- otherwise any third party could revoke someone else's stamp.
    our_quote = await db_session.scalar(
        select(activitypub.models.OutboxObject).where(
            activitypub.models.OutboxObject.quote_authorization_ap_id == stamp_ap_id,
            activitypub.models.OutboxObject.quote_state == "accepted",
            activitypub.models.OutboxObject.is_deleted.is_(False),
        )
    )
    if our_quote is not None and our_quote.quote_ap_id:
        quoted_object = await get_anybox_object_by_ap_id(
            db_session, our_quote.quote_ap_id
        )
        quoted_actor_ap_id = quoted_object.ap_actor_id if quoted_object else None
        if quoted_actor_ap_id and from_actor.ap_id == quoted_actor_ap_id:
            our_quote.quote_state = "revoked"
            logger.info(
                f"Quote authorization {stamp_ap_id} for {our_quote.ap_id} "
                "was revoked"
            )
            return True
        logger.warning(
            f"Ignoring Delete of {stamp_ap_id} from {from_actor.ap_id}, "
            f"which is not the quoted post's author ({quoted_actor_ap_id})"
        )
        return True

    # Case 2: a remote post quotes another remote post, and the quoted
    # actor is revoking the stamp we verified when we received the quote.
    # Not counted in `_get_quotes_count`: `_verify_quote_authorization`
    # requires the stamp's host to match the quoted author's, and a stamp
    # for one of *our* posts can therefore only ever be local (excluded
    # above) -- so no `quotes_count` recompute belongs on this branch.
    their_quote = await db_session.scalar(
        select(activitypub.models.InboxObject).where(
            activitypub.models.InboxObject.quote_authorization_ap_id == stamp_ap_id,
            activitypub.models.InboxObject.quote_is_verified.is_(True),
        )
    )
    if their_quote is not None and their_quote.quote_ap_id:
        quoted_object = await get_anybox_object_by_ap_id(
            db_session, their_quote.quote_ap_id
        )
        quoted_actor_ap_id = quoted_object.ap_actor_id if quoted_object else None
        if quoted_actor_ap_id and from_actor.ap_id == quoted_actor_ap_id:
            their_quote.quote_is_verified = False
            logger.info(
                f"Quote authorization {stamp_ap_id} for {their_quote.ap_id} "
                "was revoked"
            )
            return True
        logger.warning(
            f"Ignoring Delete of {stamp_ap_id} from {from_actor.ap_id}, "
            f"which is not the quoted post's author ({quoted_actor_ap_id})"
        )
        return True

    return False


async def _verify_quote_authorization(
    db_session: AsyncSession,
    quote_authorization_ap_id: str,
    quoting_object_ap_id: str,
    quoted_object_ap_id: str,
    quoted_actor_ap_id: str,
) -> bool:
    """FEP-044f stamp verification: dereference it -- or read it straight
    from our own outbox when we're the one who minted it -- and check every
    field matches what it's being presented for, plus that its own host
    matches the quoted author's: a stamp minted by anyone else is worthless.
    """
    if quote_authorization_ap_id.startswith(BASE_URL):
        stamp_object = await get_outbox_object_by_ap_id(
            db_session, quote_authorization_ap_id
        )
        if not stamp_object:
            return False
        stamp = stamp_object.ap_object
    else:
        try:
            # Bounded explicitly: this runs with the inbox write transaction
            # open (see `_process_inbound_quote`), and `ap.fetch` passes no
            # timeout of its own.
            stamp = await asyncio.wait_for(
                ap.fetch(quote_authorization_ap_id),
                timeout=_QUOTE_FETCH_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.exception(f"Failed to fetch stamp {quote_authorization_ap_id}")
            return False

    try:
        if ap.as_list(stamp.get("type"))[0] != "QuoteAuthorization":
            return False
        if (
            "attributedTo" not in stamp
            or ap.get_id(stamp["attributedTo"]) != quoted_actor_ap_id
        ):
            return False
        if (
            "interactingObject" not in stamp
            or ap.get_id(stamp["interactingObject"]) != quoting_object_ap_id
        ):
            return False
        if (
            "interactionTarget" not in stamp
            or ap.get_id(stamp["interactionTarget"]) != quoted_object_ap_id
        ):
            return False
    except (ValueError, IndexError):
        return False

    return (
        urlparse(quote_authorization_ap_id).hostname
        == urlparse(quoted_actor_ap_id).hostname
    )


async def _accept_quote_request(
    db_session: AsyncSession,
    from_actor: activitypub.models.Actor,
    quote_request_activity: activitypub.models.InboxObject,
    quoted_object: activitypub.models.OutboxObject,
    quoting_ap_id: str,
) -> None:
    stamp_object = await _mint_quote_authorization(
        db_session,
        quoting_object_ap_id=quoting_ap_id,
        quoted_object_ap_id=quoted_object.ap_id,
        relates_to_inbox_object_id=quote_request_activity.id,
    )

    reply_id = allocate_outbox_id()
    reply = {
        "@context": ap.AS_EXTENDED_CTX,
        "id": outbox_object_id(reply_id),
        "type": "Accept",
        "actor": ID,
        "object": quote_request_activity.ap_id,
        "result": stamp_object.ap_id,
    }
    outbox_activity = await save_outbox_object(
        db_session,
        reply_id,
        reply,
        relates_to_inbox_object_id=quote_request_activity.id,
    )
    if not outbox_activity.id:
        raise ValueError("Should never happen")

    await new_outgoing_activity(db_session, from_actor.inbox_url, outbox_activity.id)


async def _reject_quote_request(
    db_session: AsyncSession,
    from_actor: activitypub.models.Actor,
    quote_request_activity: activitypub.models.InboxObject,
) -> None:
    reply_id = allocate_outbox_id()
    reply = {
        "@context": ap.AS_CTX,
        "id": outbox_object_id(reply_id),
        "type": "Reject",
        "actor": ID,
        "object": quote_request_activity.ap_id,
    }
    outbox_activity = await save_outbox_object(
        db_session,
        reply_id,
        reply,
        relates_to_inbox_object_id=quote_request_activity.id,
    )
    if not outbox_activity.id:
        raise ValueError("Should never happen")

    await new_outgoing_activity(db_session, from_actor.inbox_url, outbox_activity.id)


async def _handle_quote_request_activity(
    db_session: AsyncSession,
    from_actor: activitypub.models.Actor,
    quote_request_activity: activitypub.models.InboxObject,
) -> None:
    """A remote actor asks to quote one of the owner's posts (FEP-044f)."""
    quoted_ap_id = quote_request_activity.activity_object_ap_id
    if not quoted_ap_id or not quoted_ap_id.startswith(BASE_URL):
        logger.warning(f"Received a QuoteRequest for a foreign object {quoted_ap_id=}")
        await db_session.delete(quote_request_activity)
        return

    quoted_object = await get_outbox_object_by_ap_id(db_session, quoted_ap_id)
    if not quoted_object or quoted_object.is_deleted:
        logger.warning(f"Received a QuoteRequest for an unknown object {quoted_ap_id}")
        await db_session.delete(quote_request_activity)
        return

    instrument = quote_request_activity.ap_object.get("instrument")
    quoting_ap_id = ap.get_id(instrument) if instrument else None
    if not quoting_ap_id:
        logger.warning(
            f"Received a QuoteRequest with no instrument: {quote_request_activity.ap_id}"
        )
        await db_session.delete(quote_request_activity)
        return

    policy = config.CONFIG.quote_policy
    is_public_target = quoted_object.visibility in (
        ap.VisibilityEnum.PUBLIC,
        ap.VisibilityEnum.UNLISTED,
    )

    if policy == "nobody" or not is_public_target:
        await _reject_quote_request(db_session, from_actor, quote_request_activity)
        return

    if policy == "public":
        await _accept_quote_request(
            db_session,
            from_actor,
            quote_request_activity,
            quoted_object,
            quoting_ap_id,
        )
        return

    if policy == "followers":
        is_follower = (
            await db_session.scalars(
                select(activitypub.models.Follower).where(
                    activitypub.models.Follower.actor_id == from_actor.id
                )
            )
        ).one_or_none()
        if is_follower:
            await _accept_quote_request(
                db_session,
                from_actor,
                quote_request_activity,
                quoted_object,
                quoting_ap_id,
            )
        else:
            await _reject_quote_request(db_session, from_actor, quote_request_activity)
        return

    # policy == "manual": surface a notification and let the owner decide,
    # like a follow request (`send_quote_accept`/`send_quote_reject`).
    if is_notification_enabled(models.NotificationType.PENDING_INCOMING_QUOTE_REQUEST):
        notif = models.Notification(
            notification_type=models.NotificationType.PENDING_INCOMING_QUOTE_REQUEST,
            actor_id=from_actor.id,
            inbox_object_id=quote_request_activity.id,
            outbox_object_id=quoted_object.id,
        )
        db_session.add(notif)


async def _get_incoming_quote_request_from_notification_id(
    db_session: AsyncSession,
    notification_id: int,
) -> tuple[
    models.Notification,
    activitypub.models.InboxObject,
    activitypub.models.OutboxObject,
]:
    notif = await get_notification_by_id(db_session, notification_id)
    if notif is None:
        raise ValueError(f"Notification {notification_id=} not found")

    if notif.inbox_object is None or notif.outbox_object is None:
        raise ValueError("Should never happen")

    if (ap_type := notif.inbox_object.ap_type) != "QuoteRequest":
        raise ValueError(f"Unexpected {ap_type=}")

    return notif, notif.inbox_object, notif.outbox_object


async def send_quote_accept(db_session: AsyncSession, notification_id: int) -> None:
    (
        notif,
        quote_request_activity,
        quoted_object,
    ) = await _get_incoming_quote_request_from_notification_id(
        db_session, notification_id
    )

    instrument = quote_request_activity.ap_object.get("instrument")
    quoting_ap_id = ap.get_id(instrument) if instrument else None
    if not quoting_ap_id:
        raise ValueError("Should never happen")

    await _accept_quote_request(
        db_session,
        quote_request_activity.actor,
        quote_request_activity,
        quoted_object,
        quoting_ap_id,
    )
    notif.is_accepted = True

    await db_session.commit()


async def send_quote_reject(db_session: AsyncSession, notification_id: int) -> None:
    (
        notif,
        quote_request_activity,
        _quoted_object,
    ) = await _get_incoming_quote_request_from_notification_id(
        db_session, notification_id
    )

    await _reject_quote_request(
        db_session, quote_request_activity.actor, quote_request_activity
    )
    notif.is_rejected = True

    await db_session.commit()


async def send_quote_revoke(db_session: AsyncSession, ap_object_id: str) -> None:
    """Revoke a `QuoteAuthorization` stamp this instance minted, identified
    by the ap_id of the quoting post (an inbox object) it authorized.
    """
    inbox_object = await get_inbox_object_by_ap_id(db_session, ap_object_id)
    if not inbox_object:
        raise ValueError(f"{ap_object_id} not found in the inbox")

    stamp_ap_id = inbox_object.quote_authorization_ap_id
    if (
        not inbox_object.quote_is_verified
        or not stamp_ap_id
        or not stamp_ap_id.startswith(BASE_URL)
    ):
        raise ValueError(f"{ap_object_id} has no local quote authorization to revoke")

    stamp_object = await get_outbox_object_by_ap_id(db_session, stamp_ap_id)
    if (
        not stamp_object
        or stamp_object.ap_type != "QuoteAuthorization"
        or stamp_object.is_deleted
    ):
        raise ValueError(f"{stamp_ap_id} not found in the outbox")

    delete_id = allocate_outbox_id()
    # Explicit public addressing, unlike `send_delete`: the stamp itself
    # carries no `to`/`cc`, and `_build_delivery_request` only LD-signs a
    # Delete at PUBLIC visibility. Without that, the quoting server would
    # receive an unsigned Delete it cannot forward to the quote's own
    # audience -- which is exactly what FEP-044f relies on for a revocation
    # to reach beyond a single hop. `Delete` sits outside both the homepage
    # and public-outbox type allowlists, so this doesn't expose the stamp
    # anywhere new.
    delete = {
        "@context": ap.AS_EXTENDED_CTX,
        "id": outbox_object_id(delete_id),
        "type": "Delete",
        "actor": ID,
        "to": [ap.AS_PUBLIC],
        "cc": [inbox_object.actor.ap_id],
        "object": {"type": "Tombstone", "id": stamp_object.ap_id},
    }
    outbox_object = await save_outbox_object(
        db_session,
        delete_id,
        delete,
        relates_to_outbox_object_id=stamp_object.id,
    )
    if not outbox_object.id:
        raise ValueError("Should never happen")

    stamp_object.is_deleted = True
    inbox_object.quote_is_verified = False
    await db_session.flush()

    if inbox_object.quote_ap_id:
        quoted_object = await get_outbox_object_by_ap_id(
            db_session, inbox_object.quote_ap_id
        )
        if quoted_object:
            quoted_object.quotes_count = await _get_quotes_count(
                db_session, inbox_object.quote_ap_id
            )

    await new_outgoing_activity(
        db_session, inbox_object.actor.inbox_url, outbox_object.id
    )

    await db_session.commit()


async def _handle_quote_request_accept_or_reject(
    db_session: AsyncSession,
    accept_or_reject_activity: activitypub.models.InboxObject,
    quote_request_outbox_object: activitypub.models.OutboxObject,
) -> None:
    """Our own `QuoteRequest` got an `Accept` (carrying the stamp as
    `result`) or a `Reject` back.
    """
    quote_outbox_object = quote_request_outbox_object.relates_to_outbox_object
    if not quote_outbox_object:
        logger.warning(
            f"QuoteRequest {quote_request_outbox_object.ap_id} has no related "
            "quote post"
        )
        return

    if quote_outbox_object.quote_state != "pending":
        logger.info(
            f"Quote {quote_outbox_object.ap_id} is already "
            f"{quote_outbox_object.quote_state}, ignoring"
        )
        return

    quoted_ap_id = quote_outbox_object.quote_ap_id
    if not quoted_ap_id:
        raise ValueError("Should never happen")

    # The Accept/Reject must come from the quoted post's own author -- not
    # just from whoever sent an activity naming our QuoteRequest. Otherwise
    # any third party could self-issue a stamp (attributedTo themselves) and
    # have it verify, since _verify_quote_authorization only checks that the
    # stamp's attributedTo/host match whatever `quoted_actor_ap_id` it's
    # given. Resolving that independently from the quoted post itself (which
    # is already in our inbox/outbox from when the QuoteRequest was sent) is
    # what makes that check meaningful.
    quoted_object = await get_anybox_object_by_ap_id(db_session, quoted_ap_id)
    quoted_actor_ap_id = quoted_object.ap_actor_id if quoted_object else None
    if (
        not quoted_actor_ap_id
        or accept_or_reject_activity.actor.ap_id != quoted_actor_ap_id
    ):
        logger.warning(
            f"Ignoring {accept_or_reject_activity.ap_type} for QuoteRequest "
            f"{quote_request_outbox_object.ap_id} from "
            f"{accept_or_reject_activity.actor.ap_id}, which is not the "
            f"quoted post's author ({quoted_actor_ap_id})"
        )
        return

    if accept_or_reject_activity.ap_type == "Reject":
        quote_outbox_object.quote_state = "rejected"
        return

    result = accept_or_reject_activity.ap_object.get("result")
    if not result:
        logger.warning(
            "Accept for QuoteRequest "
            f"{quote_request_outbox_object.ap_id} has no result, treating as "
            "a Reject"
        )
        quote_outbox_object.quote_state = "rejected"
        return

    stamp_ap_id = ap.get_id(result)
    is_verified = await _verify_quote_authorization(
        db_session,
        quote_authorization_ap_id=stamp_ap_id,
        quoting_object_ap_id=quote_outbox_object.ap_id,
        quoted_object_ap_id=quoted_ap_id,
        quoted_actor_ap_id=quoted_actor_ap_id,
    )
    if not is_verified:
        logger.warning(f"Invalid QuoteAuthorization {stamp_ap_id}")
        quote_outbox_object.quote_state = "rejected"
        return

    quote_outbox_object.quote_authorization_ap_id = stamp_ap_id
    quote_outbox_object.quote_state = "accepted"

    # Let followers know the quote is now authorized. `_commit=False`: this
    # runs from inside `save_to_inbox`'s dispatch, which commits once at the
    # end -- internal helpers here don't commit on their own.
    if not quote_outbox_object.source:
        # `send_update` re-renders the note from its source, so an empty one
        # would blank the post we just got authorized. Fail loudly instead:
        # the worker rolls back and retries, leaving the quote pending.
        raise ValueError(f"{quote_outbox_object.ap_id} has no source")

    await send_update(
        db_session,
        quote_outbox_object.ap_id,
        quote_outbox_object.source,
        _commit=False,
    )


async def remove_follower(
    db_session: AsyncSession,
    actor: activitypub.models.Actor,
) -> bool:
    """Drop one of our followers (Mastodon's `remove_from_followers`).

    Rejecting the original Follow is how the remote server is told the
    relationship is over — the same activity as rejecting a pending follow
    request, just sent after it had been accepted. Returns False when the actor
    isn't a follower, which the API treats as a no-op rather than an error.
    """
    follower = (
        (
            await db_session.execute(
                select(activitypub.models.Follower)
                .where(activitypub.models.Follower.actor_id == actor.id)
                .options(joinedload(activitypub.models.Follower.inbox_object))
            )
        )
        .unique()
        .scalar_one_or_none()
    )
    if follower is None:
        return False

    await _send_reject(db_session, actor, follower.inbox_object)
    await db_session.delete(follower)
    await db_session.commit()
    return True


async def _handle_undo_activity(
    db_session: AsyncSession,
    from_actor: activitypub.models.Actor,
    undo_activity: activitypub.models.InboxObject,
    ap_activity_to_undo: activitypub.models.InboxObject,
) -> None:
    if from_actor.ap_id != ap_activity_to_undo.actor.ap_id:
        logger.warning(
            "Actor mismatch between the activity and the object: "
            f"{from_actor.ap_id}/{ap_activity_to_undo.actor.ap_id}"
        )
        return

    ap_activity_to_undo.undone_by_inbox_object_id = undo_activity.id
    ap_activity_to_undo.is_deleted = True

    if ap_activity_to_undo.ap_type == "Follow":
        logger.info(f"Undo follow from {from_actor.ap_id}")
        await db_session.execute(
            delete(activitypub.models.Follower).where(
                activitypub.models.Follower.inbox_object_id == ap_activity_to_undo.id
            )
        )
        if is_notification_enabled(models.NotificationType.UNFOLLOW):
            notif = models.Notification(
                notification_type=models.NotificationType.UNFOLLOW,
                actor_id=from_actor.id,
            )
            db_session.add(notif)

    elif ap_activity_to_undo.ap_type == "Like":
        if not ap_activity_to_undo.activity_object_ap_id:
            raise ValueError("Like without object")
        liked_obj = await get_outbox_object_by_ap_id(
            db_session,
            ap_activity_to_undo.activity_object_ap_id,
        )
        if not liked_obj:
            logger.warning(
                "Cannot find liked object: "
                f"{ap_activity_to_undo.activity_object_ap_id}"
            )
            return

        liked_obj.likes_count = (
            await _get_outbox_likes_count(
                db_session,
                liked_obj,
            )
            - 1
        )
        if is_notification_enabled(models.NotificationType.UNDO_LIKE):
            notif = models.Notification(
                notification_type=models.NotificationType.UNDO_LIKE,
                actor_id=from_actor.id,
                outbox_object_id=liked_obj.id,
                inbox_object_id=ap_activity_to_undo.id,
            )
            db_session.add(notif)

    elif ap_activity_to_undo.ap_type == "Announce":
        if not ap_activity_to_undo.activity_object_ap_id:
            raise ValueError("Announce witout object")
        announced_obj_ap_id = ap_activity_to_undo.activity_object_ap_id
        logger.info(
            f"Undo for announce {ap_activity_to_undo.ap_id}/{announced_obj_ap_id}"
        )
        if announced_obj_ap_id.startswith(BASE_URL):
            announced_obj_from_outbox = await get_outbox_object_by_ap_id(
                db_session, announced_obj_ap_id
            )
            if announced_obj_from_outbox:
                logger.info("Found in the oubox")
                announced_obj_from_outbox.announces_count = (
                    activitypub.models.OutboxObject.announces_count - 1
                )
                if is_notification_enabled(models.NotificationType.UNDO_ANNOUNCE):
                    notif = models.Notification(
                        notification_type=models.NotificationType.UNDO_ANNOUNCE,
                        actor_id=from_actor.id,
                        outbox_object_id=announced_obj_from_outbox.id,
                        inbox_object_id=ap_activity_to_undo.id,
                    )
                    db_session.add(notif)
    elif ap_activity_to_undo.ap_type == "Block":
        if is_notification_enabled(models.NotificationType.UNBLOCKED):
            notif = models.Notification(
                notification_type=models.NotificationType.UNBLOCKED,
                actor_id=from_actor.id,
                inbox_object_id=ap_activity_to_undo.id,
            )
            db_session.add(notif)
    else:
        logger.warning(f"Don't know how to undo {ap_activity_to_undo.ap_type} activity")

    # commit will be perfomed in save_to_inbox


async def _handle_move_activity(
    db_session: AsyncSession,
    from_actor: activitypub.models.Actor,
    move_activity: activitypub.models.InboxObject,
) -> None:
    logger.info("Processing Move activity")

    # Ensure the object matches the actor
    old_actor_id = ap.get_object_id(move_activity.ap_object)
    if old_actor_id != from_actor.ap_id:
        logger.warning(
            f"Object does not match the actor: {old_actor_id}/{from_actor.ap_id}"
        )
        return None

    # Fetch the target account
    target = move_activity.ap_object.get("target")
    if not target:
        logger.warning("Missing target")
        return None

    new_actor_id = ap.get_id(target)
    new_actor = await fetch_actor(db_session, new_actor_id)

    logger.info(f"Moving {old_actor_id} to {new_actor_id}")

    # Ensure the target account references the old account
    if old_actor_id not in (aks := new_actor.ap_actor.get("alsoKnownAs", [])):
        logger.warning(
            f"New account does not have have an alias for the old account: {aks}"
        )
        return None

    # Unfollow the old account
    following = (
        await db_session.execute(
            select(activitypub.models.Following)
            .where(activitypub.models.Following.ap_actor_id == old_actor_id)
            .options(joinedload(activitypub.models.Following.outbox_object))
        )
    ).scalar_one_or_none()
    if not following:
        logger.warning("Not following the Move actor")
        return

    await _send_undo(db_session, following.outbox_object.ap_id)

    # Follow the new one
    if not (
        await db_session.execute(
            select(activitypub.models.Following).where(
                activitypub.models.Following.ap_actor_id == new_actor_id
            )
        )
    ).scalar():
        await _send_follow(db_session, new_actor_id)
    else:
        logger.info(f"Already following target {new_actor_id}")

    if is_notification_enabled(models.NotificationType.MOVE):
        notif = models.Notification(
            notification_type=models.NotificationType.MOVE,
            actor_id=new_actor.id,
            inbox_object_id=move_activity.id,
        )
        db_session.add(notif)


async def _handle_update_activity(
    db_session: AsyncSession,
    from_actor: activitypub.models.Actor,
    update_activity: activitypub.models.InboxObject,
) -> None:
    logger.info("Processing Update activity")
    wrapped_object = await ap.get_object(update_activity.ap_object)
    if wrapped_object["type"] in ap.ACTOR_TYPES:
        logger.info("Updating actor")

        updated_actor = RemoteActor(wrapped_object)
        if (
            from_actor.ap_id != updated_actor.ap_id
            or ap.as_list(from_actor.ap_type)[0] not in ap.ACTOR_TYPES
            or ap.as_list(updated_actor.ap_type)[0] not in ap.ACTOR_TYPES
            or from_actor.handle != updated_actor.handle
        ):
            raise ValueError(
                f"Invalid Update activity {from_actor.ap_actor}/"
                f"{updated_actor.ap_actor}"
            )

        # Update the actor
        await update_actor_if_needed(db_session, from_actor, updated_actor)
    elif (ap_type := wrapped_object["type"]) in [
        "Question",
        "Note",
        "Article",
        "Page",
        "Video",
    ]:
        logger.info(f"Updating {ap_type}")
        existing_object = await get_inbox_object_by_ap_id(
            db_session, wrapped_object["id"]
        )
        if not existing_object:
            logger.info(f"{ap_type} not found in the inbox")
        elif existing_object.actor.ap_id != from_actor.ap_id:
            logger.warning(
                f"Update actor does not match the {ap_type} actor {from_actor.ap_id}"
                f"/{existing_object.actor.ap_id}"
            )
        else:
            # Everything looks correct, update the object in the inbox
            logger.info(f"Updating {existing_object.ap_id}")
            existing_object.ap_object = wrapped_object
            existing_object.updated_at = now()

            was_interacted_with = (
                existing_object.liked_via_outbox_object_ap_id is not None
                or existing_object.announced_via_outbox_object_ap_id is not None
            )
            if was_interacted_with and is_notification_enabled(
                models.NotificationType.UPDATE
            ):
                db_session.add(
                    models.Notification(
                        notification_type=models.NotificationType.UPDATE,
                        actor_id=from_actor.id,
                        inbox_object_id=existing_object.id,
                    )
                )
    else:
        # TODO(ts): support updating objects
        logger.info(f'Cannot update {wrapped_object["type"]}')


async def _handle_create_activity(
    db_session: AsyncSession,
    from_actor: activitypub.models.Actor,
    create_activity: activitypub.models.InboxObject,
    forwarded_by_actor: activitypub.models.Actor | None = None,
    relates_to_inbox_object: activitypub.models.InboxObject | None = None,
) -> None:
    logger.info("Processing Create activity")

    # Some PeerTube activities make no sense to process
    if (
        ap_object_type := ap.as_list(
            (await ap.get_object(create_activity.ap_object))["type"]
        )[0]
    ) in ["CacheFile"]:
        logger.info(f"Dropping Create activity for {ap_object_type} object")
        await db_session.delete(create_activity)
        return None

    if relates_to_inbox_object:
        logger.warning(f"{relates_to_inbox_object.ap_id} is already in the inbox")
        return None

    wrapped_object = ap.unwrap_activity(create_activity.ap_object)
    if create_activity.actor.ap_id != ap.get_actor_id(wrapped_object):
        raise ValueError("Object actor does not match activity")

    ro = RemoteObject(wrapped_object, actor=from_actor)

    # Check if we already received a delete for this object (happens often
    # with forwarded replies)
    delete_object = await get_inbox_delete_for_activity_object_ap_id(
        db_session,
        ro.ap_id,
    )
    if delete_object:
        if delete_object.actor.ap_id != from_actor.ap_id:
            logger.warning(
                f"Got a Delete for {ro.ap_id} from {delete_object.actor.ap_id}??"
            )
            return None
        else:
            logger.info("Already received a Delete for this object, deleting activity")
            create_activity.is_deleted = True
            await db_session.flush()
            return None

    await _process_note_object(
        db_session,
        create_activity,
        from_actor,
        ro,
        forwarded_by_actor=forwarded_by_actor,
    )


async def _handle_read_activity(
    db_session: AsyncSession,
    from_actor: activitypub.models.Actor,
    read_activity: activitypub.models.InboxObject,
) -> None:
    logger.info("Processing Read activity")

    # Honk uses Read activity to propagate replies, fetch the read object
    # from the remote server
    wrapped_object = await ap.fetch(ap.get_id(read_activity.ap_object["object"]))

    wrapped_object_actor = await fetch_actor(
        db_session, ap.get_actor_id(wrapped_object)
    )
    if not wrapped_object_actor.is_blocked:
        ro = RemoteObject(wrapped_object, actor=wrapped_object_actor)

        # Check if we already know about this object
        if await get_inbox_object_by_ap_id(
            db_session,
            ro.ap_id,
        ):
            logger.info(f"{ro.ap_id} is already in the inbox, skipping processing")
            return None

        # Then process it likes it's coming from a forwarded activity
        await _process_note_object(db_session, read_activity, wrapped_object_actor, ro)


async def _process_note_object(
    db_session: AsyncSession,
    parent_activity: activitypub.models.InboxObject,
    from_actor: activitypub.models.Actor,
    ro: RemoteObject,
    forwarded_by_actor: activitypub.models.Actor | None = None,
) -> None:
    if parent_activity.ap_type not in ["Create", "Read"]:
        raise ValueError(f"Unexpected parent activity {parent_activity.ap_id}")

    ap_published_at = now()
    if "published" in ro.ap_object:
        ap_published_at = parse_isoformat(ro.ap_object["published"])

    following = await _get_following(db_session)

    is_from_following = ro.actor.ap_id in {f.ap_actor_id for f in following}
    is_reply = bool(ro.in_reply_to)
    is_local_reply = ro.is_local_reply
    is_mention = False
    hashtags = []
    tags = ro.ap_object.get("tag", [])
    for tag in ap.as_list(tags):
        if tag.get("name") == LOCAL_ACTOR.handle or tag.get("href") == LOCAL_ACTOR.url:
            is_mention = True
        if tag.get("type") == "Hashtag":
            if tag_name := tag.get("name"):
                hashtags.append(tag_name)

    object_info = ObjectInfo(
        is_reply=is_reply,
        is_local_reply=is_local_reply,
        is_mention=is_mention,
        is_from_following=is_from_following,
        hashtags=hashtags,
        actor_handle=ro.actor.handle,
        remote_object=ro,
    )

    inbox_object = activitypub.models.InboxObject(
        server=urlparse(ro.ap_id).hostname,
        actor_id=from_actor.id,
        ap_actor_id=from_actor.ap_id,
        ap_type=ro.ap_type,
        ap_id=ro.ap_id,
        ap_context=ro.ap_context,
        conversation=await fetch_conversation_root(db_session, ro),
        ap_published_at=ap_published_at,
        ap_object=ro.ap_object,
        visibility=ro.visibility,
        relates_to_inbox_object_id=parent_activity.id,
        relates_to_outbox_object_id=None,
        activity_object_ap_id=ro.activity_object_ap_id,
        og_meta=await opengraph.og_meta_from_note(db_session, ro),
        # Hide replies from the stream
        is_hidden_from_stream=not stream_visibility_callback(object_info),
        # We may already have some replies in DB
        replies_count=await _get_replies_count(db_session, ro.ap_id),
        quote_ap_id=ro.quote_ap_id,
    )

    db_session.add(inbox_object)
    await db_session.flush()
    await db_session.refresh(inbox_object)

    parent_activity.relates_to_inbox_object_id = inbox_object.id

    if inbox_object.quote_ap_id:
        await _process_inbound_quote(db_session, inbox_object, ro)

    if inbox_object.in_reply_to:
        replied_object = await get_anybox_object_by_ap_id(
            db_session, inbox_object.in_reply_to
        )
        if replied_object:
            if replied_object.is_from_outbox:
                if replied_object.ap_type == "Question" and inbox_object.ap_object.get(
                    "name"
                ):
                    await _handle_vote_answer(
                        db_session,
                        inbox_object,
                        replied_object,  # type: ignore  # outbox check below
                    )
                else:
                    new_replies_count = await _get_outbox_replies_count(
                        db_session, replied_object  # type: ignore
                    )

                    await db_session.execute(
                        update(activitypub.models.OutboxObject)
                        .where(
                            activitypub.models.OutboxObject.id == replied_object.id,
                        )
                        .values(replies_count=new_replies_count)
                    )
            else:
                new_replies_count = await _get_replies_count(
                    db_session, replied_object.ap_id
                )

                await db_session.execute(
                    update(activitypub.models.InboxObject)
                    .where(
                        activitypub.models.InboxObject.id == replied_object.id,
                    )
                    .values(replies_count=new_replies_count)
                )

        # This object is a reply of a local object, we may need to forward it
        # to our followers (we can only forward JSON-LD signed activities)
        if (
            parent_activity.ap_type == "Create"
            and replied_object
            and replied_object.is_from_outbox
            and replied_object.ap_type != "Question"
            and parent_activity.has_ld_signature
        ):
            logger.info("Forwarding Create activity as it's a local reply")
            skip_actors = [parent_activity.actor]
            if forwarded_by_actor:
                skip_actors.append(forwarded_by_actor)
            recipients = await _get_followers_recipients(
                db_session,
                skip_actors=skip_actors,
            )
            for rcp in recipients:
                await new_outgoing_activity(
                    db_session,
                    rcp,
                    outbox_object_id=None,
                    inbox_object_id=parent_activity.id,
                )

    if is_mention and is_notification_enabled(models.NotificationType.MENTION):
        notif = models.Notification(
            notification_type=models.NotificationType.MENTION,
            actor_id=from_actor.id,
            inbox_object_id=inbox_object.id,
        )
        db_session.add(notif)

    if (
        is_from_following
        and from_actor.are_new_posts_notified
        and not is_reply
        and not is_mention
        and is_notification_enabled(models.NotificationType.STATUS)
    ):
        db_session.add(
            models.Notification(
                notification_type=models.NotificationType.STATUS,
                actor_id=from_actor.id,
                inbox_object_id=inbox_object.id,
            )
        )


async def _process_inbound_quote(
    db_session: AsyncSession,
    inbox_object: activitypub.models.InboxObject,
    ro: RemoteObject,
) -> None:
    """A remote quote arrived (FEP-044f or a legacy alias, per
    `ap_object.Object.quote_ap_id`). Verify any presented stamp, persist the
    result, and -- when there is a stamp to verify -- best-effort fetch the
    quoted object so it can be rendered. A legacy-alias quote with no stamp is
    stored, but stays unverified (and so is never rendered).
    """
    quoted_ap_id = inbox_object.quote_ap_id
    if not quoted_ap_id:
        raise ValueError("Should never happen")

    quote_authorization_ap_id = ro.quote_authorization_ap_id

    quoted_object = await get_anybox_object_by_ap_id(db_session, quoted_ap_id)
    if not quoted_object and quote_authorization_ap_id:
        # Only fetched when there's a stamp to check it against. Without one
        # the quote stays unverified, and an unverified quote is never shown
        # (`get_quoted_object_for_display`, the Mastodon `_serialize_quote`),
        # so fetching its target would be work nothing reads.
        #
        # Bounded and savepointed on purpose. `save_to_inbox` inserts the
        # activity before dispatching here, so SQLite's writer lock is already
        # held: every second spent in here is a second the web app cannot
        # write (`busy_timeout` is 15s). And `save_object_to_inbox` fans out
        # network calls of its own -- the actor, the conversation root, and an
        # OpenGraph scrape per link in the quoted post -- off a URL a remote
        # actor chose for us. `begin_nested` means a failure rolls back to the
        # savepoint and leaves the session usable, so a broken quote target
        # costs the enrichment and not the whole inbound activity.
        try:
            async with db_session.begin_nested():
                raw_quoted_object = await asyncio.wait_for(
                    ap.fetch(quoted_ap_id), timeout=_QUOTE_FETCH_TIMEOUT_SECONDS
                )
                await save_object_to_inbox(db_session, raw_quoted_object)
            quoted_object = await get_anybox_object_by_ap_id(db_session, quoted_ap_id)
        except Exception:
            logger.exception(f"Failed to fetch quoted object {quoted_ap_id}")

    is_verified = False
    if quote_authorization_ap_id and quoted_object and quoted_object.ap_actor_id:
        is_verified = await _verify_quote_authorization(
            db_session,
            quote_authorization_ap_id=quote_authorization_ap_id,
            quoting_object_ap_id=inbox_object.ap_id,
            quoted_object_ap_id=quoted_ap_id,
            quoted_actor_ap_id=quoted_object.ap_actor_id,
        )

    inbox_object.quote_authorization_ap_id = quote_authorization_ap_id
    inbox_object.quote_is_verified = is_verified

    if is_verified and quoted_object and quoted_object.is_from_outbox:
        # Flushed first so the recompute below sees this quote.
        await db_session.flush()
        quoted_object.quotes_count = await _get_quotes_count(  # type: ignore
            db_session, quoted_ap_id
        )
        if is_notification_enabled(models.NotificationType.QUOTE):
            notif = models.Notification(
                notification_type=models.NotificationType.QUOTE,
                actor_id=inbox_object.actor_id,
                inbox_object_id=inbox_object.id,
                outbox_object_id=quoted_object.id,
            )
            db_session.add(notif)


async def _handle_vote_answer(
    db_session: AsyncSession,
    answer: activitypub.models.InboxObject,
    question: activitypub.models.OutboxObject,
) -> None:
    logger.info(f"Processing poll answer for {question.ap_id}: {answer.ap_id}")

    if question.is_poll_ended:
        logger.warning("Poll is ended, discarding answer")
        return

    if not question.poll_items:
        raise ValueError("Should never happen")

    answer_name = answer.ap_object["name"]
    if answer_name not in {pi["name"] for pi in question.poll_items}:
        logger.warning(f"Invalid answer {answer_name=}")
        return

    answer.is_transient = True
    poll_answer = activitypub.models.PollAnswer(
        outbox_object_id=question.id,
        poll_type="oneOf" if question.is_one_of_poll else "anyOf",
        inbox_object_id=answer.id,
        actor_id=answer.actor.id,
        name=answer_name,
    )
    db_session.add(poll_answer)
    await db_session.flush()

    voters_count = await db_session.scalar(
        select(func.count(func.distinct(activitypub.models.PollAnswer.actor_id))).where(
            activitypub.models.PollAnswer.outbox_object_id == question.id
        )
    )

    all_answers = await db_session.execute(
        select(
            func.count(activitypub.models.PollAnswer.name).label("answer_count"),
            activitypub.models.PollAnswer.name,
        )
        .where(activitypub.models.PollAnswer.outbox_object_id == question.id)
        .group_by(activitypub.models.PollAnswer.name)
    )
    all_answers_count = {a["name"]: a["answer_count"] for a in all_answers}

    logger.info(f"{voters_count=}")
    logger.info(f"{all_answers_count=}")

    question_ap_object = dict(question.ap_object)
    question_ap_object["votersCount"] = voters_count
    items_key = "oneOf" if question.is_one_of_poll else "anyOf"
    question_ap_object[items_key] = [
        {
            "type": "Note",
            "name": item["name"],
            "replies": {
                "type": "Collection",
                "totalItems": all_answers_count.get(item["name"], 0),
            },
        }
        for item in question.poll_items
    ]
    updated = now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
    question_ap_object["updated"] = updated
    question.ap_object = question_ap_object

    logger.info(f"Updated question: {question.ap_object}")

    await db_session.flush()

    # Finally send an update
    recipients = await _compute_recipients(db_session, question.ap_object)
    for rcp in recipients:
        await new_outgoing_activity(db_session, rcp, question.id)


async def _handle_announce_activity(
    db_session: AsyncSession,
    actor: activitypub.models.Actor,
    announce_activity: activitypub.models.InboxObject,
    relates_to_outbox_object: activitypub.models.OutboxObject | None,
    relates_to_inbox_object: activitypub.models.InboxObject | None,
):
    if relates_to_outbox_object:
        # This is an announce for a local object
        relates_to_outbox_object.announces_count = (
            activitypub.models.OutboxObject.announces_count + 1
        )

        if is_notification_enabled(models.NotificationType.ANNOUNCE):
            notif = models.Notification(
                notification_type=models.NotificationType.ANNOUNCE,
                actor_id=actor.id,
                outbox_object_id=relates_to_outbox_object.id,
                inbox_object_id=announce_activity.id,
            )
            db_session.add(notif)
    else:
        # Only show the announce in the stream if it comes from an actor
        # in the following collection
        followings = await _get_following(db_session)
        is_from_following = announce_activity.actor.ap_id in {
            f.ap_actor_id for f in followings
        }

        # This is announce for a maybe unknown object
        if relates_to_inbox_object:
            # We already know about this object, show the announce in the
            # stream if it's not already there, from an followed actor
            # and if we haven't seen it recently
            skip_delta = timedelta(hours=1)
            delta_from_original = now() - as_utc(
                relates_to_inbox_object.ap_published_at  # type: ignore
            )
            dup_count = 0
            if (
                not relates_to_inbox_object.is_hidden_from_stream
                and delta_from_original < skip_delta
            ) or (
                dup_count := (
                    await db_session.scalar(
                        select(func.count(activitypub.models.InboxObject.id)).where(
                            activitypub.models.InboxObject.ap_type == "Announce",
                            activitypub.models.InboxObject.ap_published_at
                            > now() - skip_delta,
                            activitypub.models.InboxObject.relates_to_inbox_object_id
                            == relates_to_inbox_object.id,
                            activitypub.models.InboxObject.is_hidden_from_stream.is_(
                                False
                            ),
                        )
                    )
                )
            ) > 0:
                logger.info(f"Deduping Announce {delta_from_original=}/{dup_count=}")
                announce_activity.is_hidden_from_stream = True
            else:
                announce_activity.is_hidden_from_stream = not is_from_following

        else:
            # Save it as an inbox object
            if not announce_activity.activity_object_ap_id:
                raise ValueError("Should never happen")
            announced_raw_object = await ap.fetch(
                announce_activity.activity_object_ap_id
            )

            # Some software return objects wrapped in a Create activity (like
            # python-federation)
            if ap.as_list(announced_raw_object["type"])[0] == "Create":
                announced_raw_object = await ap.get_object(announced_raw_object)

            announced_actor = await fetch_actor(
                db_session, ap.get_actor_id(announced_raw_object)
            )
            if not announced_actor.is_blocked:
                announced_object = RemoteObject(announced_raw_object, announced_actor)
                announced_inbox_object = activitypub.models.InboxObject(
                    server=urlparse(announced_object.ap_id).hostname,
                    actor_id=announced_actor.id,
                    ap_actor_id=announced_actor.ap_id,
                    ap_type=announced_object.ap_type,
                    ap_id=announced_object.ap_id,
                    ap_context=announced_object.ap_context,
                    conversation=await fetch_conversation_root(
                        db_session, announced_object
                    ),
                    ap_published_at=announced_object.ap_published_at,
                    ap_object=announced_object.ap_object,
                    visibility=announced_object.visibility,
                    og_meta=await opengraph.og_meta_from_note(
                        db_session, announced_object
                    ),
                    is_hidden_from_stream=True,
                )
                db_session.add(announced_inbox_object)
                await db_session.flush()
                announce_activity.relates_to_inbox_object_id = announced_inbox_object.id
                announce_activity.is_hidden_from_stream = not is_from_following


async def _handle_like_activity(
    db_session: AsyncSession,
    actor: activitypub.models.Actor,
    like_activity: activitypub.models.InboxObject,
    relates_to_outbox_object: activitypub.models.OutboxObject | None,
    relates_to_inbox_object: activitypub.models.InboxObject | None,
):
    if not relates_to_outbox_object:
        logger.info(
            "Received a like for an unknown activity: "
            f"{like_activity.activity_object_ap_id}, deleting the activity"
        )
        await db_session.delete(like_activity)
    else:
        relates_to_outbox_object.likes_count = await _get_outbox_likes_count(
            db_session,
            relates_to_outbox_object,
        )

        if is_notification_enabled(models.NotificationType.LIKE):
            notif = models.Notification(
                notification_type=models.NotificationType.LIKE,
                actor_id=actor.id,
                outbox_object_id=relates_to_outbox_object.id,
                inbox_object_id=like_activity.id,
            )
            db_session.add(notif)


async def _handle_block_activity(
    db_session: AsyncSession,
    actor: activitypub.models.Actor,
    block_activity: activitypub.models.InboxObject,
):
    if block_activity.activity_object_ap_id != LOCAL_ACTOR.ap_id:
        logger.warning(
            "Received invalid Block activity "
            f"{block_activity.activity_object_ap_id=}"
        )
        await db_session.delete(block_activity)
        return

    # Create a notification
    if is_notification_enabled(models.NotificationType.BLOCKED):
        notif = models.Notification(
            notification_type=models.NotificationType.BLOCKED,
            actor_id=actor.id,
            inbox_object_id=block_activity.id,
        )
        db_session.add(notif)


# How many of a report's reported objects are looked up. Mastodon sends the
# reported account plus the reported statuses; the cap is a bound on hostile
# input, not on anything a real client sends.
_MAX_REPORTED_OBJECTS = 40


async def _handle_flag_activity(
    db_session: AsyncSession,
    actor: activitypub.models.Actor,
    flag_activity: activitypub.models.InboxObject,
) -> None:
    """A remote user reported the owner, or one of their posts, to the owner.

    Two quirks of Mastodon's `Flag`: it comes from the remote *instance* actor
    rather than from the reporter (deliberately, to keep the reporter anonymous),
    and it addresses the reported account plus every reported status in a single
    `object` array.

    There is no moderation queue to file this into — on a single-user instance
    the owner is the moderator — so the proportionate handling is a
    notification. Dropping it silently, which is what happened before, means the
    owner never learns they were reported at all. The reporter's comment lives in
    the activity's `content`, so the `Flag` itself is kept in the inbox.
    """
    reported_ap_ids = flag_activity.activity_object_ap_ids
    local_ap_ids = [ap_id for ap_id in reported_ap_ids if ap_id.startswith(BASE_URL)]
    if not local_ap_ids:
        logger.warning(f"Received a Flag about foreign objects {reported_ap_ids=}")
        await db_session.delete(flag_activity)
        return

    # Link the first reported post (if the report is about posts and not just
    # about the account) so the notification can display it. One batched query,
    # bounded: the id list is attacker-supplied and the incoming queue processes
    # one activity at a time, so a report naming thousands of objects must not
    # turn into thousands of lookups.
    reported_post_ap_ids = [
        ap_id
        for ap_id in local_ap_ids[:_MAX_REPORTED_OBJECTS]
        if ap_id != LOCAL_ACTOR.ap_id
    ]
    reported_objects = {
        obj.ap_id: obj
        for obj in await get_outbox_objects_by_ap_ids(db_session, reported_post_ap_ids)
    }
    reported_outbox_object = next(
        (
            reported_objects[ap_id]
            for ap_id in reported_post_ap_ids
            if ap_id in reported_objects
        ),
        None,
    )

    if is_notification_enabled(models.NotificationType.REPORTED):
        notif = models.Notification(
            notification_type=models.NotificationType.REPORTED,
            actor_id=actor.id,
            inbox_object_id=flag_activity.id,
            outbox_object_id=(
                reported_outbox_object.id if reported_outbox_object else None
            ),
        )
        db_session.add(notif)


async def _process_transient_object(
    db_session: AsyncSession,
    raw_object: ap.RawObject,
    from_actor: activitypub.models.Actor,
) -> None:
    # TODO: track featured/pinned objects for actors
    ap_type = raw_object["type"]
    if ap_type in ["Add", "Remove"]:
        logger.info(f"Dropping unsupported {ap_type} object")
    else:
        # FIXME(ts): handle transient create
        logger.warning(f"Received unknown {ap_type} object")

    return None


async def save_to_inbox(
    db_session: AsyncSession,
    raw_object: ap.RawObject,
    sent_by_ap_actor_id: str,
) -> None:
    # Special case for server sending the actor as a payload (like python-federation)
    if ap.as_list(raw_object["type"])[0] in ap.ACTOR_TYPES:
        if ap.get_id(raw_object) == sent_by_ap_actor_id:
            updated_actor = RemoteActor(raw_object)

            try:
                actor = await fetch_actor(db_session, sent_by_ap_actor_id)
            except ap.ObjectNotFoundError:
                logger.warning("Actor not found")
                return

            # Update the actor
            actor.ap_actor = updated_actor.ap_actor
            await db_session.commit()
            return

        else:
            logger.warning(
                f"Reveived an actor payload {raw_object} from " f"{sent_by_ap_actor_id}"
            )
            return

    try:
        actor = await fetch_actor(db_session, ap.get_id(raw_object["actor"]))
    except ap.ObjectNotFoundError:
        logger.warning("Actor not found")
        return
    except ap.FetchError:
        logger.exception("Failed to fetch actor")
        return

    if is_hostname_blocked(actor.server):
        logger.warning(f"Server {actor.server} is blocked")
        return

    if "id" not in raw_object or not raw_object["id"]:
        # A Mastodon report has no public URI, so its `Flag` arrives without an
        # `id`. It is still worth keeping (the reporter's comment is the useful
        # part), so give it a synthetic one instead of treating it as transient.
        if ap.as_list(raw_object["type"])[0] == "Flag":
            raw_object = {**raw_object, "id": f"{actor.ap_id}#flag/{uuid.uuid4().hex}"}
        else:
            await _process_transient_object(db_session, raw_object, actor)
            return None

    # If we just blocked an actor, we want to process any undo sent as side
    # effects
    if actor.is_blocked and ap.as_list(raw_object["type"])[0] != "Undo":
        logger.warning(f"Actor {actor.ap_id} is blocked, ignoring object")
        return None

    raw_object_id = ap.get_id(raw_object)
    forwarded_by_actor = None

    # Ensure forwarded activities have a valid LD sig
    if sent_by_ap_actor_id != actor.ap_id:
        logger.info(
            f"Processing a forwarded activity {sent_by_ap_actor_id=}/{actor.ap_id}"
        )
        forwarded_by_actor = await fetch_actor(db_session, sent_by_ap_actor_id)

        is_sig_verified = False
        try:
            is_sig_verified = await ldsig.verify_signature(db_session, raw_object)
        except Exception:
            logger.exception("Failed to verify LD sig")

        if not is_sig_verified:
            logger.warning(
                f"Failed to verify LD sig, fetching remote object {raw_object_id}"
            )

            # Try to fetch the remote object since we failed to verify the LD sig
            try:
                raw_object = await ap.fetch(raw_object_id)
            except Exception:
                raise fastapi.HTTPException(status_code=401, detail="Invalid LD sig")

            # Transient activities from Mastodon like Like are not fetchable and
            # will return the actor instead
            if raw_object["id"] != raw_object_id:
                logger.info(f"Unable to fetch {raw_object_id}")
                return None

    if (
        await db_session.scalar(
            select(func.count(activitypub.models.InboxObject.id)).where(
                activitypub.models.InboxObject.ap_id == raw_object_id
            )
        )
        > 0
    ):
        logger.info(
            f'Received duplicate {raw_object["type"]} activity: {raw_object_id}'
        )
        return

    ap_published_at = now()
    if "published" in raw_object:
        ap_published_at = parse_isoformat(raw_object["published"])

    activity_ro = RemoteObject(raw_object, actor=actor)

    relates_to_inbox_object: activitypub.models.InboxObject | None = None
    relates_to_outbox_object: activitypub.models.OutboxObject | None = None
    if activity_ro.activity_object_ap_id:
        if activity_ro.activity_object_ap_id.startswith(BASE_URL):
            relates_to_outbox_object = await get_outbox_object_by_ap_id(
                db_session,
                activity_ro.activity_object_ap_id,
            )
        else:
            relates_to_inbox_object = await get_inbox_object_by_ap_id(
                db_session,
                activity_ro.activity_object_ap_id,
            )

    inbox_object = activitypub.models.InboxObject(
        server=urlparse(activity_ro.ap_id).hostname,
        actor_id=actor.id,
        ap_actor_id=actor.ap_id,
        ap_type=activity_ro.ap_type,
        ap_id=activity_ro.ap_id,
        ap_context=activity_ro.ap_context,
        ap_published_at=ap_published_at,
        ap_object=activity_ro.ap_object,
        visibility=activity_ro.visibility,
        relates_to_inbox_object_id=(
            relates_to_inbox_object.id if relates_to_inbox_object else None
        ),
        relates_to_outbox_object_id=(
            relates_to_outbox_object.id if relates_to_outbox_object else None
        ),
        activity_object_ap_id=activity_ro.activity_object_ap_id,
        is_hidden_from_stream=True,
    )

    db_session.add(inbox_object)
    await db_session.flush()
    await db_session.refresh(inbox_object)

    if activity_ro.ap_type == "Create":
        await _handle_create_activity(
            db_session,
            actor,
            inbox_object,
            forwarded_by_actor=forwarded_by_actor,
            relates_to_inbox_object=relates_to_inbox_object,
        )
    elif activity_ro.ap_type == "Read":
        await _handle_read_activity(db_session, actor, inbox_object)
    elif activity_ro.ap_type == "Update":
        await _handle_update_activity(db_session, actor, inbox_object)
    elif activity_ro.ap_type == "Move":
        await _handle_move_activity(db_session, actor, inbox_object)
    elif activity_ro.ap_type == "Delete":
        await _handle_delete_activity(
            db_session,
            actor,
            inbox_object,
            relates_to_inbox_object,
            forwarded_by_actor=forwarded_by_actor,
        )
    elif activity_ro.ap_type == "Follow":
        await _handle_follow_follow_activity(db_session, actor, inbox_object)
    elif activity_ro.ap_type == "Undo":
        if relates_to_inbox_object:
            await _handle_undo_activity(
                db_session, actor, inbox_object, relates_to_inbox_object
            )
        else:
            logger.info("Received Undo for an unknown activity")
    elif activity_ro.ap_type in ["Accept", "Reject"]:
        if not relates_to_outbox_object:
            logger.info(
                f"Received {raw_object['type']} for an unknown activity: "
                f"{activity_ro.activity_object_ap_id}"
            )
        else:
            if relates_to_outbox_object.ap_type == "Follow":
                notif_type = (
                    models.NotificationType.FOLLOW_REQUEST_ACCEPTED
                    if activity_ro.ap_type == "Accept"
                    else models.NotificationType.FOLLOW_REQUEST_REJECTED
                )
                if is_notification_enabled(notif_type):
                    notif = models.Notification(
                        notification_type=notif_type,
                        actor_id=actor.id,
                        inbox_object_id=inbox_object.id,
                    )
                    db_session.add(notif)

                if activity_ro.ap_type == "Accept":
                    following = activitypub.models.Following(
                        actor_id=actor.id,
                        outbox_object_id=relates_to_outbox_object.id,
                        ap_actor_id=actor.ap_id,
                    )
                    db_session.add(following)

                    # Pre-fetch the latest activities
                    try:
                        await prefetch_actor_outbox(db_session, actor)
                    except Exception:
                        logger.exception(f"Failed to prefetch outbox for {actor.ap_id}")
                elif activity_ro.ap_type == "Reject":
                    maybe_following = (
                        await db_session.scalars(
                            select(activitypub.models.Following).where(
                                activitypub.models.Following.ap_actor_id == actor.ap_id,
                            )
                        )
                    ).one_or_none()
                    if maybe_following:
                        logger.info("Removing actor from following")
                        await db_session.delete(maybe_following)

            elif relates_to_outbox_object.ap_type == "QuoteRequest":
                await _handle_quote_request_accept_or_reject(
                    db_session, inbox_object, relates_to_outbox_object
                )
            else:
                logger.info(
                    "Received an Accept for an unsupported activity: "
                    f"{relates_to_outbox_object.ap_type}"
                )
    elif activity_ro.ap_type == "EmojiReact":
        if not relates_to_outbox_object:
            logger.info(
                "Received a reaction for an unknown activity: "
                f"{activity_ro.activity_object_ap_id}"
            )
            await db_session.delete(inbox_object)
        else:
            # TODO(ts): support reactions
            pass
    elif activity_ro.ap_type == "Like":
        await _handle_like_activity(
            db_session,
            actor,
            inbox_object,
            relates_to_outbox_object,
            relates_to_inbox_object,
        )
    elif activity_ro.ap_type == "Announce":
        await _handle_announce_activity(
            db_session,
            actor,
            inbox_object,
            relates_to_outbox_object,
            relates_to_inbox_object,
        )
    elif activity_ro.ap_type == "View":
        # View is used by Peertube, there's nothing useful we can do with it
        await db_session.delete(inbox_object)
    elif activity_ro.ap_type == "Block":
        await _handle_block_activity(
            db_session,
            actor,
            inbox_object,
        )
    elif activity_ro.ap_type == "Flag":
        await _handle_flag_activity(
            db_session,
            actor,
            inbox_object,
        )
    elif activity_ro.ap_type == "QuoteRequest":
        await _handle_quote_request_activity(
            db_session,
            actor,
            inbox_object,
        )
    else:
        logger.warning(f"Received an unknown {inbox_object.ap_type} object")

    await db_session.commit()


_PREFETCH_TIME_BUDGET_SECONDS = 8.0


async def prefetch_actor_outbox(
    db_session: AsyncSession,
    actor: activitypub.models.Actor,
) -> None:
    """Try to fetch some notes to fill the stream.

    Bounded by a time budget rather than a fixed post count: each post costs
    its own sequential remote round-trip (fetch the activity, sometimes the
    object), so a hard count cap either wastes the budget on a fast server or
    blocks the caller for far too long on a slow one.
    """
    started_at = time.monotonic()
    outbox = await ap.parse_collection(actor.outbox_url, limit=20)
    for activity in outbox[:20]:
        if time.monotonic() - started_at > _PREFETCH_TIME_BUDGET_SECONDS:
            break

        activity_id = ap.get_id(activity)
        raw_activity = await ap.fetch(activity_id)
        if ap.as_list(raw_activity["type"])[0] == "Create":
            obj = await ap.get_object(raw_activity)
            saved_inbox_object = await get_inbox_object_by_ap_id(
                db_session, ap.get_id(obj)
            )
            if not saved_inbox_object:
                saved_inbox_object = await save_object_to_inbox(db_session, obj)

            if not saved_inbox_object.in_reply_to:
                saved_inbox_object.is_hidden_from_stream = False

    actor.outbox_backfilled_at = now()

    # commit is performed by the caller


async def save_object_to_inbox(
    db_session: AsyncSession,
    raw_object: ap.RawObject,
) -> activitypub.models.InboxObject:
    """Used to save unknown object before intetacting with them, i.e. to like
    an object that was looked up, or prefill the inbox when an actor accepted
    a follow request."""
    obj_actor = await fetch_actor(db_session, ap.get_actor_id(raw_object))

    ro = RemoteObject(raw_object, actor=obj_actor)

    ap_published_at = now()
    if "published" in ro.ap_object:
        ap_published_at = parse_isoformat(ro.ap_object["published"])

    inbox_object = activitypub.models.InboxObject(
        server=urlparse(ro.ap_id).hostname,
        actor_id=obj_actor.id,
        ap_actor_id=obj_actor.ap_id,
        ap_type=ro.ap_type,
        ap_id=ro.ap_id,
        ap_context=ro.ap_context,
        conversation=await fetch_conversation_root(db_session, ro),
        ap_published_at=ap_published_at,
        ap_object=ro.ap_object,
        visibility=ro.visibility,
        relates_to_inbox_object_id=None,
        relates_to_outbox_object_id=None,
        activity_object_ap_id=ro.activity_object_ap_id,
        og_meta=await opengraph.og_meta_from_note(db_session, ro),
        is_hidden_from_stream=True,
    )

    db_session.add(inbox_object)
    await db_session.flush()
    await db_session.refresh(inbox_object)
    return inbox_object


_FETCH_REPLIES_TIME_BUDGET_SECONDS = 8.0
_MAX_NEW_REPLIES_PER_CALL = 2
_REPLIABLE_AP_TYPES = ["Note", "Article", "Page", "Question"]


async def fetch_replies(
    db_session: AsyncSession,
    requested_object: AnyboxObject | RemoteObject,
) -> int:
    """On-demand backfill of a remote object's AS2 `replies` collection.

    ActivityPub delivery is push-based, so replies from actors/servers we
    don't follow never land in our inbox on their own. This lets an admin
    pull them in for one object at a time. It can only surface what the
    remote server chooses to expose via `replies` (some omit or restrict it).

    Saving a reply from an actor we've never seen requires fetching that
    actor's profile plus a WebFinger lookup, which can each stall for many
    seconds with no timeout on an unresponsive host. Capped to a couple of
    new replies per call so one click can't turn into a multi-minute,
    likely-to-time-out request; click again to pull in more.
    """
    # Objects saved via an Announce we hadn't seen before never got a
    # `conversation` backfilled (see _handle_announce_activity), which makes
    # the replies tree treat them as standalone and skip the lookup below
    # entirely. Fix that up while we're already here.
    if (
        isinstance(
            requested_object,
            (activitypub.models.InboxObject, activitypub.models.OutboxObject),
        )
        and requested_object.conversation is None
    ):
        requested_object.conversation = await fetch_conversation_root(
            db_session, requested_object
        )

    replies_ref = requested_object.ap_object.get("replies")

    # The cached copy may predate the remote object having (or growing) a
    # `replies` collection, since we only ever store what was pushed to us
    # or fetched once. Refresh it from its canonical URL so a stale/missing
    # pointer doesn't block backfilling forever.
    if not requested_object.ap_id.startswith(BASE_URL):
        try:
            fresh_object = await ap.fetch(requested_object.ap_id)
        except Exception:
            logger.exception(f"Failed to refresh {requested_object.ap_id}")
        else:
            replies_ref = fresh_object.get("replies") or replies_ref

    if not replies_ref:
        return 0

    try:
        if isinstance(replies_ref, str):
            raw_items = await ap.parse_collection(url=replies_ref, limit=20)
        else:
            raw_items = await ap.parse_collection(payload=replies_ref, limit=20)
    except Exception:
        logger.exception(
            f"Failed to fetch replies collection for {requested_object.ap_id}"
        )
        return 0

    logger.info(
        f"Fetched {len(raw_items)} candidate replies for {requested_object.ap_id}: "
        f"{[item.get('id') if isinstance(item, dict) else item for item in raw_items]}"
    )

    fetched_count = 0
    started_at = time.monotonic()
    for item in raw_items[:20]:
        if fetched_count >= _MAX_NEW_REPLIES_PER_CALL:
            break
        if time.monotonic() - started_at > _FETCH_REPLIES_TIME_BUDGET_SECONDS:
            break

        try:
            reply_ap_id = ap.get_id(item)
            if await get_anybox_object_by_ap_id(db_session, reply_ap_id):
                logger.info(f"Already known, skipping {reply_ap_id}")
                continue

            raw_reply = (
                item
                if isinstance(item, dict) and "type" in item
                else await ap.fetch(reply_ap_id)
            )
            if ap.as_list(raw_reply["type"])[0] not in _REPLIABLE_AP_TYPES:
                logger.info(
                    f"Skipping non-repliable type {raw_reply['type']!r} for {reply_ap_id}"
                )
                continue

            await save_object_to_inbox(db_session, raw_reply)
            fetched_count += 1
        except Exception:
            logger.exception(
                f"Failed to fetch reply {item!r} for {requested_object.ap_id}"
            )
            continue

    return fetched_count


async def public_outbox_objects_count(db_session: AsyncSession) -> int:
    return await db_session.scalar(
        select(func.count(activitypub.models.OutboxObject.id)).where(
            activitypub.models.OutboxObject.visibility == ap.VisibilityEnum.PUBLIC,
            activitypub.models.OutboxObject.is_deleted.is_(False),
        )
    )


async def fetch_actor_collection(db_session: AsyncSession, url: str) -> list[Actor]:
    if url.startswith(config.BASE_URL):
        if url == config.BASE_URL + "/followers":
            followers = (
                (
                    await db_session.scalars(
                        select(activitypub.models.Follower).options(
                            joinedload(activitypub.models.Follower.actor)
                        )
                    )
                )
                .unique()
                .all()
            )
            return [follower.actor for follower in followers]
        else:
            raise ValueError(f"internal collection for {url}) not supported")

    return [RemoteActor(actor) for actor in await ap.parse_collection(url)]


@dataclass
class ReplyTreeNode:
    ap_object: AnyboxObject | None
    wm_reply: WebmentionReply | None
    children: list["ReplyTreeNode"]
    is_requested: bool = False
    is_root: bool = False

    @property
    def published_at(self) -> datetime.datetime:
        if self.ap_object:
            return self.ap_object.ap_published_at  # type: ignore
        elif self.wm_reply:
            return self.wm_reply.published_at
        else:
            raise ValueError(f"Should never happen: {self}")


async def get_replies_tree(
    db_session: AsyncSession,
    requested_object: AnyboxObject,
    is_current_user_admin: bool,
) -> ReplyTreeNode:
    # XXX: PeerTube video don't use context
    tree_nodes: list[AnyboxObject] = []
    if requested_object.conversation is None:
        tree_nodes = [requested_object]
    else:
        logger.info(f"Requested conversation: '{requested_object.conversation}'")

        allowed_visibility = [ap.VisibilityEnum.PUBLIC, ap.VisibilityEnum.UNLISTED]
        if is_current_user_admin:
            allowed_visibility = list(ap.VisibilityEnum)
        logger.info(f"Allowed visibility: {allowed_visibility}")

        tree_nodes.extend(
            (
                await db_session.scalars(
                    select(activitypub.models.InboxObject)
                    .where(
                        (
                            activitypub.models.InboxObject.conversation
                            == requested_object.conversation
                        )
                        | (
                            activitypub.models.InboxObject.ap_context
                            == requested_object.conversation
                        ),
                        activitypub.models.InboxObject.ap_type.in_(
                            ["Note", "Page", "Article", "Question"]
                        ),
                        activitypub.models.InboxObject.is_deleted.is_(False),
                        activitypub.models.InboxObject.visibility.in_(
                            allowed_visibility
                        ),
                    )
                    .options(joinedload(activitypub.models.InboxObject.actor))
                )
            )
            .unique()
            .all()
        )

        tree_nodes.extend(
            (
                await db_session.scalars(
                    select(activitypub.models.OutboxObject)
                    .where(
                        activitypub.models.OutboxObject.conversation
                        == requested_object.conversation,
                        activitypub.models.OutboxObject.is_deleted.is_(False),
                        activitypub.models.OutboxObject.ap_type.in_(
                            ["Note", "Page", "Article", "Question"]
                        ),
                        activitypub.models.OutboxObject.visibility.in_(
                            allowed_visibility
                        ),
                    )
                    .options(
                        joinedload(
                            activitypub.models.OutboxObject.outbox_object_attachments
                        ).options(
                            joinedload(activitypub.models.OutboxObjectAttachment.upload)
                        )
                    )
                )
            )
            .unique()
            .all()
        )
    nodes_by_in_reply_to = defaultdict(list)
    for node in tree_nodes:
        nodes_by_in_reply_to[node.in_reply_to].append(node)
    logger.info(f"Nodes in reply to: {nodes_by_in_reply_to}")

    if len(nodes_by_in_reply_to.get(None, [])) > 1:
        raise ValueError(f"Invalid replies tree: {[n.ap_object for n in tree_nodes]}")

    def _get_reply_node_children(
        node: ReplyTreeNode,
        index: defaultdict[str | None, list[AnyboxObject]],
    ) -> list[ReplyTreeNode]:
        children = []
        for child in index.get(node.ap_object.ap_id, []):  # type: ignore
            child_node = ReplyTreeNode(
                ap_object=child,
                wm_reply=None,
                is_requested=child.ap_id == requested_object.ap_id,  # type: ignore
                children=[],
            )
            child_node.children = _get_reply_node_children(child_node, index)
            children.append(child_node)

        return sorted(
            children,
            key=lambda node: node.published_at,
        )

    if None in nodes_by_in_reply_to:
        root_ap_object = nodes_by_in_reply_to[None][0]
    else:
        root_ap_object = sorted(
            tree_nodes,
            key=lambda ap_obj: ap_obj.ap_published_at,  # type: ignore
        )[0]

    root_node = ReplyTreeNode(
        ap_object=root_ap_object,
        wm_reply=None,
        is_root=True,
        is_requested=root_ap_object.ap_id == requested_object.ap_id,
        children=[],
    )
    root_node.children = _get_reply_node_children(root_node, nodes_by_in_reply_to)
    return root_node
