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
from app import config
from app import models
from app.mastodon import ids
from tests.utils import setup_remote_actor
from tests.utils import setup_remote_actor_as_follower
from tests.utils import setup_remote_actor_as_following
from tests.utils import setup_remote_actor_as_following_and_follower


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


def test_accounts_show_owner(client: TestClient) -> None:
    response = client.get(f"/api/v1/accounts/{ids.LOCAL_ACTOR_ID}")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == ids.LOCAL_ACTOR_ID
    assert data["username"] == config.USERNAME
    assert data["acct"] == config.USERNAME


def test_accounts_show_remote_actor(
    client: TestClient, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)

    response = client.get(f"/api/v1/accounts/{ids.encode_account_id(follower.actor)}")

    assert response.status_code == 200
    data = response.json()
    assert data["username"] == "toto"
    assert data["acct"] == "toto@example.com"


def test_accounts_show_not_found(client: TestClient) -> None:
    response = client.get("/api/v1/accounts/999999")
    assert response.status_code == 404


def test_accounts_verify_credentials_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/accounts/verify_credentials")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_accounts_verify_credentials(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read:accounts")

    response = client.get(
        "/api/v1/accounts/verify_credentials",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == ids.LOCAL_ACTOR_ID
    assert "source" in data
    assert data["source"]["privacy"] == "public"


def test_accounts_followers_for_owner(
    client: TestClient, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)

    response = client.get(f"/api/v1/accounts/{ids.LOCAL_ACTOR_ID}/followers")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["id"] == ids.encode_account_id(follower.actor)


def test_accounts_followers_hidden_when_configured(
    client: TestClient,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "HIDES_FOLLOWERS", True)
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    setup_remote_actor_as_follower(ra)

    response = client.get(f"/api/v1/accounts/{ids.LOCAL_ACTOR_ID}/followers")

    assert response.status_code == 200
    assert response.json() == []


def test_accounts_following_for_owner(
    client: TestClient, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    following = setup_remote_actor_as_following(ra)

    response = client.get(f"/api/v1/accounts/{ids.LOCAL_ACTOR_ID}/following")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert following.actor is not None
    assert data[0]["id"] == ids.encode_account_id(following.actor)


def test_accounts_followers_for_remote_actor_is_empty_not_error(
    client: TestClient, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)

    response = client.get(
        f"/api/v1/accounts/{ids.encode_account_id(follower.actor)}/followers"
    )

    assert response.status_code == 200
    assert response.json() == []


def test_accounts_followers_for_unknown_actor_404s(client: TestClient) -> None:
    response = client.get("/api/v1/accounts/999999/followers")
    assert response.status_code == 404


def test_accounts_relationships_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/accounts/relationships?id[]=0")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_accounts_relationships(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    following, follower = setup_remote_actor_as_following_and_follower(ra)
    assert following.actor is not None
    remote_id = ids.encode_account_id(following.actor)

    token = await _make_access_token(async_db_session, "read:accounts")

    response = client.get(
        f"/api/v1/accounts/relationships?id[]={ids.LOCAL_ACTOR_ID}&id[]={remote_id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = {rel["id"]: rel for rel in response.json()}

    assert data[ids.LOCAL_ACTOR_ID]["following"] is False
    assert data[remote_id]["following"] is True
    assert data[remote_id]["followed_by"] is True


def test_accounts_familiar_followers_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/accounts/familiar_followers?id[]=0")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_accounts_familiar_followers(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    # Regression test: /api/v1/accounts/{account_id} is registered with a
    # dynamic path and previously swallowed this route (account_id=
    # "familiar_followers"), 404ing here instead of matching this handler.
    token = await _make_access_token(async_db_session, "read:accounts")

    response = client.get(
        "/api/v1/accounts/familiar_followers?id[]=0&id[]=1",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data == [
        {"id": "0", "accounts": []},
        {"id": "1", "accounts": []},
    ]


def test_accounts_lookup_owner_bare_username(client: TestClient) -> None:
    response = client.get(f"/api/v1/accounts/lookup?acct={config.USERNAME}")
    assert response.status_code == 200
    assert response.json()["id"] == ids.LOCAL_ACTOR_ID


def test_accounts_lookup_owner_full_acct(client: TestClient) -> None:
    response = client.get(
        f"/api/v1/accounts/lookup?acct={config.USERNAME}@{config.WEBFINGER_DOMAIN}"
    )
    assert response.status_code == 200
    assert response.json()["id"] == ids.LOCAL_ACTOR_ID


def test_accounts_lookup_remote_actor(
    client: TestClient, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)

    response = client.get("/api/v1/accounts/lookup?acct=toto@example.com")

    assert response.status_code == 200
    assert response.json()["id"] == ids.encode_account_id(follower.actor)


def test_accounts_lookup_not_found(client: TestClient) -> None:
    response = client.get("/api/v1/accounts/lookup?acct=nobody@nowhere.example")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_accounts_statuses_owner(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    _, outbox_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "My post",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    response = client.get(f"/api/v1/accounts/{ids.LOCAL_ACTOR_ID}/statuses")

    assert response.status_code == 200
    returned_ids = [status["id"] for status in response.json()]
    assert ids.encode_outbox_id(outbox_object) in returned_ids


def test_accounts_statuses_remote_actor(
    client: TestClient, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    remote_note = RemoteObject(
        factories.build_note_object(from_remote_actor=ra, content="From them"),
        ra,
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        remote_note, follower.actor
    )

    response = client.get(
        f"/api/v1/accounts/{ids.encode_account_id(follower.actor)}/statuses"
    )

    assert response.status_code == 200
    returned_ids = [status["id"] for status in response.json()]
    assert ids.encode_inbox_id(inbox_object) in returned_ids


def test_accounts_statuses_unknown_actor_404s(client: TestClient) -> None:
    response = client.get("/api/v1/accounts/999999/statuses")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_accounts_statuses_owner_hides_non_public_from_anonymous_callers(
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
    _, direct_post = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Direct message",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.DIRECT,
    )

    anonymous_response = client.get(f"/api/v1/accounts/{ids.LOCAL_ACTOR_ID}/statuses")
    anonymous_ids = {status["id"] for status in anonymous_response.json()}
    assert ids.encode_outbox_id(public_post) in anonymous_ids
    assert ids.encode_outbox_id(direct_post) not in anonymous_ids

    token = await _make_access_token(async_db_session, "read:statuses")
    admin_response = client.get(
        f"/api/v1/accounts/{ids.LOCAL_ACTOR_ID}/statuses",
        headers={"Authorization": f"Bearer {token}"},
    )
    admin_ids = {status["id"] for status in admin_response.json()}
    assert ids.encode_outbox_id(direct_post) in admin_ids


@pytest.mark.asyncio
async def test_statuses_favourited_by_404s_for_private_status(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    _, private_post = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Followers only",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.FOLLOWERS_ONLY,
    )
    status_id = ids.encode_outbox_id(private_post)

    favourited_by = client.get(f"/api/v1/statuses/{status_id}/favourited_by")
    assert favourited_by.status_code == 404

    reblogged_by = client.get(f"/api/v1/statuses/{status_id}/reblogged_by")
    assert reblogged_by.status_code == 404

    token = await _make_access_token(async_db_session, "read:statuses")
    authorized = client.get(
        f"/api/v1/statuses/{status_id}/favourited_by",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert authorized.status_code == 200
