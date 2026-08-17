import base64
import secrets

import pytest
from Crypto.PublicKey import ECC
from Crypto.Random import get_random_bytes
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.webpush import _raw_public_point
from app.webpush import vapid_public_key_b64


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


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _subscription_payload(endpoint: str = "https://push.example.net/sub/abc") -> dict:
    ua_key = ECC.generate(curve="P-256")
    return {
        "subscription": {
            "endpoint": endpoint,
            "keys": {
                "p256dh": _b64url(_raw_public_point(ua_key)),
                "auth": _b64url(get_random_bytes(16)),
            },
        }
    }


def test_push_subscription_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/push/subscription")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_push_subscription_requires_push_scope(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read write")
    response = client.get(
        "/api/v1/push/subscription",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
    assert response.headers["WWW-Authenticate"] == 'Bearer scope="push"'


@pytest.mark.asyncio
async def test_push_subscription_full_crud_round_trip(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read write follow push")
    headers = {"Authorization": f"Bearer {token}"}

    create_response = client.post(
        "/api/v1/push/subscription",
        json=_subscription_payload(),
        headers=headers,
    )
    assert create_response.status_code == 200
    created = create_response.json()
    assert created["endpoint"] == "https://push.example.net/sub/abc"
    assert created["server_key"] == vapid_public_key_b64()
    assert created["standard"] is True
    # Default alerts are true (deliberate divergence from upstream Mastodon,
    # which defaults them false and leaves a fresh subscription silently
    # inert until update_subscription is called).
    assert created["alerts"]["mention"] is True
    assert created["alerts"]["follow"] is True
    assert created["alerts"]["admin.sign_up"] is False
    assert created["alerts"]["admin.report"] is False

    get_response = client.get("/api/v1/push/subscription", headers=headers)
    assert get_response.status_code == 200
    assert get_response.json()["id"] == created["id"]

    put_response = client.put(
        "/api/v1/push/subscription",
        json={"data": {"alerts": {"mention": False}, "policy": "followed"}},
        headers=headers,
    )
    assert put_response.status_code == 200
    updated = put_response.json()
    assert updated["alerts"]["mention"] is False
    # Absent alert keys keep their existing value.
    assert updated["alerts"]["follow"] is True
    assert updated["policy"] == "followed"
    assert updated["endpoint"] == "https://push.example.net/sub/abc"

    delete_response = client.delete("/api/v1/push/subscription", headers=headers)
    assert delete_response.status_code == 200
    assert delete_response.json() == {}

    # Idempotent: deleting again is still a 200, not a 404.
    second_delete_response = client.delete("/api/v1/push/subscription", headers=headers)
    assert second_delete_response.status_code == 200

    missing_response = client.get("/api/v1/push/subscription", headers=headers)
    assert missing_response.status_code == 404


@pytest.mark.asyncio
async def test_push_subscription_post_twice_leaves_one_row(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "push")
    headers = {"Authorization": f"Bearer {token}"}

    client.post(
        "/api/v1/push/subscription",
        json=_subscription_payload("https://push.example.net/sub/first"),
        headers=headers,
    )
    second = client.post(
        "/api/v1/push/subscription",
        json=_subscription_payload("https://push.example.net/sub/second"),
        headers=headers,
    )
    assert second.status_code == 200
    assert second.json()["endpoint"] == "https://push.example.net/sub/second"

    count = await async_db_session.scalar(
        select(func.count(models.PushSubscription.id))
    )
    assert count == 1


@pytest.mark.asyncio
async def test_push_subscription_accepts_form_body(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "push")
    headers = {"Authorization": f"Bearer {token}"}
    ua_key = ECC.generate(curve="P-256")

    response = client.post(
        "/api/v1/push/subscription",
        data={
            "subscription[endpoint]": "https://push.example.net/sub/form",
            "subscription[keys][p256dh]": _b64url(_raw_public_point(ua_key)),
            "subscription[keys][auth]": _b64url(get_random_bytes(16)),
            "data[alerts][mention]": "true",
            "data[policy]": "all",
        },
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["endpoint"] == "https://push.example.net/sub/form"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["subscription"].update(endpoint="http://insecure.example"),
        lambda p: p["subscription"]["keys"].update(p256dh="not-base64!!"),
        lambda p: p["subscription"]["keys"].update(auth=_b64url(b"\x00" * 15)),
    ],
)
async def test_push_subscription_validation_rejects_bad_input(
    client: TestClient,
    async_db_session: AsyncSession,
    mutate,
) -> None:
    token = await _make_access_token(async_db_session, "push")
    payload = _subscription_payload()
    mutate(payload)

    response = client.post(
        "/api/v1/push/subscription",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_failed"


@pytest.mark.asyncio
async def test_push_subscription_rejects_invalid_policy(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "push")
    payload = _subscription_payload()
    payload["data"] = {"policy": "nonsense"}

    response = client.post(
        "/api/v1/push/subscription",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


def test_push_server_key_matches_across_advertisement_sites(
    client: TestClient,
) -> None:
    expected = vapid_public_key_b64()

    v1 = client.get("/api/v1/instance").json()
    assert v1["configuration"]["vapid"]["public_key"] == expected

    v2 = client.get("/api/v2/instance").json()
    assert v2["configuration"]["vapid"]["public_key"] == expected
