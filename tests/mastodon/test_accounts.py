import secrets

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

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
