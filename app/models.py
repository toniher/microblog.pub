import enum
from datetime import datetime
from typing import Any

import pydantic
from loguru import logger
from sqlalchemy import JSON
from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import UniqueConstraint
from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import relationship

from activitypub import activitypub as ap
from activitypub.models import Actor
from activitypub.models import InboxObject
from activitypub.models import OutboxObject
from activitypub.models import muted_actor_ids
from app.database import Base
from app.database import metadata_obj
from app.utils import webmentions
from app.utils.datetime import now


class ObjectRevision(pydantic.BaseModel):
    ap_object: ap.RawObject
    source: str
    updated_at: str


class IndieAuthAuthorizationRequest(Base):
    __tablename__ = "indieauth_authorization_request"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)

    code = Column(String, nullable=False, unique=True, index=True)
    scope = Column(String, nullable=False)
    redirect_uri = Column(String, nullable=False)
    client_id = Column(String, nullable=False)
    code_challenge = Column(String, nullable=True)
    code_challenge_method = Column(String, nullable=True)

    is_used = Column(Boolean, nullable=False, default=False)


class IndieAuthAccessToken(Base):
    __tablename__ = "indieauth_access_token"

    id = Column(Integer, primary_key=True, index=True)
    created_at: Mapped[datetime] = Column(
        DateTime(timezone=True), nullable=False, default=now
    )

    # Will be null for personal access tokens
    indieauth_authorization_request_id = Column(
        Integer, ForeignKey("indieauth_authorization_request.id"), nullable=True
    )
    indieauth_authorization_request = relationship(
        IndieAuthAuthorizationRequest,
        uselist=False,
    )

    access_token: Mapped[str] = Column(String, nullable=False, unique=True, index=True)
    refresh_token = Column(String, nullable=True, unique=True, index=True)
    expires_in: Mapped[int] = Column(Integer, nullable=False)
    scope = Column(String, nullable=False)
    is_revoked = Column(Boolean, nullable=False, default=False)
    was_refreshed = Column(Boolean, nullable=False, default=False, server_default="0")


class OAuthClient(Base):
    __tablename__ = "oauth_client"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)

    # Request
    client_name = Column(String, nullable=False)
    redirect_uris: Mapped[list[str]] = Column(JSON, nullable=True)

    # Optional from request
    client_uri = Column(String, nullable=True)
    logo_uri = Column(String, nullable=True)
    scope = Column(String, nullable=True)

    # Response
    client_id = Column(String, nullable=False, unique=True, index=True)
    client_secret = Column(String, nullable=False, unique=True)


@enum.unique
class WebmentionType(str, enum.Enum):
    UNKNOWN = "unknown"
    LIKE = "like"
    REPLY = "reply"
    REPOST = "repost"


class Webmention(Base):
    __tablename__ = "webmention"
    __table_args__ = (UniqueConstraint("source", "target", name="uix_source_target"),)

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)

    is_deleted = Column(Boolean, nullable=False, default=False)

    source: Mapped[str] = Column(String, nullable=False, index=True, unique=True)
    source_microformats: Mapped[dict[str, Any] | None] = Column(JSON, nullable=True)

    target = Column(String, nullable=False, index=True)
    outbox_object_id = Column(Integer, ForeignKey("outbox.id"), nullable=False)
    outbox_object = relationship(OutboxObject, uselist=False)

    webmention_type = Column(Enum(WebmentionType), nullable=True)

    @property
    def as_facepile_item(self) -> webmentions.Webmention | None:
        if not self.source_microformats:
            return None
        try:
            return webmentions.Webmention.from_microformats(
                self.source_microformats["items"], self.source
            )
        except Exception:
            # TODO: return a facepile with the unknown image
            logger.warning(
                f"Failed to generate facefile item for Webmention id={self.id}"
            )
            return None


@enum.unique
class NotificationType(str, enum.Enum):
    NEW_FOLLOWER = "new_follower"
    PENDING_INCOMING_FOLLOWER = "pending_incoming_follower"
    REJECTED_FOLLOWER = "rejected_follower"
    UNFOLLOW = "unfollow"

    FOLLOW_REQUEST_ACCEPTED = "follow_request_accepted"
    FOLLOW_REQUEST_REJECTED = "follow_request_rejected"

    MOVE = "move"

    LIKE = "like"
    UNDO_LIKE = "undo_like"

    ANNOUNCE = "announce"
    UNDO_ANNOUNCE = "undo_announce"

    MENTION = "mention"

    NEW_WEBMENTION = "new_webmention"
    UPDATED_WEBMENTION = "updated_webmention"
    DELETED_WEBMENTION = "deleted_webmention"

    # incoming
    BLOCKED = "blocked"
    UNBLOCKED = "unblocked"

    # A remote user reported one of the owner's posts (or the account itself)
    # via an inbound `Flag`. There is no moderation queue on a single-user
    # instance — the owner *is* the moderator — so the report surfaces as a
    # notification and nothing else.
    REPORTED = "reported"

    # A remote actor's quote of one of the owner's posts was authorized
    # (FEP-044f), whether by auto-accept or manual approval.
    QUOTE = "quote"

    # `quote_policy = "manual"`: a remote actor's QuoteRequest is waiting on
    # the owner to accept/reject it, like PENDING_INCOMING_FOLLOWER.
    PENDING_INCOMING_QUOTE_REQUEST = "pending_incoming_quote_request"

    # outgoing
    BLOCK = "block"
    UNBLOCK = "unblock"

    # A followed actor (with `notify` set) published a new top-level post.
    STATUS = "status"
    # A followed actor edited a post the owner favourited or boosted.
    UPDATE = "update"
    # A poll the owner posted, or voted in, has ended.
    POLL = "poll"


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    notification_type = Column(Enum(NotificationType), nullable=True)
    is_new = Column(Boolean, nullable=False, default=True)

    actor_id = Column(Integer, ForeignKey("actor.id"), nullable=True)
    actor = relationship(Actor, uselist=False)

    # Indexed for the `poll` sweep's watermark check
    # (`app.poll_notifications`), which correlates a NOT EXISTS on these
    # columns every few seconds; unindexed they turned into a full scan of
    # this table per candidate poll, growing with the notification history.
    outbox_object_id = Column(
        Integer, ForeignKey("outbox.id"), nullable=True, index=True
    )
    outbox_object = relationship(OutboxObject, uselist=False)

    inbox_object_id = Column(Integer, ForeignKey("inbox.id"), nullable=True, index=True)
    inbox_object = relationship(InboxObject, uselist=False)

    webmention_id = Column(
        Integer, ForeignKey("webmention.id", name="fk_webmention_id"), nullable=True
    )
    webmention = relationship(Webmention, uselist=False)

    is_accepted = Column(Boolean, nullable=True)
    is_rejected = Column(Boolean, nullable=True)


class Marker(Base):
    """Cross-device read-position sync (`GET/POST /api/v1/markers`).

    Single-user instance, so `timeline` alone ("home"/"notifications") is
    the natural unique key — no per-account scoping needed.
    """

    __tablename__ = "marker"

    id = Column(Integer, primary_key=True, index=True)
    timeline = Column(String, nullable=False, unique=True, index=True)
    last_read_id = Column(String, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now)


class MutedConversation(Base):
    """A muted thread (Mastodon's `POST /api/v1/statuses/{id}/mute`).

    Keyed on the `conversation` string `InboxObject`/`OutboxObject` already
    track for threading (see `activitypub/boxes.py`'s `fetch_conversation_root`)
    rather than the status id itself, so replies that arrive *after* the mute
    are covered too, not just the ones that exist yet.
    """

    __tablename__ = "muted_conversation"

    id = Column(Integer, primary_key=True, index=True)
    conversation = Column(String, nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)


class ScheduledStatus(Base):
    """A status queued for later publication (`POST /api/v1/statuses` with
    `scheduled_at`).

    The compose parameters live in a single JSON blob rather than in columns:
    they are exactly what Mastodon echoes back as the entity's `params`, and
    publishing just replays them through `send_create` — nothing ever queries
    an individual parameter. Attachments and the reply parent are kept as the
    Mastodon ids the client sent (see `app.scheduled_statuses.ComposeParams`),
    not as foreign keys, so deleting an upload or a parent status can't leave
    a dangling reference in a row that may sit here for weeks.

    `scheduled_at` is the user-facing publication time (echoed, and editable
    via `PUT`); `next_try` is the worker's view of the same thing, moved
    forward on a failed publish attempt. A NULL `next_try` means "given up
    after `MAX_TRIES`" — the row stays listed so the owner can reschedule it
    (which resets the retry state) or delete it.
    """

    __tablename__ = "scheduled_status"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)

    scheduled_at: Mapped[datetime] = Column(
        DateTime(timezone=True), nullable=False, index=True
    )
    params: Mapped[dict[str, Any]] = Column(JSON, nullable=False)

    tries = Column(Integer, nullable=False, default=0)
    next_try = Column(DateTime(timezone=True), nullable=True, index=True)
    last_error = Column(String, nullable=True)


class PushSubscription(Base):
    """A Web Push subscription (`POST /api/v1/push/subscription`).

    Mastodon semantics: one subscription per access token (a `POST` replaces
    any existing row), hence the unique FK. There is no separate delivery
    queue — unlike `OutgoingActivity`, which fans one activity out to N
    inboxes, each subscription is a strictly serial channel to one device, so
    a per-subscription cursor (`last_notification_id`) gives ordering for
    free with less machinery. A subscription that dies (404/410, or its
    token is revoked/expired, or it exhausts its retries) is deleted rather
    than flagged errored, so `GET` never advertises a subscription that will
    never deliver again.
    """

    __tablename__ = "push_subscription"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)

    access_token_id = Column(
        Integer,
        ForeignKey(
            "indieauth_access_token.id", name="fk_push_subscription_access_token_id"
        ),
        nullable=False,
        unique=True,
        index=True,
    )
    access_token = relationship(IndieAuthAccessToken, uselist=False)

    endpoint = Column(String, nullable=False)
    # `p256dh` (65-byte uncompressed EC point) and `auth` (16-byte secret),
    # both stored base64url-encoded — see `app.webpush.parse_p256dh` /
    # `parse_auth_secret` for the decoded form the worker actually uses.
    p256dh = Column(String, nullable=False)
    auth = Column(String, nullable=False)

    alert_mention = Column(Boolean, nullable=False, default=True)
    alert_status = Column(Boolean, nullable=False, default=True)
    alert_reblog = Column(Boolean, nullable=False, default=True)
    alert_follow = Column(Boolean, nullable=False, default=True)
    alert_follow_request = Column(Boolean, nullable=False, default=True)
    alert_favourite = Column(Boolean, nullable=False, default=True)
    alert_poll = Column(Boolean, nullable=False, default=True)
    alert_update = Column(Boolean, nullable=False, default=True)

    # all|followed|follower|none, mirroring the Mastodon subscription policy.
    policy = Column(String, nullable=False, default="all")

    # The watermark: the highest `Notification.id` already dealt with
    # (delivered, filtered out, or permanently rejected). Plain Integer, not
    # a FK — the referenced row may be pruned by `app/prune.py` while the
    # watermark itself must survive.
    last_notification_id = Column(Integer, nullable=False, default=0)

    tries = Column(Integer, nullable=False, default=0)
    next_try = Column(DateTime(timezone=True), nullable=True, default=now, index=True)
    last_try = Column(DateTime(timezone=True), nullable=True)
    last_success_at = Column(DateTime(timezone=True), nullable=True)
    last_status_code = Column(Integer, nullable=True)
    error = Column(String, nullable=True)


def notification_not_muted() -> Any:
    """Where-clause dropping notifications sent by a muted actor.

    Only mutes set to hide notifications count (the API's `notifications`
    parameter); the NULL arm keeps actor-less notifications — webmentions —
    which a bare `NOT IN` would evaluate to NULL and silently drop.
    """
    return or_(
        Notification.actor_id.is_(None),
        Notification.actor_id.not_in(muted_actor_ids(notifications_only=True)),
    )


def notification_not_in_muted_conversation() -> Any:
    """Where-clause dropping notifications tied to a muted conversation.

    Only inbox-side notifications (mentions/replies) carry a conversation
    worth checking against `MutedConversation`; anything without an inbox
    object (follows, webmentions...) is unaffected, same NULL-safe shape as
    `notification_not_muted`.
    """
    muted_inbox_object_ids = select(InboxObject.id).where(
        InboxObject.conversation.in_(select(MutedConversation.conversation))
    )
    return or_(
        Notification.inbox_object_id.is_(None),
        Notification.inbox_object_id.not_in(muted_inbox_object_ids),
    )


def notification_target_present() -> Any:
    """Where-clause dropping notifications whose target row is gone.

    `outbox_object_id`/`inbox_object_id` are nullable FKs with no cascade and
    SQLite's FK enforcement is off, so hard-deleting the row they point at
    (`app/prune.py`) leaves the notification behind with a dangling
    reference. Mastodon never outlives a status with a notification about it,
    so its clients aren't prepared for one either — a `status_id: null`
    mention group is what broke the notifications screen in the wild.

    Kept in SQL rather than only filtered after the fact so `LIMIT` (and the
    grouped-notifications assembly window) counts serviceable rows only:
    filtering afterwards silently shortens a page, and an all-dangling page
    yields no `Link` header at all, which clients read as end-of-list. Both
    FK columns are indexed and the targets are primary keys, so each arm
    costs an index lookup.
    """
    return or_(
        and_(
            Notification.outbox_object_id.is_(None),
            Notification.inbox_object_id.is_(None),
        ),
        Notification.outbox_object.has(),
        Notification.inbox_object.has(),
    )


outbox_fts = Table(
    "outbox_fts",
    # TODO(tsileo): use Base.metadata
    metadata_obj,
    Column("rowid", Integer),
    Column("outbox_fts", String),
    Column("summary", String, nullable=True),
    Column("name", String, nullable=True),
    Column("source", String),
)

# db.execute(select(outbox_fts.c.rowid).where(outbox_fts.c.outbox_fts.op("MATCH")("toto AND omg"))).all()  # noqa
# db.execute(select(models.OutboxObject).join(outbox_fts, outbox_fts.c.rowid == models.OutboxObject.id).where(outbox_fts.c.outbox_fts.op("MATCH")("toto2"))).scalars()  # noqa
# db.execute(insert(outbox_fts).values({"outbox_fts": "delete", "rowid": 1, "source": dat[0].source}))  # noqa
