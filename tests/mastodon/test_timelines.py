import secrets

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from activitypub import activitypub as ap
from activitypub import boxes
from activitypub.ap_object import ObjectType
from activitypub.ap_object import RemoteObject
from activitypub.tests import factories
from app import models
from app.mastodon import ids
from tests.utils import setup_remote_actor
from tests.utils import setup_remote_actor_as_follower


async def _make_access_token(db_session: AsyncSession, scope: str) -> str:
    token = models.IndieAuthAccessToken(
        access_token=secrets.token_urlsafe(16),
        refresh_token=None,
        expires_in=3600,
        scope=scope,
    )
    db_session.add(token)
    await db_session.commit()
    return token.access_token


def test_timelines_home_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/timelines/home")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_timelines_home_merges_own_posts_and_followed_notes(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    _, own_post = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "My own post",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    remote_note = RemoteObject(
        factories.build_note_object(from_remote_actor=ra, content="Followed note"),
        ra,
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        remote_note, follower.actor
    )

    token = await _make_access_token(async_db_session, "read:statuses")
    response = client.get(
        "/api/v1/timelines/home", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert {status["id"] for status in response.json()} == {
        ids.encode_outbox_id(own_post),
        ids.encode_inbox_id(inbox_object),
    }


@pytest.mark.asyncio
async def test_timelines_home_excludes_replies(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    _, root_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Root",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    _, reply_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "A reply",
        uploads=[],
        in_reply_to=root_object.ap_id,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    token = await _make_access_token(async_db_session, "read:statuses")
    response = client.get(
        "/api/v1/timelines/home", headers={"Authorization": f"Bearer {token}"}
    )

    returned_ids = {status["id"] for status in response.json()}
    assert ids.encode_outbox_id(root_object) in returned_ids
    assert ids.encode_outbox_id(reply_object) not in returned_ids


@pytest.mark.asyncio
async def test_timelines_public_local_only(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    _, public_post = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Public post",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    _, unlisted_post = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Unlisted post",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.UNLISTED,
    )

    response = client.get("/api/v1/timelines/public?local=true")

    assert response.status_code == 200
    returned_ids = [status["id"] for status in response.json()]
    assert ids.encode_outbox_id(public_post) in returned_ids
    assert ids.encode_outbox_id(unlisted_post) not in returned_ids


@pytest.mark.asyncio
async def test_timelines_public_federated_includes_remote_public_notes(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    remote_note = RemoteObject(
        factories.build_note_object(
            from_remote_actor=ra,
            content="Public federated note",
            to=[ap.AS_PUBLIC],
        ),
        ra,
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        remote_note, follower.actor
    )
    assert inbox_object.visibility == ap.VisibilityEnum.PUBLIC

    response = client.get("/api/v1/timelines/public")

    assert response.status_code == 200
    returned_ids = {status["id"] for status in response.json()}
    assert ids.encode_inbox_id(inbox_object) in returned_ids


@pytest.mark.asyncio
async def test_timelines_tag_filters_by_hashtag(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    _, tagged_post = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Post about #microblogging",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    _, untagged_post = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Just a regular post",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    response = client.get("/api/v1/timelines/tag/microblogging")

    assert response.status_code == 200
    returned_ids = {status["id"] for status in response.json()}
    assert ids.encode_outbox_id(tagged_post) in returned_ids
    assert ids.encode_outbox_id(untagged_post) not in returned_ids


@pytest.mark.asyncio
async def test_timelines_home_pagination_max_id(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    _, first_post = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "First",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    _, second_post = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Second",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    token = await _make_access_token(async_db_session, "read:statuses")
    headers = {"Authorization": f"Bearer {token}"}

    full_page = client.get("/api/v1/timelines/home", headers=headers).json()
    assert [s["id"] for s in full_page] == [
        ids.encode_outbox_id(second_post),
        ids.encode_outbox_id(first_post),
    ]

    older_page = client.get(
        f"/api/v1/timelines/home?max_id={ids.encode_outbox_id(second_post)}",
        headers=headers,
    ).json()
    assert [s["id"] for s in older_page] == [ids.encode_outbox_id(first_post)]


@pytest.mark.asyncio
async def test_timelines_home_ids_sort_descending_across_inbox_and_outbox(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    # Regression test for the id-monotonicity bug: InboxObject/OutboxObject
    # have independent PK sequences, so interleaved inserts used to produce
    # ids like [5, 6, 4, 2] once merged by publish time — not descending, even
    # though the array itself was correctly ordered by publish time. Ids are
    # now timestamp-prefixed so id order always matches array order.
    _, first_post = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "First own post",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    remote_note = RemoteObject(
        factories.build_note_object(from_remote_actor=ra, content="Followed note"),
        ra,
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        remote_note, follower.actor
    )

    _, second_post = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Second own post",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    token = await _make_access_token(async_db_session, "read:statuses")
    response = client.get(
        "/api/v1/timelines/home", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    data = response.json()
    returned_ids = {status["id"] for status in data}
    assert returned_ids == {
        ids.encode_outbox_id(first_post),
        ids.encode_inbox_id(inbox_object),
        ids.encode_outbox_id(second_post),
    }
    numeric_ids = [int(status["id"]) for status in data]
    assert numeric_ids == sorted(numeric_ids, reverse=True)


@pytest.mark.asyncio
async def test_timelines_home_coerces_null_sensitive_to_bool(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    # Some AP servers send an explicit `"sensitive": null`. If serialized as
    # `null`, strict Mastodon clients (Tusky/Fedilab) fail to deserialize the
    # non-null boolean and silently drop the entire timeline page.
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    note_data = factories.build_note_object(
        from_remote_actor=ra,
        content="Explicit null sensitive",
        to=[ap.AS_PUBLIC],
    )
    note_data["sensitive"] = None
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        RemoteObject(note_data, ra), follower.actor
    )

    token = await _make_access_token(async_db_session, "read:statuses")
    response = client.get(
        "/api/v1/timelines/home",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    status = next(
        s for s in response.json() if s["id"] == ids.encode_inbox_id(inbox_object)
    )
    assert status["sensitive"] is False


@pytest.mark.asyncio
async def test_timelines_home_serializes_reblog_target_url_list_with_strings(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    remote_note_data = factories.build_note_object(
        from_remote_actor=ra,
        content="Boosted note",
    )
    remote_note_data["url"] = [
        remote_note_data["url"],
        {
            "type": "Link",
            "href": remote_note_data["url"] + "/html",
            "mediaType": "text/html",
        },
    ]
    remote_note = RemoteObject(remote_note_data, ra)
    factories.InboxObjectFactory.from_remote_object(remote_note, follower.actor)

    reblog = RemoteObject(
        {
            "@context": ap.AS_CTX,
            "type": "Announce",
            "id": f"{ra.ap_id}/announce/with-string-url-list",
            "actor": ra.ap_id,
            "object": remote_note.ap_id,
            "to": [ap.AS_PUBLIC],
            "cc": [],
            "published": remote_note_data["published"],
            "url": f"{ra.ap_id}/announce/with-string-url-list",
        },
        ra,
    )
    factories.InboxObjectFactory.from_remote_object(reblog, follower.actor)

    token = await _make_access_token(async_db_session, "read:statuses")
    response = client.get(
        "/api/v1/timelines/home",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert any(
        status["reblog"] is not None
        and status["reblog"]["url"] == remote_note_data["url"][0]
        for status in response.json()
    )


@pytest.mark.asyncio
async def test_timelines_home_serializes_reblog_target_dict_in_reply_to(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    root_note = RemoteObject(
        factories.build_note_object(
            from_remote_actor=ra,
            content="Root remote note",
        ),
        ra,
    )
    root_inbox_object = factories.InboxObjectFactory.from_remote_object(
        root_note, follower.actor
    )

    reply_note_data = factories.build_note_object(
        from_remote_actor=ra,
        content="Reply remote note",
    )
    reply_note_data["inReplyTo"] = {"id": root_note.ap_id}
    reply_note = RemoteObject(reply_note_data, ra)
    factories.InboxObjectFactory.from_remote_object(reply_note, follower.actor)

    reblog = RemoteObject(
        {
            "@context": ap.AS_CTX,
            "type": "Announce",
            "id": f"{ra.ap_id}/announce/with-dict-in-reply-to",
            "actor": ra.ap_id,
            "object": reply_note.ap_id,
            "to": [ap.AS_PUBLIC],
            "cc": [],
            "published": reply_note_data["published"],
            "url": f"{ra.ap_id}/announce/with-dict-in-reply-to",
        },
        ra,
    )
    factories.InboxObjectFactory.from_remote_object(reblog, follower.actor)

    token = await _make_access_token(async_db_session, "read:statuses")
    response = client.get(
        "/api/v1/timelines/home",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert any(
        status["reblog"] is not None
        and status["reblog"]["in_reply_to_id"] == ids.encode_inbox_id(root_inbox_object)
        for status in response.json()
    )
