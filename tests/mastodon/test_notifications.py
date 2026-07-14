import re
import secrets
from datetime import datetime
from datetime import timezone

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from activitypub import activitypub as ap
from activitypub import boxes
from activitypub.ap_object import ObjectType
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


def test_notifications_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/notifications")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_notifications_list_maps_types_and_filters_unmapped(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    _, outbox_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Like me",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    follow_notif = models.Notification(
        notification_type=models.NotificationType.NEW_FOLLOWER,
        actor_id=follower.actor.id,
    )
    like_notif = models.Notification(
        notification_type=models.NotificationType.LIKE,
        actor_id=follower.actor.id,
        outbox_object_id=outbox_object.id,
    )
    # Has no Mastodon equivalent — must never be surfaced.
    undo_like_notif = models.Notification(
        notification_type=models.NotificationType.UNDO_LIKE,
        actor_id=follower.actor.id,
    )
    async_db_session.add_all([follow_notif, like_notif, undo_like_notif])
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "read:notifications")
    response = client.get(
        "/api/v1/notifications", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    data = response.json()
    types_by_id = {n["id"]: n["type"] for n in data}
    assert types_by_id == {
        str(follow_notif.id): "follow",
        str(like_notif.id): "favourite",
    }
    like_entity = next(n for n in data if n["id"] == str(like_notif.id))
    assert like_entity["status"]["id"] == ids.encode_outbox_id(outbox_object)


@pytest.mark.asyncio
async def test_notifications_list_marks_as_read(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    notif = models.Notification(
        notification_type=models.NotificationType.NEW_FOLLOWER,
        actor_id=follower.actor.id,
        is_new=True,
    )
    async_db_session.add(notif)
    await async_db_session.commit()
    notif_id = notif.id

    token = await _make_access_token(async_db_session, "read:notifications")
    client.get("/api/v1/notifications", headers={"Authorization": f"Bearer {token}"})

    # The request handler updates the row via a different AsyncSession;
    # this session's identity map still holds the pre-update `notif`
    # instance (expire_on_commit=False), so force a fresh read.
    async_db_session.expire_all()
    refreshed = (
        await async_db_session.scalars(
            select(models.Notification).where(models.Notification.id == notif_id)
        )
    ).one()
    assert refreshed.is_new is False


@pytest.mark.asyncio
async def test_notifications_list_formats_created_at_with_millisecond_precision(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    # Plain `datetime.isoformat()` emits 6-digit microseconds whenever they're
    # non-zero (the common case for real timestamps), which strict RFC3339
    # clients (e.g. Ice Cubes) fail to decode — silently dropping every
    # notification in the response. Pin to exactly 3 fractional digits.
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    notif = models.Notification(
        notification_type=models.NotificationType.NEW_FOLLOWER,
        actor_id=follower.actor.id,
        created_at=datetime(2024, 1, 1, 12, 0, 0, 123456, tzinfo=timezone.utc),
    )
    async_db_session.add(notif)
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "read:notifications")
    response = client.get(
        "/api/v1/notifications", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    created_at = response.json()[0]["created_at"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", created_at)


@pytest.mark.asyncio
async def test_notifications_show_and_not_found(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    notif = models.Notification(
        notification_type=models.NotificationType.NEW_FOLLOWER,
        actor_id=follower.actor.id,
    )
    async_db_session.add(notif)
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "read:notifications")
    headers = {"Authorization": f"Bearer {token}"}

    response = client.get(f"/api/v1/notifications/{notif.id}", headers=headers)
    assert response.status_code == 200
    assert response.json()["type"] == "follow"

    assert (
        client.get("/api/v1/notifications/999999", headers=headers).status_code == 404
    )


@pytest.mark.asyncio
async def test_notifications_clear_deletes_all(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    async_db_session.add(
        models.Notification(
            notification_type=models.NotificationType.NEW_FOLLOWER,
            actor_id=follower.actor.id,
        )
    )
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "write:notifications")
    response = client.post(
        "/api/v1/notifications/clear", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200

    remaining = (await async_db_session.scalars(select(models.Notification))).all()
    assert remaining == []


@pytest.mark.asyncio
async def test_notifications_dismiss_deletes_one(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    kept = models.Notification(
        notification_type=models.NotificationType.NEW_FOLLOWER,
        actor_id=follower.actor.id,
    )
    dismissed = models.Notification(
        notification_type=models.NotificationType.MENTION,
        actor_id=follower.actor.id,
    )
    async_db_session.add_all([kept, dismissed])
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "write:notifications")
    response = client.post(
        f"/api/v1/notifications/{dismissed.id}/dismiss",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200

    remaining_ids = {
        n.id
        for n in (await async_db_session.scalars(select(models.Notification))).all()
    }
    assert remaining_ids == {kept.id}


@pytest.mark.asyncio
async def test_notifications_type_filters(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    follow_notif = models.Notification(
        notification_type=models.NotificationType.NEW_FOLLOWER,
        actor_id=follower.actor.id,
    )
    mention_notif = models.Notification(
        notification_type=models.NotificationType.MENTION,
        actor_id=follower.actor.id,
    )
    async_db_session.add_all([follow_notif, mention_notif])
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "read:notifications")
    headers = {"Authorization": f"Bearer {token}"}

    only_follow = client.get(
        "/api/v1/notifications?types[]=follow", headers=headers
    ).json()
    assert {n["id"] for n in only_follow} == {str(follow_notif.id)}

    excluding_follow = client.get(
        "/api/v1/notifications?exclude_types[]=follow", headers=headers
    ).json()
    assert {n["id"] for n in excluding_follow} == {str(mention_notif.id)}


@pytest.mark.asyncio
async def test_notifications_policy_get_and_put_accept_everything(
    client: TestClient,
    async_db_session: AsyncSession,
) -> None:
    token = await _make_access_token(async_db_session, "read write")
    headers = {"Authorization": f"Bearer {token}"}

    get_response = client.get("/api/v2/notifications/policy", headers=headers)
    assert get_response.status_code == 200
    policy = get_response.json()
    assert policy["for_not_following"] == "accept"
    assert policy["summary"] == {
        "pending_requests_count": 0,
        "pending_notifications_count": 0,
    }

    put_response = client.put(
        "/api/v2/notifications/policy",
        headers=headers,
        json={"for_not_following": "drop"},
    )
    assert put_response.status_code == 200
    assert put_response.json()["for_not_following"] == "accept"


@pytest.mark.asyncio
async def test_notification_requests_are_always_empty(
    client: TestClient,
    async_db_session: AsyncSession,
) -> None:
    # Also guards route-registration order: `requests`/`requests/merged` must
    # not be swallowed by the `/api/v1/notifications/{notification_id}` route.
    token = await _make_access_token(async_db_session, "read:notifications")
    headers = {"Authorization": f"Bearer {token}"}

    requests_response = client.get("/api/v1/notifications/requests", headers=headers)
    assert requests_response.status_code == 200
    assert requests_response.json() == []

    merged_response = client.get(
        "/api/v1/notifications/requests/merged", headers=headers
    )
    assert merged_response.status_code == 200
    assert merged_response.json() == {"merged": True}


@pytest.mark.asyncio
async def test_notifications_list_serializes_actor_string_media_fields(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    ra.ap_actor["icon"] = "https://example.com/media/avatar.jpg"
    ra.ap_actor["image"] = "https://example.com/media/header.jpg"
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    notif = models.Notification(
        notification_type=models.NotificationType.NEW_FOLLOWER,
        actor_id=follower.actor.id,
    )
    async_db_session.add(notif)
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "read:notifications")
    response = client.get(
        "/api/v1/notifications",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    account = response.json()[0]["account"]
    assert account["avatar_static"] == "https://example.com/media/avatar.jpg"
    assert account["header"] == "https://example.com/media/header.jpg"
