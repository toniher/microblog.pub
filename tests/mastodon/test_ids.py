from datetime import datetime
from datetime import timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from activitypub.actor import LOCAL_ACTOR
from activitypub.ap_object import RemoteObject
from activitypub.models import InboxObject
from activitypub.models import OutboxObject
from activitypub.tests import factories
from app.mastodon import ids

_DT = datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_encode_decode_object_id_roundtrip_outbox() -> None:
    encoded = ids.encode_object_id(42, ids.ObjectSource.OUTBOX, _DT)
    assert ids.decode_object_id(encoded) == (42, ids.ObjectSource.OUTBOX)


def test_encode_decode_object_id_roundtrip_inbox() -> None:
    encoded = ids.encode_object_id(42, ids.ObjectSource.INBOX, _DT)
    assert ids.decode_object_id(encoded) == (42, ids.ObjectSource.INBOX)


def test_outbox_and_inbox_ids_with_same_internal_id_do_not_collide() -> None:
    outbox_id = ids.encode_object_id(1, ids.ObjectSource.OUTBOX, _DT)
    inbox_id = ids.encode_object_id(1, ids.ObjectSource.INBOX, _DT)
    assert outbox_id != inbox_id


@pytest.mark.parametrize("bogus", ["not-a-number", "-1", "", "1.5"])
def test_decode_object_id_rejects_invalid_input(bogus: str) -> None:
    assert ids.decode_object_id(bogus) is None


@pytest.mark.parametrize("source", [ids.ObjectSource.OUTBOX, ids.ObjectSource.INBOX])
@pytest.mark.parametrize("rowid", [1, 2, 1000, (1 << 30) - 1])
def test_encode_decode_object_id_roundtrip_across_rowids_and_timestamps(
    rowid: int, source: "ids.ObjectSource"
) -> None:
    for dt in (
        datetime(1970, 1, 1, tzinfo=timezone.utc),
        datetime(2020, 6, 15, 12, 30, 45, tzinfo=timezone.utc),
        datetime(2106, 2, 1, tzinfo=timezone.utc),
    ):
        encoded = ids.encode_object_id(rowid, source, dt)
        assert ids.decode_object_id(encoded) == (rowid, source)


def test_encode_object_id_is_monotonic_with_published_at() -> None:
    earlier = ids.encode_object_id(1, ids.ObjectSource.OUTBOX, _DT)
    later = ids.encode_object_id(
        1, ids.ObjectSource.OUTBOX, datetime(2024, 1, 2, tzinfo=timezone.utc)
    )
    assert ids.mastodon_id_int(later) > ids.mastodon_id_int(earlier)

    # Cross-source: a later timestamp still wins regardless of table.
    later_inbox = ids.encode_object_id(
        1, ids.ObjectSource.INBOX, datetime(2024, 1, 2, tzinfo=timezone.utc)
    )
    assert ids.mastodon_id_int(later_inbox) > ids.mastodon_id_int(earlier)


def test_encode_object_id_is_monotonic_with_rowid_within_same_second() -> None:
    lower = ids.encode_object_id(1, ids.ObjectSource.OUTBOX, _DT)
    higher = ids.encode_object_id(2, ids.ObjectSource.OUTBOX, _DT)
    assert ids.mastodon_id_int(higher) > ids.mastodon_id_int(lower)


def test_encode_object_id_is_stable() -> None:
    assert ids.encode_object_id(7, ids.ObjectSource.INBOX, _DT) == ids.encode_object_id(
        7, ids.ObjectSource.INBOX, _DT
    )


def test_decode_object_id_is_backward_compatible_with_old_format() -> None:
    # The pre-timestamp scheme was `str(internal_id * 2 + source)`; those ids
    # must still decode to the same (rowid, source), since they're the
    # zero-seconds-prefix subset of the new bit layout.
    old_format_id = str(42 * 2 + int(ids.ObjectSource.OUTBOX))
    assert ids.decode_object_id(old_format_id) == (42, ids.ObjectSource.OUTBOX)


def test_encode_object_id_rejects_rowid_over_ceiling() -> None:
    with pytest.raises(ValueError):
        ids.encode_object_id(1 << 30, ids.ObjectSource.OUTBOX, _DT)


def test_encode_object_id_rejects_seconds_over_ceiling() -> None:
    with pytest.raises(ValueError):
        ids.encode_object_id(
            1, ids.ObjectSource.OUTBOX, datetime(2200, 1, 1, tzinfo=timezone.utc)
        )


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
    stray_id = ids.encode_object_id(outbox_object.id, ids.ObjectSource.INBOX, _DT)

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
