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
