import secrets

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from activitypub import activitypub as ap
from activitypub import boxes
from activitypub.actor import LOCAL_ACTOR
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


@pytest.mark.asyncio
async def test_follow_and_unfollow(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)
    account_id = ids.encode_account_id(actor)

    token = await _make_access_token(async_db_session, "write:follows")
    headers = {"Authorization": f"Bearer {token}"}

    followed = client.post(
        f"/api/v1/accounts/{account_id}/follow", headers=headers
    ).json()
    assert followed["id"] == account_id
    # Not accepted yet (no Accept received) — following stays false until
    # get_actors_metadata sees a Follower/Following row, which only exists
    # post-acceptance; the important thing is the request didn't error.
    assert "following" in followed

    unfollowed = client.post(
        f"/api/v1/accounts/{account_id}/unfollow", headers=headers
    ).json()
    assert unfollowed["id"] == account_id


@pytest.mark.asyncio
async def test_unfollow_when_not_following_is_noop(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)
    account_id = ids.encode_account_id(actor)

    token = await _make_access_token(async_db_session, "write:follows")
    response = client.post(
        f"/api/v1/accounts/{account_id}/unfollow",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200


def test_follow_requires_auth(client: TestClient) -> None:
    response = client.post("/api/v1/accounts/999999/follow")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_cannot_follow_self(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:follows")
    response = client.post(
        f"/api/v1/accounts/{ids.LOCAL_ACTOR_ID}/follow",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_block_and_unblock(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)
    account_id = ids.encode_account_id(actor)

    token = await _make_access_token(async_db_session, "write:blocks")
    headers = {"Authorization": f"Bearer {token}"}

    blocked = client.post(
        f"/api/v1/accounts/{account_id}/block", headers=headers
    ).json()
    assert blocked["blocking"] is True

    unblocked = client.post(
        f"/api/v1/accounts/{account_id}/unblock", headers=headers
    ).json()
    assert unblocked["blocking"] is False


@pytest.mark.asyncio
async def test_mute_and_unmute_are_noops(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)
    account_id = ids.encode_account_id(actor)

    token = await _make_access_token(async_db_session, "write:mutes")
    headers = {"Authorization": f"Bearer {token}"}

    muted = client.post(f"/api/v1/accounts/{account_id}/mute", headers=headers).json()
    assert muted["muting"] is False

    unmuted = client.post(
        f"/api/v1/accounts/{account_id}/unmute", headers=headers
    ).json()
    assert unmuted["muting"] is False


@pytest.mark.asyncio
async def test_account_note_echoes_comment(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)
    account_id = ids.encode_account_id(actor)

    token = await _make_access_token(async_db_session, "write:accounts")
    response = client.post(
        f"/api/v1/accounts/{account_id}/note",
        headers={"Authorization": f"Bearer {token}"},
        data={"comment": "met them at a conference"},
    )

    assert response.status_code == 200
    assert response.json()["note"] == "met them at a conference"


@pytest.mark.asyncio
async def test_follow_requests_list_and_authorize(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)

    follow_activity = RemoteObject(
        factories.build_follow_activity(
            from_remote_actor=ra, for_remote_actor=LOCAL_ACTOR
        ),
        ra,
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        follow_activity, actor
    )
    notif = models.Notification(
        notification_type=models.NotificationType.PENDING_INCOMING_FOLLOWER,
        actor_id=actor.id,
        inbox_object_id=inbox_object.id,
    )
    async_db_session.add(notif)
    await async_db_session.commit()

    account_id = ids.encode_account_id(actor)
    token = await _make_access_token(async_db_session, "read:follows write:follows")
    headers = {"Authorization": f"Bearer {token}"}

    listed = client.get("/api/v1/follow_requests", headers=headers).json()
    assert account_id in {a["id"] for a in listed}

    authorized = client.post(
        f"/api/v1/follow_requests/{account_id}/authorize", headers=headers
    )
    assert authorized.status_code == 200

    listed_after = client.get("/api/v1/follow_requests", headers=headers).json()
    assert account_id not in {a["id"] for a in listed_after}


@pytest.mark.asyncio
async def test_follow_requests_reject(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)

    follow_activity = RemoteObject(
        factories.build_follow_activity(
            from_remote_actor=ra, for_remote_actor=LOCAL_ACTOR
        ),
        ra,
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        follow_activity, actor
    )
    notif = models.Notification(
        notification_type=models.NotificationType.PENDING_INCOMING_FOLLOWER,
        actor_id=actor.id,
        inbox_object_id=inbox_object.id,
    )
    async_db_session.add(notif)
    await async_db_session.commit()

    account_id = ids.encode_account_id(actor)
    token = await _make_access_token(async_db_session, "write:follows")

    response = client.post(
        f"/api/v1/follow_requests/{account_id}/reject",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_follow_requests_authorize_not_found(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    account_id = ids.encode_account_id(follower.actor)

    token = await _make_access_token(async_db_session, "write:follows")
    response = client.post(
        f"/api/v1/follow_requests/{account_id}/authorize",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404


def test_search_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v2/search?q=hello")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_search_requires_query(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read:search")
    response = client.get(
        "/api/v2/search", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_search_local_accounts(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)

    token = await _make_access_token(async_db_session, "read:search")
    response = client.get(
        "/api/v2/search?q=toto&type=accounts",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["statuses"] == []
    assert data["hashtags"] == []
    assert ids.encode_account_id(actor) in {a["id"] for a in data["accounts"]}


@pytest.mark.asyncio
async def test_search_local_statuses(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    _, outbox_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "A very unique searchable phrase",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    token = await _make_access_token(async_db_session, "read:search")
    response = client.get(
        "/api/v2/search?q=unique+searchable&type=statuses",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert ids.encode_outbox_id(outbox_object) in {s["id"] for s in data["statuses"]}


@pytest.mark.asyncio
async def test_search_hashtags_stub(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read:search")
    response = client.get(
        "/api/v2/search?q=%23microblogging&type=hashtags",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["hashtags"] == [
        {
            "name": "microblogging",
            "url": response.json()["hashtags"][0]["url"],
            "history": [],
        }
    ]
