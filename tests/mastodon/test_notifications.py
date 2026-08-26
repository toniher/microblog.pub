import base64
import re
import secrets
from datetime import datetime
from datetime import timezone

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

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

    # `group_key` is non-optional from Mastodon 4.3, which is the level
    # `_MASTODON_COMPAT_VERSION` advertises — a strict client decoder fails
    # the whole screen on a missing key, not just the one row. `follow` and
    # `favourite` are both real grouping types (app.mastodon.notification_groups);
    # each notification here is the only one of its kind, so both still come
    # back as singleton groups, just keyed by type/target rather than by id.
    follow_entity = next(n for n in data if n["id"] == str(follow_notif.id))
    assert follow_entity["group_key"] == f"follow-{follow_notif.created_at:%Y%m%d}"
    assert like_entity["group_key"] == f"favourite-{outbox_object.id}"
    assert len({n["group_key"] for n in data}) == len(data)


@pytest.mark.asyncio
async def test_notifications_list_maps_status_update_and_poll_types(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    note = RemoteObject(
        factories.build_note_object(from_remote_actor=ra, content="Hello"),
        ra,
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(note, follower.actor)

    _, outbox_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "My own poll target",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    status_notif = models.Notification(
        notification_type=models.NotificationType.STATUS,
        actor_id=follower.actor.id,
        inbox_object_id=inbox_object.id,
    )
    update_notif = models.Notification(
        notification_type=models.NotificationType.UPDATE,
        actor_id=follower.actor.id,
        inbox_object_id=inbox_object.id,
    )
    # An actor-less POLL row -- the owner's own poll ending.
    poll_notif = models.Notification(
        notification_type=models.NotificationType.POLL,
        actor_id=None,
        outbox_object_id=outbox_object.id,
    )
    async_db_session.add_all([status_notif, update_notif, poll_notif])
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "read:notifications")
    response = client.get(
        "/api/v1/notifications", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    data = response.json()
    types_by_id = {n["id"]: n["type"] for n in data}
    assert types_by_id[str(status_notif.id)] == "status"
    assert types_by_id[str(update_notif.id)] == "update"
    assert types_by_id[str(poll_notif.id)] == "poll"

    poll_entity = next(n for n in data if n["id"] == str(poll_notif.id))
    assert poll_entity["account"]["id"] == ids.LOCAL_ACTOR_ID


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
async def test_notifications_unread_count(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    unread = models.Notification(
        notification_type=models.NotificationType.NEW_FOLLOWER,
        actor_id=follower.actor.id,
        is_new=True,
    )
    already_read = models.Notification(
        notification_type=models.NotificationType.LIKE,
        actor_id=follower.actor.id,
        is_new=False,
    )
    # Has no Mastodon equivalent — must never be counted.
    unmapped = models.Notification(
        notification_type=models.NotificationType.UNDO_LIKE,
        actor_id=follower.actor.id,
        is_new=True,
    )
    async_db_session.add_all([unread, already_read, unmapped])
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "read:notifications")
    response = client.get(
        "/api/v1/notifications/unread_count",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == {"count": 1}


@pytest.mark.asyncio
async def test_notifications_unread_count_drops_to_zero_after_list_marks_read(
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

    token = await _make_access_token(async_db_session, "read:notifications")
    headers = {"Authorization": f"Bearer {token}"}

    before = client.get("/api/v1/notifications/unread_count", headers=headers)
    assert before.json() == {"count": 1}

    client.get("/api/v1/notifications", headers=headers)

    after = client.get("/api/v1/notifications/unread_count", headers=headers)
    assert after.json() == {"count": 0}


def test_notifications_unread_count_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/notifications/unread_count")
    assert response.status_code == 401


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


def _decode_proxied_media_url(proxied_url: str) -> str:
    # BASE_URL + "/proxy/media/{expires}/{sig}/" + base64(original_url)
    encoded = proxied_url.rstrip("/").rsplit("/", 1)[-1]
    padded = encoded + "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(padded).decode()


@pytest.mark.asyncio
async def test_notifications_list_serializes_actor_string_media_fields(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    # icon/image given as a bare string (not the usual {"type": "Image",
    # "url": ...} object) must still resolve correctly — and, like every
    # other remote media URL, get proxied rather than leaking the raw
    # remote URL to the client.
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
    assert account["avatar_static"].startswith(f"{config.BASE_URL}/proxy/media/")
    assert account["header"].startswith(f"{config.BASE_URL}/proxy/media/")
    assert (
        _decode_proxied_media_url(account["avatar_static"])
        == "https://example.com/media/avatar.jpg"
    )
    assert (
        _decode_proxied_media_url(account["header"])
        == "https://example.com/media/header.jpg"
    )


@pytest.mark.asyncio
async def test_notifications_hides_muted_actor(
    client: TestClient,
    db: Session,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    muted_ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    muted_follower = setup_remote_actor_as_follower(muted_ra)
    assert muted_follower.actor is not None

    other_ra = setup_remote_actor(respx_mock, base_url="https://example.org")
    other_follower = setup_remote_actor_as_follower(other_ra)
    assert other_follower.actor is not None

    async_db_session.add_all(
        [
            models.Notification(
                notification_type=models.NotificationType.NEW_FOLLOWER,
                actor_id=muted_follower.actor.id,
            ),
            models.Notification(
                notification_type=models.NotificationType.NEW_FOLLOWER,
                actor_id=other_follower.actor.id,
            ),
        ]
    )
    await async_db_session.commit()

    muted_follower.actor.is_muted = True
    muted_follower.actor.are_notifications_muted = True
    db.commit()

    token = await _make_access_token(async_db_session, "read:notifications")
    response = client.get(
        "/api/v1/notifications", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    account_ids = {notif["account"]["id"] for notif in response.json()}
    assert str(other_follower.actor.id) in account_ids
    assert str(muted_follower.actor.id) not in account_ids


@pytest.mark.asyncio
async def test_notifications_kept_when_mute_spares_notifications(
    client: TestClient,
    db: Session,
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

    # Muted, but the mute was set with notifications=false.
    follower.actor.is_muted = True
    follower.actor.are_notifications_muted = False
    db.commit()

    token = await _make_access_token(async_db_session, "read:notifications")
    response = client.get(
        "/api/v1/notifications", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert {notif["account"]["id"] for notif in response.json()} == {
        str(follower.actor.id)
    }


@pytest.mark.asyncio
async def test_notifications_hides_muted_conversation(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    muted_note = RemoteObject(
        factories.build_note_object(from_remote_actor=ra, content="@me noisy reply"),
        ra,
    )
    muted_inbox_object = factories.InboxObjectFactory.from_remote_object(
        muted_note, follower.actor
    )
    muted_inbox_object.conversation = "https://example.com/thread-1"

    kept_note = RemoteObject(
        factories.build_note_object(from_remote_actor=ra, content="@me hi"), ra
    )
    kept_inbox_object = factories.InboxObjectFactory.from_remote_object(
        kept_note, follower.actor
    )
    kept_inbox_object.conversation = "https://example.com/thread-2"

    async_db_session.add_all(
        [
            models.Notification(
                notification_type=models.NotificationType.MENTION,
                actor_id=follower.actor.id,
                inbox_object_id=muted_inbox_object.id,
            ),
            models.Notification(
                notification_type=models.NotificationType.MENTION,
                actor_id=follower.actor.id,
                inbox_object_id=kept_inbox_object.id,
            ),
            models.MutedConversation(conversation="https://example.com/thread-1"),
        ]
    )
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "read:notifications")
    response = client.get(
        "/api/v1/notifications", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    status_ids = {notif["status"]["id"] for notif in response.json()}
    assert ids.encode_inbox_id(kept_inbox_object) in status_ids
    assert ids.encode_inbox_id(muted_inbox_object) not in status_ids
