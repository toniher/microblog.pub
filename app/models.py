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

    # outgoing
    BLOCK = "block"
    UNBLOCK = "unblock"


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    notification_type = Column(Enum(NotificationType), nullable=True)
    is_new = Column(Boolean, nullable=False, default=True)

    actor_id = Column(Integer, ForeignKey("actor.id"), nullable=True)
    actor = relationship(Actor, uselist=False)

    outbox_object_id = Column(Integer, ForeignKey("outbox.id"), nullable=True)
    outbox_object = relationship(OutboxObject, uselist=False)

    inbox_object_id = Column(Integer, ForeignKey("inbox.id"), nullable=True)
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
