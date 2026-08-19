import httpx
import pytest
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

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
    # fetch_replies refreshes the root object from its canonical URL first
    respx_mock.get(root_note["id"]).mock(
        return_value=httpx.Response(200, json=root_note)
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
async def test_fetch_replies_caps_new_replies_per_call(
    async_db_session: AsyncSession, respx_mock
) -> None:
    # Given a remote note with 3 replies from actors we've never seen
    root_ra = factories.RemoteActorFactory(
        base_url="https://root3.example", username="root3", public_key="pk4"
    )
    root_actor = factories.ActorFactory.from_remote_actor(root_ra)

    root_note = factories.build_note_object(root_ra)
    replies_url = root_note["id"] + "/replies"
    root_note["replies"] = replies_url
    requested_object = RemoteObject(root_note, actor=root_actor)

    reply_notes: list[dict] = []
    for i in range(3):
        reply_ra = factories.RemoteActorFactory(
            base_url=f"https://reply{i}.example",
            username=f"replier{i}",
            public_key=f"pk-reply-{i}",
        )
        reply_note = factories.build_note_object(
            reply_ra, content=f"reply {i}", in_reply_to=root_note["id"]
        )
        reply_notes.append(reply_note)
        respx_mock.get(reply_ra.ap_id).mock(
            return_value=httpx.Response(200, json=reply_ra.ap_actor)
        )
        respx_mock.get(
            f"https://reply{i}.example/.well-known/webfinger",
            params={"resource": f"acct%3Areplier{i}%40reply{i}.example"},
        ).mock(
            return_value=httpx.Response(
                200, json={"subject": f"acct:replier{i}@reply{i}.example"}
            )
        )

    # Registered after reply_notes is fully populated: httpx.Response(json=...)
    # serializes eagerly, so mocking with a list mutated afterward would bake
    # in an empty response.
    respx_mock.get(replies_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "@context": AS_CTX,
                "type": "OrderedCollection",
                "orderedItems": reply_notes,
            },
        )
    )
    # fetch_replies refreshes the root object from its canonical URL first
    respx_mock.get(root_note["id"]).mock(
        return_value=httpx.Response(200, json=root_note)
    )

    # When fetching replies for the root note
    fetched_count = await boxes.fetch_replies(async_db_session, requested_object)

    # Then only the capped number of new replies is saved
    assert fetched_count == 2
    saved = (
        (await async_db_session.execute(select(activitypub.models.InboxObject)))
        .scalars()
        .all()
    )
    assert len(saved) == 2


@pytest.mark.asyncio
async def test_fetch_replies_no_replies_collection(
    async_db_session: AsyncSession, respx_mock
) -> None:
    # Given a remote note with no replies collection advertised
    root_ra = factories.RemoteActorFactory(
        base_url="https://root2.example", username="root2", public_key="pk3"
    )
    root_actor = factories.ActorFactory.from_remote_actor(root_ra)
    root_note = factories.build_note_object(root_ra)
    requested_object = RemoteObject(root_note, actor=root_actor)
    # fetch_replies refreshes the root object from its canonical URL first,
    # which still doesn't advertise a `replies` collection
    respx_mock.get(root_note["id"]).mock(
        return_value=httpx.Response(200, json=root_note)
    )

    # When fetching replies
    fetched_count = await boxes.fetch_replies(async_db_session, requested_object)

    # Then nothing is fetched
    assert fetched_count == 0


@pytest.mark.asyncio
async def test_fetch_replies_stale_cache_refreshed_from_remote(
    async_db_session: AsyncSession, respx_mock
) -> None:
    # Given a locally cached copy of a remote note captured before it had
    # any replies (e.g. saved via an earlier Like/Announce/reply lookup)
    root_ra = factories.RemoteActorFactory(
        base_url="https://root4.example", username="root4", public_key="pk5"
    )
    root_actor = factories.ActorFactory.from_remote_actor(root_ra)
    stale_root_note = factories.build_note_object(root_ra)
    requested_object = RemoteObject(stale_root_note, actor=root_actor)
    assert "replies" not in requested_object.ap_object

    # And the remote object now advertises a populated replies collection
    fresh_root_note = dict(stale_root_note)
    replies_url = fresh_root_note["id"] + "/replies"
    fresh_root_note["replies"] = replies_url
    respx_mock.get(fresh_root_note["id"]).mock(
        return_value=httpx.Response(200, json=fresh_root_note)
    )

    reply_ra = factories.RemoteActorFactory(
        base_url="https://reply4.example", username="replier4", public_key="pk6"
    )
    reply_note = factories.build_note_object(
        reply_ra, content="hello back", in_reply_to=stale_root_note["id"]
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
        "https://reply4.example/.well-known/webfinger",
        params={"resource": "acct%3Areplier4%40reply4.example"},
    ).mock(
        return_value=httpx.Response(
            200, json={"subject": "acct:replier4@reply4.example"}
        )
    )

    # When fetching replies for the stale cached object
    fetched_count = await boxes.fetch_replies(async_db_session, requested_object)

    # Then the reply is still found and saved, despite the stale cache
    assert fetched_count == 1
    saved = (
        await async_db_session.execute(select(activitypub.models.InboxObject))
    ).scalar_one()
    assert saved.ap_id == reply_note["id"]


@pytest.mark.parametrize(
    "model,index_name",
    [
        (activitypub.models.InboxObject, "ix_inbox_in_reply_to"),
        (activitypub.models.OutboxObject, "ix_outbox_in_reply_to"),
    ],
)
def test_reply_lookup_uses_the_expression_index(
    db: Session,
    model,
    index_name: str,
) -> None:
    """`inReplyTo` is matched with `json_extract`, which SQLite can only serve
    from an expression index when the JSON path is rendered as a *literal* —
    with the path sent as a bound parameter the planner falls back to a full
    table scan (~90ms over a 50k-row inbox). `in_reply_to_expr()` exists to keep
    it a literal; this asserts the planner actually takes the index, so a
    rewrite back to `func.json_extract(col, "$.inReplyTo")` fails here rather
    than quietly regressing.
    """
    stmt = select(model.ap_id).where(
        activitypub.models.in_reply_to_expr(model.ap_object)
        == "http://localhost:8000/o/whatever"
    )
    sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))

    plan = " ".join(
        str(row) for row in db.execute(text("EXPLAIN QUERY PLAN " + sql)).all()
    )

    assert index_name in plan, plan
