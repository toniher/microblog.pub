import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import activitypub.models
from activitypub import activitypub as ap
from activitypub import boxes
from activitypub.ap_object import ObjectType
from activitypub.models import OutboxObject
from app.utils.datetime import now


@pytest.mark.asyncio
async def test_fetch_outbox__empty(async_db_session: AsyncSession) -> None:
    result = await boxes.fetch_outbox(async_db_session)
    assert len(result) == 0


@pytest.mark.asyncio
async def test_fetch_outbox__note(async_db_session: AsyncSession) -> None:
    await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "THIS IS A TEST",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    result = await boxes.fetch_outbox(async_db_session)
    assert len(result) == 1
    assert isinstance(result[0], OutboxObject)
    assert result[0].ap_type == ObjectType.NOTE.value


@pytest.mark.asyncio
async def test_send_create__quote_of_own_post(async_db_session: AsyncSession) -> None:
    _, quoted_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Original post",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    _, quoting_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Check this out",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
        quote_of=quoted_object.ap_id,
    )

    # Quoting our own post is auto-authorized, no round trip needed
    assert quoting_object.quote_ap_id == quoted_object.ap_id
    assert quoting_object.quote_state == "accepted"
    assert quoting_object.quote_authorization_ap_id
    assert quoting_object.ap_object["quote"] == quoted_object.ap_id
    assert (
        quoting_object.ap_object["quoteAuthorization"]
        == quoting_object.quote_authorization_ap_id
    )
    assert "RE:" in quoting_object.ap_object["content"]

    # And a servable stamp was minted, referencing both objects
    stamp = await boxes.get_outbox_object_by_ap_id(
        async_db_session, quoting_object.quote_authorization_ap_id
    )
    assert stamp is not None
    assert stamp.ap_type == "QuoteAuthorization"
    assert stamp.ap_object["interactingObject"] == quoting_object.ap_id
    assert stamp.ap_object["interactionTarget"] == quoted_object.ap_id
    assert stamp.is_hidden_from_homepage is True


def test_quote_reply_link_html_escapes_the_url() -> None:
    # A remote object's `url`/`id` is attacker-controlled: an unescaped quote
    # could break out of the `href` attribute.
    link = boxes._quote_reply_link_html('https://example.com/"><script>1</script>')
    assert "<script>" not in link
    assert '"><script>' not in link
    assert "&quot;&gt;&lt;script&gt;" in link


def test_quote_reply_link_html_rejects_non_http_schemes() -> None:
    # A hostile remote `url` could be a `javascript:` URI rather than http(s).
    assert boxes._quote_reply_link_html("javascript:alert(1)") == ""


@pytest.mark.asyncio
async def test_send_create__quote_of_remote_post_sends_quote_request(
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    # The quote content includes a `RE: <url>` link to the quoted post, on a
    # different host than ours -- send_create's webmention-discovery pass
    # (app.utils.opengraph.external_urls) treats that like any other external
    # link in the content and probes it, so it needs a mock too.
    respx_mock.get("https://example.com/users/alice/notes/1").mock(
        return_value=httpx.Response(200, text="<html></html>")
    )

    remote_actor = activitypub.models.Actor(
        ap_id="https://example.com/users/alice",
        ap_actor={
            "id": "https://example.com/users/alice",
            "type": "Person",
            "inbox": "https://example.com/users/alice/inbox",
            "preferredUsername": "alice",
        },
        ap_type="Person",
    )
    async_db_session.add(remote_actor)
    await async_db_session.flush()

    quoted_ap_id = "https://example.com/users/alice/notes/1"
    remote_note = activitypub.models.InboxObject(
        server="example.com",
        actor_id=remote_actor.id,
        ap_actor_id=remote_actor.ap_id,
        ap_type="Note",
        ap_id=quoted_ap_id,
        ap_context=None,
        ap_published_at=now(),
        ap_object={
            "id": quoted_ap_id,
            "type": "Note",
            "attributedTo": remote_actor.ap_id,
            "content": "hello",
            "to": [ap.AS_PUBLIC],
        },
        visibility=ap.VisibilityEnum.PUBLIC,
        is_hidden_from_stream=False,
    )
    async_db_session.add(remote_note)
    await async_db_session.commit()

    _, quoting_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Check this out",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
        quote_of=quoted_ap_id,
    )

    assert quoting_object.quote_ap_id == quoted_ap_id
    assert quoting_object.quote_state == "pending"
    assert quoting_object.quote_authorization_ap_id is None

    quote_request = (
        await async_db_session.execute(
            select(activitypub.models.OutboxObject).where(
                activitypub.models.OutboxObject.ap_type == "QuoteRequest"
            )
        )
    ).scalar_one()
    assert quote_request.ap_object["object"] == quoted_ap_id
    assert quote_request.ap_object["instrument"] == quoting_object.ap_id
    assert quote_request.relates_to_outbox_object_id == quoting_object.id

    outgoing = (
        await async_db_session.execute(select(activitypub.models.OutgoingActivity))
    ).scalar_one()
    assert outgoing.outbox_object_id == quote_request.id
    assert outgoing.recipient == remote_actor.inbox_url


@pytest.mark.asyncio
async def test_send_quote_revoke(async_db_session: AsyncSession) -> None:
    # Given one of our posts, quoted by a remote actor with a stamp we minted
    _, quoted_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Original post",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    remote_actor = activitypub.models.Actor(
        ap_id="https://example.com/users/alice",
        ap_actor={
            "id": "https://example.com/users/alice",
            "type": "Person",
            "inbox": "https://example.com/users/alice/inbox",
            "preferredUsername": "alice",
        },
        ap_type="Person",
    )
    async_db_session.add(remote_actor)
    await async_db_session.flush()

    quoting_ap_id = "https://example.com/users/alice/notes/2"
    stamp_object = await boxes._mint_quote_authorization(
        async_db_session,
        quoting_object_ap_id=quoting_ap_id,
        quoted_object_ap_id=quoted_object.ap_id,
    )

    quoting_note = activitypub.models.InboxObject(
        server="example.com",
        actor_id=remote_actor.id,
        ap_actor_id=remote_actor.ap_id,
        ap_type="Note",
        ap_id=quoting_ap_id,
        ap_context=None,
        ap_published_at=now(),
        ap_object={
            "id": quoting_ap_id,
            "type": "Note",
            "attributedTo": remote_actor.ap_id,
            "content": "RE: ...",
            "to": [ap.AS_PUBLIC],
            "quote": quoted_object.ap_id,
            "quoteAuthorization": stamp_object.ap_id,
        },
        visibility=ap.VisibilityEnum.PUBLIC,
        is_hidden_from_stream=False,
        quote_ap_id=quoted_object.ap_id,
        quote_authorization_ap_id=stamp_object.ap_id,
        quote_is_verified=True,
    )
    async_db_session.add(quoting_note)
    await async_db_session.flush()

    quoted_object.quotes_count = await boxes._get_quotes_count(
        async_db_session, quoted_object.ap_id
    )
    await async_db_session.commit()
    assert quoted_object.quotes_count == 1

    # When the owner revokes it
    await boxes.send_quote_revoke(async_db_session, quoting_note.ap_id)

    # Then the stamp is torn down
    await async_db_session.refresh(stamp_object)
    assert stamp_object.is_deleted is True

    # And the Delete is publicly addressed (so it gets LD-signed and the
    # quoting server can forward it) and delivered to the quoter alone
    delete_object = (
        await async_db_session.execute(
            select(activitypub.models.OutboxObject).where(
                activitypub.models.OutboxObject.ap_type == "Delete",
                activitypub.models.OutboxObject.relates_to_outbox_object_id
                == stamp_object.id,
            )
        )
    ).scalar_one()
    assert delete_object.ap_object["object"] == {
        "type": "Tombstone",
        "id": stamp_object.ap_id,
    }
    assert delete_object.ap_object["to"] == [ap.AS_PUBLIC]
    assert delete_object.ap_object["cc"] == [remote_actor.ap_id]
    assert delete_object.visibility == ap.VisibilityEnum.PUBLIC

    outgoing = (
        (
            await async_db_session.execute(
                select(activitypub.models.OutgoingActivity).where(
                    activitypub.models.OutgoingActivity.outbox_object_id
                    == delete_object.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(outgoing) == 1
    assert outgoing[0].recipient == remote_actor.inbox_url

    # And the local state reflects the revocation
    await async_db_session.refresh(quoting_note)
    assert quoting_note.quote_is_verified is False

    await async_db_session.refresh(quoted_object)
    assert quoted_object.quotes_count == 0


@pytest.mark.asyncio
async def test_send_quote_revoke_requires_a_local_verified_stamp(
    async_db_session: AsyncSession,
) -> None:
    remote_actor = activitypub.models.Actor(
        ap_id="https://example.com/users/alice",
        ap_actor={
            "id": "https://example.com/users/alice",
            "type": "Person",
            "inbox": "https://example.com/users/alice/inbox",
            "preferredUsername": "alice",
        },
        ap_type="Person",
    )
    async_db_session.add(remote_actor)
    await async_db_session.flush()

    # A quote we never verified (no local stamp minted for it)
    quoting_note = activitypub.models.InboxObject(
        server="example.com",
        actor_id=remote_actor.id,
        ap_actor_id=remote_actor.ap_id,
        ap_type="Note",
        ap_id="https://example.com/users/alice/notes/3",
        ap_context=None,
        ap_published_at=now(),
        ap_object={
            "id": "https://example.com/users/alice/notes/3",
            "type": "Note",
            "attributedTo": remote_actor.ap_id,
            "content": "RE: ...",
            "to": [ap.AS_PUBLIC],
        },
        visibility=ap.VisibilityEnum.PUBLIC,
        is_hidden_from_stream=False,
        quote_is_verified=False,
    )
    async_db_session.add(quoting_note)
    await async_db_session.commit()

    with pytest.raises(ValueError):
        await boxes.send_quote_revoke(async_db_session, quoting_note.ap_id)


@pytest.mark.asyncio
async def test_compute_all_known_recipients(async_db_session: AsyncSession) -> None:
    def _actor(name: str, endpoints: object, is_deleted: bool = False):
        ap_id = f"https://{name}.example/u"
        return activitypub.models.Actor(
            ap_id=ap_id,
            ap_type="Person",
            is_deleted=is_deleted,
            ap_actor={"id": ap_id, "inbox": ap_id + "/inbox", "endpoints": endpoints},
        )

    async_db_session.add_all(
        [
            _actor("shared", {"sharedInbox": "https://shared.example/inbox"}),
            _actor("shared2", {"sharedInbox": "https://shared.example/inbox"}),
            _actor("null", None),
            _actor("empty", {"sharedInbox": ""}),
            _actor("deleted", {}, is_deleted=True),
        ]
    )
    await async_db_session.commit()

    assert await boxes.compute_all_known_recipients(async_db_session) == {
        "https://shared.example/inbox",
        "https://null.example/u/inbox",
        "https://empty.example/u/inbox",
    }


@pytest.mark.asyncio
async def test_send_update__resolves_recipients_before_writing(
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    # An audience member not in the actor cache gets fetched on edit: that
    # must happen before `send_update` writes (autouse
    # `no_network_while_writing` guard).
    from activitypub.tests import factories

    _, pubkey = factories.generate_key()
    ra = factories.RemoteActorFactory(
        base_url="https://remote.example", username="bob", public_key=pubkey
    )
    respx_mock.get(ra.ap_id).mock(return_value=httpx.Response(200, json=ra.ap_actor))
    respx_mock.get("https://remote.example/.well-known/webfinger").mock(
        return_value=httpx.Response(404)
    )

    _, outbox_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "first",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    outbox_object.ap_object = {
        **outbox_object.ap_object,
        "cc": [*outbox_object.ap_object["cc"], ra.ap_id],
    }
    await async_db_session.commit()

    await boxes.send_update(async_db_session, outbox_object.ap_id, "edited", uploads=[])

    # Not in the note's audience at creation: queued by the edit
    recipients = (
        await async_db_session.scalars(
            select(activitypub.models.OutgoingActivity.recipient)
        )
    ).all()
    assert ra.inbox_url in recipients


async def _stale_cached_actor(
    db_session: AsyncSession, respx_mock: respx.MockRouter, host: str
):
    from datetime import timedelta

    from activitypub.tests import factories

    _, pubkey = factories.generate_key()
    ra = factories.RemoteActorFactory(
        base_url=f"https://{host}", username="bob", public_key=pubkey
    )
    respx_mock.get(ra.ap_id).mock(return_value=httpx.Response(200, json=ra.ap_actor))
    respx_mock.get(f"https://{host}/.well-known/webfinger").mock(
        return_value=httpx.Response(404)
    )
    actor = activitypub.models.Actor(
        ap_id=ra.ap_id,
        ap_type="Person",
        ap_actor=ra.ap_actor,
        handle=f"@bob@{host}",
        updated_at=now() - timedelta(days=2),
    )
    db_session.add(actor)
    await db_session.commit()
    return ra


@pytest.mark.asyncio
async def test_send_undo_follow__refreshes_stale_actor_before_writing(
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = await _stale_cached_actor(async_db_session, respx_mock, "stale.example")
    follow = activitypub.models.OutboxObject(
        public_id="follow-1",
        ap_type="Follow",
        ap_id=f"{boxes.ID}/o/follow-1",
        ap_object={
            "type": "Follow",
            "id": f"{boxes.ID}/o/follow-1",
            "actor": boxes.ID,
            "object": ra.ap_id,
        },
        visibility=ap.VisibilityEnum.PUBLIC,
        activity_object_ap_id=ra.ap_id,
    )
    async_db_session.add(follow)
    await async_db_session.commit()

    await boxes.send_undo(async_db_session, follow.ap_id)

    recipients = (
        await async_db_session.scalars(
            select(activitypub.models.OutgoingActivity.recipient)
        )
    ).all()
    assert ra.inbox_url in recipients


@pytest.mark.asyncio
async def test_send_delete__resolves_recipients_before_writing(
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    from activitypub.tests import factories

    _, pubkey = factories.generate_key()
    ra = factories.RemoteActorFactory(
        base_url="https://remote.example", username="bob", public_key=pubkey
    )
    respx_mock.get(ra.ap_id).mock(return_value=httpx.Response(200, json=ra.ap_actor))
    respx_mock.get("https://remote.example/.well-known/webfinger").mock(
        return_value=httpx.Response(404)
    )
    _, note = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "to be deleted",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    note.ap_object = {**note.ap_object, "cc": [*note.ap_object["cc"], ra.ap_id]}
    await async_db_session.commit()

    await boxes.send_delete(async_db_session, note.ap_id)

    recipients = (
        await async_db_session.scalars(
            select(activitypub.models.OutgoingActivity.recipient)
        )
    ).all()
    assert ra.inbox_url in recipients


@pytest.mark.asyncio
async def test_send_update__callers_pending_changes_not_flushed_before_network(
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    # The admin edit sets `alias` before calling `send_update`.
    from activitypub.tests import factories

    _, pubkey = factories.generate_key()
    ra = factories.RemoteActorFactory(
        base_url="https://remote.example", username="bob", public_key=pubkey
    )
    respx_mock.get(ra.ap_id).mock(return_value=httpx.Response(200, json=ra.ap_actor))
    respx_mock.get("https://remote.example/.well-known/webfinger").mock(
        return_value=httpx.Response(404)
    )
    _, note = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "first",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    note.ap_object = {**note.ap_object, "cc": [*note.ap_object["cc"], ra.ap_id]}
    await async_db_session.commit()

    note.alias = "new-alias"
    await boxes.send_update(async_db_session, note.ap_id, "edited")

    assert note.alias == "new-alias"
    assert note.url and note.url.endswith("new-alias")
