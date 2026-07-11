"""Mastodon-style numeric ids for AP objects/actors.

Mastodon clients treat entity ids as opaque numeric strings. `InboxObject` and
`OutboxObject` (activitypub/models.py) are separate tables with independent
integer PK sequences, so a raw `id` is ambiguous between them. The source
table is encoded into the id itself so decoding is unambiguous, rather than
guessing via fetch precedence.
"""

import enum

from activitypub.models import Actor
from activitypub.models import InboxObject
from activitypub.models import OutboxObject
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


def decode_account_id(mastodon_id: str) -> int | None:
    try:
        return int(mastodon_id)
    except ValueError:
        return None


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

    if source is ObjectSource.OUTBOX:
        outbox_object = await db_session.get(OutboxObject, internal_id)
        if outbox_object is not None:
            return outbox_object
        return await db_session.get(InboxObject, internal_id)
    else:
        inbox_object = await db_session.get(InboxObject, internal_id)
        if inbox_object is not None:
            return inbox_object
        return await db_session.get(OutboxObject, internal_id)


async def get_account_by_mastodon_id(
    db_session: AsyncSession, mastodon_id: str
) -> Actor | None:
    internal_id = decode_account_id(mastodon_id)
    if internal_id is None:
        return None
    return await db_session.get(Actor, internal_id)
