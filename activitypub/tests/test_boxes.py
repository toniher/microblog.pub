import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import activitypub.models
from activitypub import boxes
from activitypub.activitypub import AS_CTX
from activitypub.ap_object import RemoteObject
from activitypub.tests import factories


@pytest.mark.asyncio
async def test_fetch_replies(async_db_session: AsyncSession, respx_mock) -> None:
    # Given a remote note with a replies collection
    root_ra = factories.RemoteActorFactory(
        base_url="https://root.example", username="root", public_key="pk"
    )
    root_actor = factories.ActorFactory.from_remote_actor(root_ra)

    root_note = factories.build_note_object(root_ra)
    replies_url = root_note["id"] + "/replies"
    root_note["replies"] = replies_url
    requested_object = RemoteObject(root_note, actor=root_actor)

    # And a reply from an actor we don't follow
    reply_ra = factories.RemoteActorFactory(
        base_url="https://reply.example", username="replier", public_key="pk2"
    )
    reply_note = factories.build_note_object(
        reply_ra, content="hello back", in_reply_to=root_note["id"]
    )

    respx_mock.get(replies_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "@context": AS_CTX,
                "type": "OrderedCollection",
                "orderedItems": [reply_note],
            },
        )
    )
    respx_mock.get(reply_ra.ap_id).mock(
        return_value=httpx.Response(200, json=reply_ra.ap_actor)
    )
    respx_mock.get(
        "https://reply.example/.well-known/webfinger",
        params={"resource": "acct%3Areplier%40reply.example"},
    ).mock(
        return_value=httpx.Response(200, json={"subject": "acct:replier@reply.example"})
    )

    # When fetching replies for the root note
    fetched_count = await boxes.fetch_replies(async_db_session, requested_object)

    # Then the reply has been saved to the inbox
    assert fetched_count == 1
    saved = (
        await async_db_session.execute(select(activitypub.models.InboxObject))
    ).scalar_one()
    assert saved.ap_id == reply_note["id"]

    # And fetching again is a no-op (already saved)
    fetched_count_again = await boxes.fetch_replies(async_db_session, requested_object)
    assert fetched_count_again == 0


@pytest.mark.asyncio
async def test_fetch_replies_no_replies_collection(
    async_db_session: AsyncSession,
) -> None:
    # Given a remote note with no replies collection advertised
    root_ra = factories.RemoteActorFactory(
        base_url="https://root2.example", username="root2", public_key="pk3"
    )
    root_actor = factories.ActorFactory.from_remote_actor(root_ra)
    root_note = factories.build_note_object(root_ra)
    requested_object = RemoteObject(root_note, actor=root_actor)

    # When fetching replies
    fetched_count = await boxes.fetch_replies(async_db_session, requested_object)

    # Then nothing is fetched
    assert fetched_count == 0
