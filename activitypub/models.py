from typing import Any
from typing import Optional
from typing import Union

from sqlalchemy import DDL
from sqlalchemy import JSON
from sqlalchemy import Boolean
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import Enum
from sqlalchemy import Float
from sqlalchemy import ForeignKey
from sqlalchemy import Index
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import UniqueConstraint
from sqlalchemy import column
from sqlalchemy import event
from sqlalchemy import func
from sqlalchemy import inspect
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import table
from sqlalchemy import text
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import aliased
from sqlalchemy.orm import relationship
from sqlalchemy.sql import Select

from activitypub import activitypub as ap
from activitypub.actor import LOCAL_ACTOR
from activitypub.actor import Actor as BaseActor
from activitypub.ap_object import Attachment
from activitypub.ap_object import Object as BaseObject
from activitypub.ap_object import format_xsd_duration
from app.config import ALIAS_URL_PREFIX
from app.config import BASE_URL
from app.database import Base
from app.utils import search_text
from app.utils.datetime import as_utc
from app.utils.datetime import now


class Actor(Base, BaseActor):
    __tablename__ = "actor"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now)

    ap_id: Mapped[str] = Column(String, unique=True, nullable=False, index=True)
    ap_actor: Mapped[ap.RawObject] = Column(JSON, nullable=False)
    ap_type = Column(String, nullable=False)

    handle = Column(String, nullable=True, index=True)

    is_blocked = Column(Boolean, nullable=False, default=False, server_default="0")
    is_deleted = Column(Boolean, nullable=False, default=False, server_default="0")

    # Muting is purely local (nothing is federated, unlike a block): the actor
    # keeps following/being followed, their posts just stop showing up.
    #
    # Indexed because `muted_actor_ids()` runs as a subquery on every timeline
    # *and* notification read (it renders twice per timeline query, once for
    # the actor and once nested for boosts of a muted actor's posts): without
    # it SQLite pays for an AUTOMATIC PARTIAL COVERING INDEX build plus a bare
    # SCAN actor on every execution (measured over 5k actors, 1% muted: 0.386ms
    # -> 0.178ms/query, both scans gone, with the index). Plain, not partial —
    # see the note on `5eabb060f447`.
    is_muted = Column(
        Boolean, nullable=False, default=False, server_default="0", index=True
    )
    # Null while muted means "until I unmute"; a timestamp makes the mute
    # lapse on its own (Mastodon's `duration` parameter). Nothing sweeps
    # expired rows — `is_muted_now`/`muted_actor_ids()` apply the expiry at
    # read time, and unmuting clears the flag.
    muted_until = Column(DateTime(timezone=True), nullable=True)
    # Whether the mute also hides notifications from this actor (Mastodon's
    # `notifications` parameter, `muting_notifications` in a relationship).
    are_notifications_muted = Column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    # Whether boosts from this actor are hidden from the stream (Mastodon's
    # `reblogs` follow parameter, inverted). Read-time and retroactive —
    # applied wherever the stream is queried, not at ingestion.
    #
    # Indexed because `announces_hidden_actor_ids()` runs as a subquery on
    # every timeline read: without it SQLite builds a transient AUTOMATIC
    # PARTIAL COVERING INDEX over the whole actor table on each execution
    # (measured at +0.25ms/query over 5k actors, vs +0.006ms with the index).
    # Plain, not partial — see the note on `5eabb060f447`.
    are_announces_hidden_from_stream = Column(
        Boolean, nullable=False, default=False, server_default="0", index=True
    )
    # Whether a new top-level post from this actor generates a `status`
    # notification (Mastodon's `notify` follow parameter).
    are_new_posts_notified = Column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    # A personal note about this actor (Mastodon's `accounts/{id}/note`).
    # Purely local, nothing federated. Null/empty means no note.
    note = Column(String, nullable=True)

    # Last time we tried to backfill this actor's outbox on demand (e.g. a
    # Mastodon client viewing a non-followed actor's profile). Throttles
    # repeat live fetches from the same actor across requests.
    outbox_backfilled_at = Column(DateTime(timezone=True), nullable=True)

    # Cached from the actor's followers/following/outbox collections (we
    # never store a remote actor's own social graph/posts otherwise). Null
    # means "never fetched"; the Mastodon API serializer falls back to 0.
    followers_count = Column(Integer, nullable=True)
    following_count = Column(Integer, nullable=True)
    statuses_count = Column(Integer, nullable=True)
    counts_refreshed_at = Column(DateTime(timezone=True), nullable=True)

    # Normalized (NFC + casefold) handle/name/ID, kept in sync by the mapper
    # events below and indexed by `actor_search`'s FTS5 trigram index -- see
    # `app/utils/search_text.py`. Nullable so a row written before this
    # column existed degrades to "not indexed" rather than breaking.
    search_text = Column(String, nullable=True)

    @property
    def is_from_db(self) -> bool:
        return True

    @property
    def is_muted_now(self) -> bool:
        """`is_muted` with the expiry applied — a lapsed mute is no mute."""
        if not self.is_muted:
            return False
        return self.muted_until is None or as_utc(self.muted_until) > now()

    @property
    def are_notifications_muted_now(self) -> bool:
        return self.is_muted_now and bool(self.are_notifications_muted)


def muted_actor_ids(*, notifications_only: bool = False) -> Select:
    """Ids of the actors whose mute is currently in effect.

    The SQL counterpart of `Actor.is_muted_now`, for filtering timelines and
    notifications with a subquery instead of loading every muted actor.
    """
    where = [
        Actor.is_muted.is_(True),
        or_(Actor.muted_until.is_(None), Actor.muted_until > now()),
    ]
    if notifications_only:
        where.append(Actor.are_notifications_muted.is_(True))
    return select(Actor.id).where(*where)


def not_from_muted_actors() -> list[Any]:
    """Where-clauses dropping muted actors from a query over `InboxObject`.

    Covers both what a muted actor sent us and what someone else did with a
    muted actor's object (a boost of their note, most visibly) — Mastodon
    hides both.
    """
    related = aliased(InboxObject)
    return [
        InboxObject.actor_id.not_in(muted_actor_ids()),
        or_(
            InboxObject.relates_to_inbox_object_id.is_(None),
            InboxObject.relates_to_inbox_object_id.not_in(
                select(related.id).where(related.actor_id.in_(muted_actor_ids()))
            ),
        ),
    ]


def announces_hidden_actor_ids() -> Select:
    """Ids of the actors whose boosts are hidden from the stream."""
    return select(Actor.id).where(Actor.are_announces_hidden_from_stream.is_(True))


def not_hidden_announces() -> list[Any]:
    """Where-clause dropping boosts from actors with hidden announces.

    `ap_type` is non-null on every `InboxObject`, so unlike
    `not_from_muted_actors()` this needs no NULL arm.
    """
    return [
        or_(
            InboxObject.ap_type != "Announce",
            InboxObject.actor_id.not_in(announces_hidden_actor_ids()),
        )
    ]


# `inReplyTo` lives only inside the `ap_object` JSON, so every reply lookup
# (`_get_replies_count`, `fetch_direct_replies_ap_ids`) matches on the extracted
# value. SQLite *can* index that expression -- but only if the query renders the
# JSON path as a literal: with the path sent as a bound parameter the planner
# does not recognize it as the indexed expression and reverts to `SCAN`, parsing
# every stored payload (measured over a 50k-row inbox: ~92ms per lookup scanning,
# vs. 5.8ms taking the index, and the latter is constant in table size rather
# than linear). So the queries must go through `in_reply_to_expr()` and the
# indexes below must stay textually equivalent to it -- see
# `test_reply_lookup_uses_the_expression_index`.
_IN_REPLY_TO_JSON_PATH = "'$.inReplyTo'"
_IN_REPLY_TO_INDEX_EXPR = f"json_extract(ap_object, {_IN_REPLY_TO_JSON_PATH})"


def in_reply_to_expr(ap_object_column: Any) -> Any:
    """`inReplyTo`, extracted so the expression indexes below can serve it."""
    return func.json_extract(ap_object_column, text(_IN_REPLY_TO_JSON_PATH))


class InboxObject(Base, BaseObject):
    __tablename__ = "inbox"
    __table_args__ = (
        Index("ix_inbox_ap_published_at", "ap_published_at"),
        Index(
            "ix_inbox_stream",
            "is_deleted",
            "is_hidden_from_stream",
            "ap_published_at",
        ),
        # `ix_inbox_stream` drives every plain timeline (home/public/hashtag)
        # in `ap_published_at` order, which is right for those. A list
        # timeline additionally filters to a handful of member `actor_id`s,
        # and SQLite won't give up the ordering index for that filter on its
        # own — this composite exists so `app.mastodon.timelines` can force
        # an actor_id-driven plan for that one case (`force_actor_index=`),
        # turning an O(inbox size) scan into O(member posts).
        Index("ix_inbox_actor_id_ap_published_at", "actor_id", "ap_published_at"),
        Index("ix_inbox_in_reply_to", text(_IN_REPLY_TO_INDEX_EXPR)),
        Index("ix_inbox_quote_ap_id", "quote_ap_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now)

    actor_id = Column(Integer, ForeignKey("actor.id"), nullable=False, index=True)
    actor: Mapped[Actor] = relationship(Actor, uselist=False)

    server = Column(String, nullable=False)

    is_hidden_from_stream = Column(Boolean, nullable=False, default=False)

    ap_actor_id = Column(String, nullable=False)
    ap_type = Column(String, nullable=False, index=True)
    ap_id: Mapped[str] = Column(String, nullable=False, unique=True, index=True)
    ap_context = Column(String, nullable=True)
    ap_published_at = Column(DateTime(timezone=True), nullable=False)
    ap_object: Mapped[ap.RawObject] = Column(JSON, nullable=False)

    # Only set for activities
    activity_object_ap_id = Column(String, nullable=True, index=True)

    visibility = Column(Enum(ap.VisibilityEnum), nullable=False)
    conversation = Column(String, nullable=True, index=True)

    has_local_mention = Column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    # Used for Like, Announce and Undo activities
    relates_to_inbox_object_id = Column(
        Integer,
        ForeignKey("inbox.id"),
        nullable=True,
        index=True,
    )
    relates_to_inbox_object: Mapped[Optional["InboxObject"]] = relationship(
        "InboxObject",
        foreign_keys=relates_to_inbox_object_id,
        remote_side=id,
        uselist=False,
    )
    relates_to_outbox_object_id = Column(
        Integer,
        ForeignKey("outbox.id"),
        nullable=True,
        index=True,
    )
    relates_to_outbox_object: Mapped[Optional["OutboxObject"]] = relationship(
        "OutboxObject",
        foreign_keys=[relates_to_outbox_object_id],
        uselist=False,
    )

    undone_by_inbox_object_id = Column(
        Integer, ForeignKey("inbox.id"), nullable=True, index=True
    )

    # Link the oubox AP ID to allow undo without any extra query
    liked_via_outbox_object_ap_id = Column(String, nullable=True)
    announced_via_outbox_object_ap_id = Column(String, nullable=True)
    voted_for_answers: Mapped[list[str] | None] = Column(JSON, nullable=True)

    is_bookmarked = Column(Boolean, nullable=False, default=False)

    # Used to mark deleted objects, but also activities that were undone
    is_deleted = Column(Boolean, nullable=False, default=False)
    is_transient = Column(Boolean, nullable=False, default=False, server_default="0")

    replies_count: Mapped[int] = Column(Integer, nullable=False, default=0)

    # FEP-044f quote posts (see ap_object.Object.quote_ap_id/quote_authorization_ap_id
    # for the tolerant parsing of the various wire aliases). Columns rather than a
    # json_extract() lookup, per the ix_*_in_reply_to lesson above -- maintaining
    # OutboxObject.quotes_count queries quote_ap_id, and that must not scan `ap_object`.
    quote_ap_id = Column(String, nullable=True)
    quote_authorization_ap_id = Column(String, nullable=True)
    quote_is_verified = Column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    og_meta: Mapped[list[dict[str, Any]] | None] = Column(JSON, nullable=True)

    # Normalized (NFC + casefold) visible content plus link targets, kept in
    # sync by the mapper events below and indexed by `inbox_search`'s FTS5
    # trigram index -- see `app/utils/search_text.py`. Nullable so a row
    # written before this column existed degrades to "not indexed" rather
    # than breaking.
    search_text = Column(String, nullable=True)

    @property
    def relates_to_anybox_object(self) -> Union["InboxObject", "OutboxObject"] | None:
        if self.relates_to_inbox_object_id:
            return self.relates_to_inbox_object
        elif self.relates_to_outbox_object_id:
            return self.relates_to_outbox_object
        else:
            return None

    @property
    def is_from_db(self) -> bool:
        return True

    @property
    def is_from_inbox(self) -> bool:
        return True

    @property
    def is_quote_revocable(self) -> bool:
        """Whether the owner minted the quote's authorization stamp and can
        revoke it via `boxes.send_quote_revoke`."""
        return bool(
            self.quote_is_verified
            and self.quote_authorization_ap_id
            and self.quote_authorization_ap_id.startswith(BASE_URL)
        )


# Max number of posts that can be pinned at once (matches Mastodon's default)
MAX_PINNED_OBJECTS = 5


class OutboxObject(Base, BaseObject):
    __tablename__ = "outbox"
    __table_args__ = (
        Index("ix_outbox_ap_published_at", "ap_published_at"),
        # Deliberately does NOT lead with `visibility`: the Mastodon outbox
        # timeline (`app.mastodon.timelines.fetch_outbox_timeline_page`) never
        # constrains it, and
        # SQLite can't use an index whose leading column is unconstrained.
        # Leading with the two flags both queries do share lets this one index
        # serve the homepage/articles pages *and* the Mastodon timelines, with
        # `ap_published_at` trailing so it also satisfies the ORDER BY. The
        # homepage's `visibility` predicate becomes a post-filter, which is
        # cheap here since nearly every outbox row on a single-user instance
        # is public.
        Index(
            "ix_outbox_homepage",
            "is_deleted",
            "is_hidden_from_homepage",
            "ap_published_at",
        ),
        Index("ix_outbox_in_reply_to", text(_IN_REPLY_TO_INDEX_EXPR)),
    )

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now)

    is_hidden_from_homepage = Column(Boolean, nullable=False, default=False)

    public_id = Column(String, nullable=False, index=True)
    slug = Column(String, nullable=True, index=True)
    # Human-readable alias overriding the permalink, see the `url` property
    # below. One unique index serves both the uniqueness and the lookup --
    # a separate non-unique index alongside a UNIQUE constraint would build a
    # second B-tree over the same column for no read benefit. SQLite allows
    # unlimited NULLs under it, so unaliased rows are unaffected.
    alias = Column(String, nullable=True, index=True, unique=True)

    ap_type = Column(String, nullable=False, index=True)
    ap_id: Mapped[str] = Column(String, nullable=False, unique=True, index=True)
    ap_context = Column(String, nullable=True)
    ap_object: Mapped[ap.RawObject] = Column(JSON, nullable=False)

    activity_object_ap_id = Column(String, nullable=True, index=True)

    # Source content for activities (like Notes)
    source = Column(String, nullable=True)
    revisions: Mapped[list[dict[str, Any]] | None] = Column(JSON, nullable=True)

    ap_published_at = Column(DateTime(timezone=True), nullable=False, default=now)
    visibility = Column(Enum(ap.VisibilityEnum), nullable=False)
    conversation = Column(String, nullable=True, index=True)

    likes_count = Column(Integer, nullable=False, default=0)
    announces_count = Column(Integer, nullable=False, default=0)
    replies_count: Mapped[int] = Column(Integer, nullable=False, default=0)
    webmentions_count: Mapped[int] = Column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # reactions: Mapped[list[dict[str, Any]] | None] = Column(JSON, nullable=True)

    og_meta: Mapped[list[dict[str, Any]] | None] = Column(JSON, nullable=True)

    # Normalized (NFC + casefold) visible content plus link targets, kept in
    # sync by the mapper events below and indexed by `outbox_search`'s FTS5
    # trigram index -- see `app/utils/search_text.py`. Nullable so a row
    # written before this column existed degrades to "not indexed" rather
    # than breaking.
    search_text = Column(String, nullable=True)

    # For the featured collection
    is_pinned = Column(Boolean, nullable=False, default=False)
    is_transient = Column(Boolean, nullable=False, default=False, server_default="0")

    # Never actually delete from the outbox
    is_deleted = Column(Boolean, nullable=False, default=False)

    # Used for Create, Like, Announce and Undo activities
    relates_to_inbox_object_id = Column(
        Integer,
        ForeignKey("inbox.id"),
        nullable=True,
        index=True,
    )
    relates_to_inbox_object: Mapped[Optional["InboxObject"]] = relationship(
        "InboxObject",
        foreign_keys=[relates_to_inbox_object_id],
        uselist=False,
    )
    relates_to_outbox_object_id = Column(
        Integer,
        ForeignKey("outbox.id"),
        nullable=True,
        index=True,
    )
    relates_to_outbox_object: Mapped[Optional["OutboxObject"]] = relationship(
        "OutboxObject",
        foreign_keys=[relates_to_outbox_object_id],
        remote_side=id,
        uselist=False,
    )
    # For Follow activies
    relates_to_actor_id = Column(
        Integer,
        ForeignKey("actor.id"),
        nullable=True,
        index=True,
    )
    relates_to_actor: Mapped[Optional["Actor"]] = relationship(
        "Actor",
        foreign_keys=[relates_to_actor_id],
        uselist=False,
    )

    undone_by_outbox_object_id = Column(
        Integer, ForeignKey("outbox.id"), nullable=True, index=True
    )

    # FEP-044f quote posts: set on the *quoting* outbox row (the post we quote,
    # the stamp we received back, and where the request stands). Also reused
    # for the stamp itself when this row is an outbound `QuoteAuthorization`
    # (an OutboxObject with ap_type="QuoteAuthorization").
    quote_ap_id = Column(String, nullable=True)
    quote_authorization_ap_id = Column(String, nullable=True)
    quote_state = Column(String, nullable=True)
    # Authorized *remote* quotes of this post (maintained on the quoted side,
    # not the quoting side). Recomputed from `inbox.quote_ap_id` by
    # `boxes._get_quotes_count` rather than incremented, so a deleted quote
    # does not leave the count drifting upward.
    quotes_count = Column(Integer, nullable=False, default=0, server_default="0")

    @property
    def actor(self) -> BaseActor:
        return LOCAL_ACTOR

    outbox_object_attachments: Mapped[list["OutboxObjectAttachment"]] = relationship(
        "OutboxObjectAttachment", uselist=True, backref="outbox_object"
    )

    @property
    def attachments(self) -> list[Attachment]:
        out = []
        for attachment in self.outbox_object_attachments:
            upload = attachment.upload
            url = BASE_URL + f"/attachments/{upload.content_hash}/{attachment.filename}"
            resized_url = (
                BASE_URL
                + (
                    "/attachments/thumbnails/"
                    f"{upload.content_hash}/{attachment.filename}"
                )
                if upload.has_thumbnail
                else None
            )
            out.append(
                Attachment.model_validate(
                    {
                        "type": "Document",
                        "mediaType": upload.content_type,
                        "name": attachment.alt or attachment.filename,
                        "url": url,
                        "width": upload.width,
                        "height": upload.height,
                        "proxiedUrl": url,
                        "resizedUrl": resized_url,
                        "blurhash": upload.blurhash,
                        "duration": (
                            format_xsd_duration(float(upload.duration))
                            if upload.duration is not None
                            else None
                        ),
                        "posterUrl": (
                            resized_url
                            if resized_url and upload.content_type.startswith("video")
                            else None
                        ),
                        "hasAudio": upload.has_audio,
                        "focalPoint": (
                            [upload.focus_x, upload.focus_y]
                            if upload.focus_x is not None and upload.focus_y is not None
                            else None
                        ),
                    }
                )
            )
        return out

    @property
    def relates_to_anybox_object(self) -> Union["InboxObject", "OutboxObject"] | None:
        if self.relates_to_inbox_object_id:
            return self.relates_to_inbox_object
        elif self.relates_to_outbox_object_id:
            return self.relates_to_outbox_object
        else:
            return None

    @property
    def is_from_db(self) -> bool:
        return True

    @property
    def is_from_outbox(self) -> bool:
        return True

    @property
    def url(self) -> str | None:
        if self.alias:
            return f"{BASE_URL}/{ALIAS_URL_PREFIX}/{self.alias}"
        # XXX: rewrite old URL here for compat
        if self.ap_type == "Article" and self.slug and self.public_id:
            return f"{BASE_URL}/articles/{self.public_id[:7]}/{self.slug}"
        # Not `super().url`: that falls back to `ap_object["url"]` verbatim,
        # which for a local object only ever legitimately diverges from
        # `ap_id` in the two cases already handled above. Reading it here too
        # would mean trusting whatever was last written there -- including a
        # stale alias URL still sitting in storage while `set_outbox_object_alias`
        # is in the middle of recomputing it (the property is evaluated from
        # the very dict it's about to replace).
        return self.ap_id


# --- Full-text search ------------------------------------------------------
#
# `search_text` (defined on each model above) is normalized (NFC + casefold)
# at write time by the mapper events below -- that's what makes search
# Unicode-correct, since SQLite's `LIKE`/`lower()` fold ASCII only. It is
# indexed by an FTS5 external-content table per source table, using the
# trigram tokenizer so a substring `GLOB` query is served from the index
# instead of a full scan -- see `matches_search()` and
# `app/mastodon/router.py`'s search functions.
#
# The DDL lives here, rather than only in the migration, because
# `tests/conftest.py` builds its schema with `Base.metadata.create_all`;
# DDL that lived only in the migration would leave every test running
# unindexed. The migration imports `fts5_ddl_statements()` so the two stay
# identical -- the same discipline `_IN_REPLY_TO_INDEX_EXPR` follows for the
# reply index.


def fts5_ddl_statements(table_name: str) -> tuple[list[str], list[str]]:
    """`(create, drop)` DDL statements for `table_name`'s FTS5 shadow index.

    Each entry is executed as its own statement -- SQLite (and so
    SQLAlchemy's `DDL`) only runs one statement per `execute()` call, so this
    can't be a single multi-statement script.
    """
    fts = f"{table_name}_search"
    create = [
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {fts} USING fts5("
        f"search_text, content='{table_name}', content_rowid='id', "
        f"tokenize='trigram')",
        f"CREATE TRIGGER IF NOT EXISTS {fts}_ai AFTER INSERT ON {table_name} BEGIN "
        f"INSERT INTO {fts}(rowid, search_text) VALUES (new.id, new.search_text); "
        f"END",
        f"CREATE TRIGGER IF NOT EXISTS {fts}_ad AFTER DELETE ON {table_name} BEGIN "
        f"INSERT INTO {fts}({fts}, rowid, search_text) "
        f"VALUES('delete', old.id, old.search_text); "
        f"END",
        f"CREATE TRIGGER IF NOT EXISTS {fts}_au AFTER UPDATE ON {table_name} BEGIN "
        f"INSERT INTO {fts}({fts}, rowid, search_text) "
        f"VALUES('delete', old.id, old.search_text); "
        f"INSERT INTO {fts}(rowid, search_text) VALUES (new.id, new.search_text); "
        f"END",
    ]
    drop = [
        f"DROP TRIGGER IF EXISTS {fts}_au",
        f"DROP TRIGGER IF EXISTS {fts}_ad",
        f"DROP TRIGGER IF EXISTS {fts}_ai",
        f"DROP TABLE IF EXISTS {fts}",
    ]
    return create, drop


def _search_table(table_name: str) -> Any:
    """A lightweight, unmapped handle onto `table_name`'s FTS5 shadow table,
    for building `GLOB` queries against it. It deliberately isn't a `Base`
    model/`Table` on `Base.metadata`: the DDL above creates it as a virtual
    table, not one `create_all` should try to `CREATE TABLE` itself."""
    return table(f"{table_name}_search", column("rowid"), column("search_text"))


ACTOR_SEARCH = _search_table("actor")
INBOX_SEARCH = _search_table("inbox")
OUTBOX_SEARCH = _search_table("outbox")

for _model, _table_name in (
    (Actor, "actor"),
    (InboxObject, "inbox"),
    (OutboxObject, "outbox"),
):
    _create_stmts, _drop_stmts = fts5_ddl_statements(_table_name)
    for _stmt in _create_stmts:
        event.listen(_model.__table__, "after_create", DDL(_stmt))
    for _stmt in _drop_stmts:
        event.listen(_model.__table__, "before_drop", DDL(_stmt))
del _model, _table_name, _create_stmts, _drop_stmts, _stmt


def matches_search(fts_table: Any, pattern: str) -> Any:
    """`fts_table.search_text GLOB pattern`, evaluated through the trigram
    index rather than `LIKE ... ESCAPE`: the FTS5 trigram tokenizer only
    serves `L0` (plain `LIKE`) or `G0` (`GLOB`) -- an `ESCAPE` clause
    silently drops the query back to a full scan, which is why the old
    `LIKE ... ESCAPE` form can't just be pointed at this table. Build
    `pattern` with `app.utils.search_text.glob_pattern()`."""
    return fts_table.c.search_text.op("GLOB")(pattern)


def _populate_search_text(source_attr: str, compute: Any) -> Any:
    def handler(mapper: Any, connection: Any, target: Any) -> None:
        setattr(target, "search_text", compute(getattr(target, source_attr)))

    return handler


def _refresh_search_text_on_change(source_attr: str, compute: Any) -> Any:
    def handler(mapper: Any, connection: Any, target: Any) -> None:
        # A plain field bump (e.g. `replies_count`) shouldn't re-parse HTML
        # on every write; only recompute when the indexed source actually
        # changed.
        if inspect(target).attrs[source_attr].history.has_changes():
            setattr(target, "search_text", compute(getattr(target, source_attr)))

    return handler


event.listen(
    Actor,
    "before_insert",
    _populate_search_text("ap_actor", search_text.actor_search_text),
)
event.listen(
    Actor,
    "before_update",
    _refresh_search_text_on_change("ap_actor", search_text.actor_search_text),
)
for _box_model in (InboxObject, OutboxObject):
    event.listen(
        _box_model,
        "before_insert",
        _populate_search_text("ap_object", search_text.object_search_text),
    )
    event.listen(
        _box_model,
        "before_update",
        _refresh_search_text_on_change("ap_object", search_text.object_search_text),
    )
del _box_model


class Follower(Base):
    __tablename__ = "follower"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now)

    actor_id = Column(Integer, ForeignKey("actor.id"), nullable=False, unique=True)
    actor: Mapped[Actor] = relationship(Actor, uselist=False)

    inbox_object_id = Column(Integer, ForeignKey("inbox.id"), nullable=False)
    inbox_object = relationship(InboxObject, uselist=False)

    ap_actor_id = Column(String, nullable=False, unique=True)


class Following(Base):
    __tablename__ = "following"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=now)

    actor_id = Column(Integer, ForeignKey("actor.id"), nullable=False, unique=True)
    actor = relationship(Actor, uselist=False)

    outbox_object_id = Column(Integer, ForeignKey("outbox.id"), nullable=False)
    outbox_object = relationship(OutboxObject, uselist=False)

    ap_actor_id = Column(String, nullable=False, unique=True)


class IncomingActivity(Base):
    __tablename__ = "incoming_activity"
    __table_args__ = (
        Index(
            "ix_incoming_activity_queue",
            "is_errored",
            "is_processed",
            "next_try",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)

    # An incoming activity can be a webmention
    webmention_source = Column(String, nullable=True)
    # or an AP object
    sent_by_ap_actor_id = Column(String, nullable=True)
    ap_id = Column(String, nullable=True, index=True)
    ap_object: Mapped[ap.RawObject] = Column(JSON, nullable=True)

    tries: Mapped[int] = Column(Integer, nullable=False, default=0)
    next_try = Column(DateTime(timezone=True), nullable=True, default=now)

    last_try = Column(DateTime(timezone=True), nullable=True)

    is_processed = Column(Boolean, nullable=False, default=False)
    is_errored = Column(Boolean, nullable=False, default=False)
    error = Column(String, nullable=True)


class OutgoingActivity(Base):
    __tablename__ = "outgoing_activity"
    __table_args__ = (
        Index(
            "ix_outgoing_activity_queue",
            "is_errored",
            "is_sent",
            "next_try",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)

    recipient = Column(String, nullable=False)

    outbox_object_id = Column(
        Integer, ForeignKey("outbox.id"), nullable=True, index=True
    )
    outbox_object = relationship(OutboxObject, uselist=False)

    # Can also reference an inbox object if it needds to be forwarded
    inbox_object_id = Column(Integer, ForeignKey("inbox.id"), nullable=True, index=True)
    inbox_object = relationship(InboxObject, uselist=False)

    # The source will be the outbox object URL
    webmention_target = Column(String, nullable=True)

    tries = Column(Integer, nullable=False, default=0)
    next_try = Column(DateTime(timezone=True), nullable=True, default=now)

    last_try = Column(DateTime(timezone=True), nullable=True)
    last_status_code = Column(Integer, nullable=True)
    last_response = Column(String, nullable=True)

    is_sent = Column(Boolean, nullable=False, default=False)
    is_errored = Column(Boolean, nullable=False, default=False)
    error = Column(String, nullable=True)

    @property
    def anybox_object(self) -> OutboxObject | InboxObject:
        if self.outbox_object_id:
            return self.outbox_object  # type: ignore
        elif self.inbox_object_id:
            return self.inbox_object  # type: ignore
        else:
            raise ValueError("Should never happen")


class TaggedOutboxObject(Base):
    __tablename__ = "tagged_outbox_object"
    __table_args__ = (
        UniqueConstraint("outbox_object_id", "tag", name="uix_tagged_object"),
    )

    id = Column(Integer, primary_key=True, index=True)

    outbox_object_id = Column(Integer, ForeignKey("outbox.id"), nullable=False)
    outbox_object = relationship(OutboxObject, uselist=False)

    tag = Column(String, nullable=False, index=True)


class Upload(Base):
    __tablename__ = "upload"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)

    content_type: Mapped[str] = Column(String, nullable=False)
    content_hash = Column(String, nullable=False, unique=True)

    has_thumbnail = Column(Boolean, nullable=False)

    # Alt text set via the Mastodon media API (POST/PUT /api/v1/media), which
    # uploads media before a status exists to attach it to — unlike
    # OutboxObjectAttachment.alt, this must survive independently of any post.
    description = Column(String, nullable=True)

    # Only set for images and video (blurhash), or video/audio (width/height,
    # video only)
    blurhash = Column(String, nullable=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)

    # Only set for video/audio; NULL when ffmpeg is unavailable or the probe
    # failed.
    duration = Column(Float, nullable=True)
    has_audio = Column(Boolean, nullable=True)

    # Client-supplied cropping hint (Mastodon's `focus` media param), each in
    # [-1.0, 1.0] with (0, 0) as center. Set via POST/PUT /api/v1/media, never
    # derived from the file itself.
    focus_x = Column(Float, nullable=True)
    focus_y = Column(Float, nullable=True)

    @property
    def is_image(self) -> bool:
        return self.content_type.startswith("image")


class OutboxObjectAttachment(Base):
    __tablename__ = "outbox_object_attachment"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)
    filename = Column(String, nullable=False)
    alt = Column(String, nullable=True)

    outbox_object_id = Column(Integer, ForeignKey("outbox.id"), nullable=False)

    upload_id = Column(Integer, ForeignKey("upload.id"), nullable=False)
    upload: Mapped["Upload"] = relationship(Upload, uselist=False)


class PollAnswer(Base):
    __tablename__ = "poll_answer"
    __table_args__ = (
        # Enforce a single answer for poll/actor/answer
        UniqueConstraint(
            "outbox_object_id",
            "name",
            "actor_id",
            name="uix_outbox_object_id_name_actor_id",
        ),
        # Enforce an actor can only vote once on a "oneOf" Question
        Index(
            "uix_one_of_outbox_object_id_actor_id",
            "outbox_object_id",
            "actor_id",
            unique=True,
            sqlite_where=text('poll_type = "oneOf"'),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=now)

    outbox_object_id = Column(Integer, ForeignKey("outbox.id"), nullable=False)
    outbox_object = relationship(OutboxObject, uselist=False)

    # oneOf|anyOf
    poll_type = Column(String, nullable=False)

    inbox_object_id = Column(Integer, ForeignKey("inbox.id"), nullable=False)
    inbox_object = relationship(InboxObject, uselist=False)

    actor_id = Column(Integer, ForeignKey("actor.id"), nullable=False)
    actor = relationship(Actor, uselist=False)

    name = Column(String, nullable=False)
