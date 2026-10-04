"""Mastodon 4.6 surface (api_versions 8-10): new entity keys, `exclude_direct`,
and the read-only stubs for profile, collections and annual reports."""

import secrets
from datetime import timedelta

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from activitypub.tests import factories
from app import models
from app.mastodon import ids
from app.utils.datetime import now
from tests.utils import setup_remote_actor


async def _headers(db_session: AsyncSession, scope: str) -> dict[str, str]:
    token = models.IndieAuthAccessToken(
        access_token=secrets.token_urlsafe(16),
        refresh_token=None,
        expires_in=3600,
        scope=scope,
    )
    db_session.add(token)
    await db_session.commit()
    return {"Authorization": f"Bearer {token.access_token}"}


def test_instance_v2_46_fields(client: TestClient) -> None:
    body = client.get("/api/v2/instance").json()
    assert body["version"].startswith("4.6.0 ")
    assert body["wrapstodon"] is None
    assert body["configuration"]["accounts"]["max_display_name_length"] == 40
    # Required by Tusky's instance model.
    assert body["configuration"]["accounts"]["max_featured_tags"] == 10


@pytest.mark.asyncio
async def test_account_has_46_keys(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    headers = await _headers(async_db_session, "read:accounts")
    account = client.get("/api/v1/accounts/verify_credentials", headers=headers).json()
    assert account["avatar_description"] == ""
    assert account["show_media"] is True
    assert account["feature_approval"]["current_user"] == "denied"


@pytest.mark.asyncio
async def test_relationship_muting_expires_at(
    client: TestClient,
    db: Session,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)
    account_id = ids.encode_account_id(actor)
    headers = await _headers(async_db_session, "read:accounts read:mutes write:mutes")

    client.post(
        f"/api/v1/accounts/{account_id}/mute", headers=headers, data={"duration": "60"}
    )
    rel = client.get(
        "/api/v1/accounts/relationships", params={"id[]": account_id}, headers=headers
    ).json()[0]
    assert rel["muting"] is True
    assert rel["muting_expires_at"] is not None

    actor.muted_until = now() - timedelta(seconds=1)
    db.commit()
    rel = client.get(
        "/api/v1/accounts/relationships", params={"id[]": account_id}, headers=headers
    ).json()[0]
    assert rel["muting_expires_at"] is None


@pytest.mark.asyncio
async def test_stubs(client: TestClient, async_db_session: AsyncSession) -> None:
    h = await _headers(async_db_session, "read write")

    assert client.get("/api/v1/profile", headers=h).json()["show_media"] is True
    assert client.patch("/api/v1/profile", headers=h).status_code == 422
    refused = client.put("/api/v1/profile", headers=h)
    assert refused.status_code == 422
    assert refused.json()["error"].startswith("Validation failed: ")
    assert client.delete("/api/v1/profile/avatar", headers=h).status_code == 422

    assert client.get("/api/v1/accounts/anything/collections").json() == {
        "collections": []
    }
    assert client.get("/api/v1/accounts/anything/in_collections", headers=h).json() == {
        "collections": []
    }
    assert client.get("/api/v1/collections/1").status_code == 404
    assert client.post("/api/v1/collections", headers=h).status_code == 422

    assert (
        client.get("/api/v1/annual_reports", headers=h).json()["annual_reports"] == []
    )
    assert (
        client.get("/api/v1/annual_reports/2025/state", headers=h).json()["state"]
        == "ineligible"
    )
    assert client.get("/api/v1/annual_reports/2025", headers=h).status_code == 404

    own = f"/api/v1/accounts/{ids.LOCAL_ACTOR_ID}/statuses"
    assert client.get(own, params={"exclude_direct": "true"}).status_code == 200
