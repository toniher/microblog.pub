"""Mastodon-style numeric ids for AP objects/actors.

Mastodon clients treat entity ids as opaque numeric strings. `InboxObject` and
`OutboxObject` (activitypub/models.py) are separate tables with independent
integer PK sequences, so a raw `id` is ambiguous between them. The source
table is encoded into the id itself so decoding is unambiguous, rather than
guessing via fetch precedence.
"""

import enum

from sqlalchemy.orm import joinedload

from activitypub.models import Actor
from activitypub.models import InboxObject
from activitypub.models import OutboxObject
from activitypub.models import OutboxObjectAttachment
from activitypub.models import Upload
from app.database import AsyncSession


class ObjectSource(enum.IntEnum):
    OUTBOX = 0
    INBOX = 1


# The server's single owner (`activitypub.actor.LOCAL_ACTOR`) isn't a row in
# the `actor` table (it wraps the local profile directly), so it has no
# integer PK to encode. Reserve "0" for it: real `Actor` row ids are
# autoincrement starting at 1, so this can never collide with one.
LOCAL_ACTOR_ID = "0"


def encode_object_id(internal_id: int, source: ObjectSource) -> str:
    return str(internal_id * 2 + int(source))


def decode_object_id(mastodon_id: str) -> tuple[int, ObjectSource] | None:
    try:
        raw = int(mastodon_id)
    except ValueError:
        return None
    if raw < 0:
        return None
    return raw // 2, ObjectSource(raw % 2)


def decode_object_id_for_source(mastodon_id: str, source: ObjectSource) -> int | None:
    """Decode a status id, but only if it belongs to `source`.

    For single-table paginated lists (e.g. the owner's own outbox, or a
    remote actor's cached inbox notes): a cursor id from the *other* table
    doesn't apply to this collection, so treat it as absent rather than
    guess.
    """
    decoded = decode_object_id(mastodon_id)
    if decoded is None or decoded[1] is not source:
        return None
    return decoded[0]


def encode_outbox_id(outbox_object: OutboxObject) -> str:
    if outbox_object.id is None:
        raise ValueError("OutboxObject must be persisted before it has an id")
    return encode_object_id(outbox_object.id, ObjectSource.OUTBOX)


def encode_inbox_id(inbox_object: InboxObject) -> str:
    if inbox_object.id is None:
        raise ValueError("InboxObject must be persisted before it has an id")
    return encode_object_id(inbox_object.id, ObjectSource.INBOX)


def encode_account_id(actor: Actor) -> str:
    return str(actor.id)


def account_id_for_actor(actor: object) -> str:
    """Mastodon account id for any actor this server can encounter: the
    owner (`LOCAL_ACTOR_ID` sentinel) or a cached remote `Actor` row.
    """
    if isinstance(actor, Actor):
        return encode_account_id(actor)
    return LOCAL_ACTOR_ID


def decode_account_id(mastodon_id: str) -> int | None:
    try:
        return int(mastodon_id)
    except ValueError:
        return None


# Eager-load what the Mastodon Status serializer needs off an object so nothing
# lazy-loads later — lazy loading isn't available in an async session and would
# crash. OutboxObject.actor is a plain property (always LOCAL_ACTOR, no query),
# so only its attachments need eager loading; InboxObject.actor is a real
# relationship and does.
_OUTBOX_OPTIONS = [
    joinedload(OutboxObject.outbox_object_attachments).joinedload(
        OutboxObjectAttachment.upload
    )
]
_INBOX_OPTIONS = [joinedload(InboxObject.actor)]


async def get_object_by_mastodon_id(
    db_session: AsyncSession, mastodon_id: str
) -> OutboxObject | InboxObject | None:
    """Resolve a Mastodon status id back to its InboxObject/OutboxObject row.

    IDs minted by `encode_object_id` decode unambiguously. As a fallback (a
    stray id that doesn't resolve in its encoded table), try the other table
    with the same internal id — outbox first, then inbox — mirroring
    `activitypub.boxes.get_anybox_object_by_ap_id`'s precedence.
    """
    decoded = decode_object_id(mastodon_id)
    if decoded is None:
        return None
    internal_id, source = decoded

    # populate_existing=True matters: right after a write (e.g. send_create),
    # the object is already in the session's identity map, and Session.get()
    # silently ignores eager-load `options` on an identity-map hit unless
    # this is set — leaving the relationship unloaded and any later access
    # a lazy-load crash (lazy loading isn't available in an async session).
    if source is ObjectSource.OUTBOX:
        outbox_object = await db_session.get(
            OutboxObject, internal_id, options=_OUTBOX_OPTIONS, populate_existing=True
        )
        if outbox_object is not None:
            return outbox_object
        return await db_session.get(
            InboxObject, internal_id, options=_INBOX_OPTIONS, populate_existing=True
        )
    else:
        inbox_object = await db_session.get(
            InboxObject, internal_id, options=_INBOX_OPTIONS, populate_existing=True
        )
        if inbox_object is not None:
            return inbox_object
        return await db_session.get(
            OutboxObject, internal_id, options=_OUTBOX_OPTIONS, populate_existing=True
        )


async def get_account_by_mastodon_id(
    db_session: AsyncSession, mastodon_id: str
) -> Actor | None:
    internal_id = decode_account_id(mastodon_id)
    if internal_id is None:
        return None
    return await db_session.get(Actor, internal_id)


# Uploads are a single table (unlike statuses/actors), so their Mastodon id is
# just the row's own PK — no dual-table encoding needed.


def encode_upload_id(upload: Upload) -> str:
    if upload.id is None:
        raise ValueError("Upload must be persisted before it has an id")
    return str(upload.id)


def decode_upload_id(mastodon_id: str) -> int | None:
    try:
        return int(mastodon_id)
    except ValueError:
        return None


async def get_upload_by_mastodon_id(
    db_session: AsyncSession, mastodon_id: str
) -> Upload | None:
    internal_id = decode_upload_id(mastodon_id)
    if internal_id is None:
        return None
    return await db_session.get(Upload, internal_id)
