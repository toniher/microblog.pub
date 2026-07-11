import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from activitypub.actor import LOCAL_ACTOR
from activitypub.ap_object import RemoteObject
from activitypub.models import InboxObject
from activitypub.models import OutboxObject
from activitypub.tests import factories
from app.mastodon import ids


def test_encode_decode_object_id_roundtrip_outbox() -> None:
    encoded = ids.encode_object_id(42, ids.ObjectSource.OUTBOX)
    assert ids.decode_object_id(encoded) == (42, ids.ObjectSource.OUTBOX)


def test_encode_decode_object_id_roundtrip_inbox() -> None:
    encoded = ids.encode_object_id(42, ids.ObjectSource.INBOX)
    assert ids.decode_object_id(encoded) == (42, ids.ObjectSource.INBOX)


def test_outbox_and_inbox_ids_with_same_internal_id_do_not_collide() -> None:
    outbox_id = ids.encode_object_id(1, ids.ObjectSource.OUTBOX)
    inbox_id = ids.encode_object_id(1, ids.ObjectSource.INBOX)
    assert outbox_id != inbox_id


@pytest.mark.parametrize("bogus", ["not-a-number", "-1", "", "1.5"])
def test_decode_object_id_rejects_invalid_input(bogus: str) -> None:
    assert ids.decode_object_id(bogus) is None


def _make_outbox_object() -> OutboxObject:
    follow_id = "outbox-note"
    remote_object = RemoteObject(
        factories.build_note_object(
            from_remote_actor=LOCAL_ACTOR, outbox_public_id=follow_id
        ),
        LOCAL_ACTOR,
    )
    return factories.OutboxObjectFactory.from_remote_object(follow_id, remote_object)


def _make_inbox_object() -> InboxObject:
    ra = factories.RemoteActorFactory(
        base_url="https://example.com", username="toto", public_key="pk"
    )
    actor = factories.ActorFactory.from_remote_actor(ra)
    remote_object = RemoteObject(
        factories.build_note_object(
            from_remote_actor=ra, outbox_public_id="inbox-note"
        ),
        ra,
    )
    return factories.InboxObjectFactory.from_remote_object(remote_object, actor)


@pytest.mark.asyncio
async def test_get_object_by_mastodon_id_resolves_outbox_and_inbox_independently(
    async_db_session: AsyncSession,
) -> None:
    outbox_object = _make_outbox_object()
    inbox_object = _make_inbox_object()

    # Independent PK sequences: both rows get internal id=1.
    assert outbox_object.id == 1
    assert inbox_object.id == 1

    resolved_outbox = await ids.get_object_by_mastodon_id(
        async_db_session, ids.encode_outbox_id(outbox_object)
    )
    resolved_inbox = await ids.get_object_by_mastodon_id(
        async_db_session, ids.encode_inbox_id(inbox_object)
    )

    assert resolved_outbox is not None
    assert resolved_outbox.id == outbox_object.id
    assert resolved_outbox.ap_id == outbox_object.ap_id

    assert resolved_inbox is not None
    assert resolved_inbox.id == inbox_object.id
    assert resolved_inbox.ap_id == inbox_object.ap_id


@pytest.mark.asyncio
async def test_get_object_by_mastodon_id_falls_back_to_other_table(
    async_db_session: AsyncSession,
) -> None:
    # Only an OutboxObject exists (internal id=1); an id encoded as "inbox
    # id=1" has no matching InboxObject row, so it should fall back to the
    # OutboxObject with the same internal id.
    outbox_object = _make_outbox_object()
    assert outbox_object.id is not None
    stray_id = ids.encode_object_id(outbox_object.id, ids.ObjectSource.INBOX)

    resolved = await ids.get_object_by_mastodon_id(async_db_session, stray_id)

    assert resolved is not None
    assert resolved.ap_id == outbox_object.ap_id


@pytest.mark.asyncio
async def test_get_object_by_mastodon_id_returns_none_for_unresolvable_id(
    async_db_session: AsyncSession,
) -> None:
    assert await ids.get_object_by_mastodon_id(async_db_session, "not-a-number") is None
    assert await ids.get_object_by_mastodon_id(async_db_session, "999999") is None


@pytest.mark.asyncio
async def test_get_account_by_mastodon_id(async_db_session: AsyncSession) -> None:
    ra = factories.RemoteActorFactory(
        base_url="https://example.com", username="toto", public_key="pk"
    )
    actor = factories.ActorFactory.from_remote_actor(ra)

    resolved = await ids.get_account_by_mastodon_id(
        async_db_session, ids.encode_account_id(actor)
    )

    assert resolved is not None
    assert resolved.ap_id == actor.ap_id
    assert await ids.get_account_by_mastodon_id(async_db_session, "bogus") is None
