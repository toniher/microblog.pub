"""Grouped notifications (`GET /api/v2/notifications*`, Mastodon 4.3).

See `app/mastodon/notification_groups.py` for the grouping rules under test
here.
"""

import secrets
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

import activitypub.models
from activitypub import activitypub as ap
from activitypub import boxes
from activitypub.ap_object import ObjectType
from activitypub.tests import factories
from app import models
from tests.utils import setup_remote_actor


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


def _make_actor(respx_mock: respx.MockRouter, n: int) -> activitypub.models.Actor:
    ra = setup_remote_actor(respx_mock, base_url=f"https://actor{n}.example")
    return factories.ActorFactory.from_remote_actor(ra)


async def _make_post(db_session: AsyncSession, content: str = "hi"):
    _, outbox_object = await boxes.send_create(
        db_session,
        ObjectType.NOTE.value,
        content,
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    return outbox_object


def _like(actor_id: int | None, outbox_object_id: int | None) -> models.Notification:
    return models.Notification(
        notification_type=models.NotificationType.LIKE,
        actor_id=actor_id,
        outbox_object_id=outbox_object_id,
    )


def _follow(
    actor_id: int | None, created_at: datetime | None = None
) -> models.Notification:
    if created_at is not None:
        return models.Notification(
            notification_type=models.NotificationType.NEW_FOLLOWER,
            actor_id=actor_id,
            created_at=created_at,
        )
    return models.Notification(
        notification_type=models.NotificationType.NEW_FOLLOWER,
        actor_id=actor_id,
    )


@pytest.mark.asyncio
async def test_three_favourites_of_one_post_group_together(
    client: TestClient, async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    post = await _make_post(async_db_session)
    actors = [_make_actor(respx_mock, i) for i in range(3)]
    async_db_session.add_all([_like(a.id, post.id) for a in actors])
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "read:notifications")
    response = client.get(
        "/api/v2/notifications", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    data = response.json()

    assert len(data["notification_groups"]) == 1
    group = data["notification_groups"][0]
    assert group["type"] == "favourite"
    assert group["group_key"] == f"favourite-{post.id}"
    assert group["notifications_count"] == 3
    assert len(group["sample_account_ids"]) == 3
    assert len(set(group["sample_account_ids"])) == 3
    assert group["status_id"] is not None

    assert len(data["accounts"]) == 3
    assert len(data["statuses"]) == 1
    assert data["statuses"][0]["id"] == group["status_id"]


@pytest.mark.asyncio
async def test_favourites_of_two_posts_are_two_groups(
    client: TestClient, async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    post_a = await _make_post(async_db_session, "a")
    post_b = await _make_post(async_db_session, "b")
    actor = _make_actor(respx_mock, 0)
    async_db_session.add_all([_like(actor.id, post_a.id), _like(actor.id, post_b.id)])
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "read:notifications")
    response = client.get(
        "/api/v2/notifications", headers={"Authorization": f"Bearer {token}"}
    )
    data = response.json()
    keys = {g["group_key"] for g in data["notification_groups"]}
    assert keys == {f"favourite-{post_a.id}", f"favourite-{post_b.id}"}


@pytest.mark.asyncio
async def test_follows_group_by_utc_calendar_day(
    client: TestClient, async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    day = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    actors = [_make_actor(respx_mock, i) for i in range(3)]
    async_db_session.add_all(
        [
            _follow(actors[0].id, day),
            _follow(actors[1].id, day + timedelta(hours=2)),
            _follow(actors[2].id, day + timedelta(days=1)),
        ]
    )
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "read:notifications")
    response = client.get(
        "/api/v2/notifications", headers={"Authorization": f"Bearer {token}"}
    )
    data = response.json()
    groups_by_key = {g["group_key"]: g for g in data["notification_groups"]}
    assert groups_by_key[f"follow-{day:%Y%m%d}"]["notifications_count"] == 2
    assert (
        groups_by_key[f"follow-{(day + timedelta(days=1)):%Y%m%d}"][
            "notifications_count"
        ]
        == 1
    )


@pytest.mark.asyncio
async def test_ungroupable_type_keeps_the_ungrouped_shape(
    client: TestClient, async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    actor = _make_actor(respx_mock, 0)
    notif = models.Notification(
        notification_type=models.NotificationType.MENTION,
        actor_id=actor.id,
    )
    async_db_session.add(notif)
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "read:notifications")
    response = client.get(
        "/api/v2/notifications", headers={"Authorization": f"Bearer {token}"}
    )
    data = response.json()
    assert len(data["notification_groups"]) == 1
    group = data["notification_groups"][0]
    assert group["group_key"] == f"ungrouped-{notif.id}"
    assert group["notifications_count"] == 1


@pytest.mark.asyncio
async def test_grouped_types_param_narrows_which_types_group(
    client: TestClient, async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    post = await _make_post(async_db_session)
    day = datetime(2026, 1, 1, tzinfo=timezone.utc)
    actors = [_make_actor(respx_mock, i) for i in range(3)]
    like_notif = _like(actors[0].id, post.id)
    async_db_session.add_all(
        [like_notif, _follow(actors[1].id, day), _follow(actors[2].id, day)]
    )
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "read:notifications")
    response = client.get(
        "/api/v2/notifications?grouped_types[]=follow",
        headers={"Authorization": f"Bearer {token}"},
    )
    data = response.json()
    keys = {g["group_key"] for g in data["notification_groups"]}
    # The favourite is excluded from grouping by the narrowed set, so it
    # falls back to its own ungrouped-{id} key; the follows still group.
    assert f"ungrouped-{like_notif.id}" in keys
    assert f"follow-{day:%Y%m%d}" in keys
    assert not any(k.startswith("favourite-") for k in keys)


@pytest.mark.asyncio
async def test_pagination_link_header_walks_across_groups(
    client: TestClient, async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    post_a = await _make_post(async_db_session, "a")
    post_b = await _make_post(async_db_session, "b")
    actor = _make_actor(respx_mock, 0)
    like_a = _like(actor.id, post_a.id)
    like_b = _like(actor.id, post_b.id)
    async_db_session.add_all([like_a, like_b])
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "read:notifications")
    headers = {"Authorization": f"Bearer {token}"}
    response = client.get("/api/v2/notifications?limit=1", headers=headers)
    assert response.status_code == 200
    assert "Link" in response.headers
    data = response.json()
    assert len(data["notification_groups"]) == 1
    first_group = data["notification_groups"][0]
    assert first_group["group_key"] == f"favourite-{post_b.id}"

    next_response = client.get(
        f"/api/v2/notifications?limit=1&max_id={first_group['page_min_id']}",
        headers=headers,
    )
    assert next_response.status_code == 200
    next_data = next_response.json()
    assert len(next_data["notification_groups"]) == 1
    assert next_data["notification_groups"][0]["group_key"] == f"favourite-{post_a.id}"


@pytest.mark.asyncio
async def test_notifications_count_is_true_total_even_when_page_cuts_the_group(
    client: TestClient, async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    post = await _make_post(async_db_session)
    actors = [_make_actor(respx_mock, i) for i in range(5)]
    async_db_session.add_all([_like(a.id, post.id) for a in actors])
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "read:notifications")
    # limit=1 -> the assembly window is min(1*4, 200) = 4 rows, strictly less
    # than the 5 real members of this single favourite group.
    response = client.get(
        "/api/v2/notifications?limit=1", headers={"Authorization": f"Bearer {token}"}
    )
    data = response.json()
    assert len(data["notification_groups"]) == 1
    assert data["notification_groups"][0]["notifications_count"] == 5


@pytest.mark.asyncio
async def test_muted_actor_favourite_is_absent_from_grouped_screen(
    client: TestClient,
    async_db_session: AsyncSession,
    db: Session,
    respx_mock: respx.MockRouter,
) -> None:
    post = await _make_post(async_db_session)
    actor = _make_actor(respx_mock, 0)
    # Mutate/commit via the matching sync session, not async_db_session --
    # see tests/test_streaming_pump.py's test_muted_actor_notification_produces_no_event.
    actor.is_muted = True
    actor.are_notifications_muted = True
    db.commit()
    async_db_session.add(_like(actor.id, post.id))
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "read:notifications")
    response = client.get(
        "/api/v2/notifications", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.json()["notification_groups"] == []


@pytest.mark.asyncio
async def test_show_group_round_trips_a_key_and_404s_unknown(
    client: TestClient, async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    post = await _make_post(async_db_session)
    actors = [_make_actor(respx_mock, i) for i in range(2)]
    async_db_session.add_all([_like(a.id, post.id) for a in actors])
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "read:notifications")
    headers = {"Authorization": f"Bearer {token}"}
    list_response = client.get("/api/v2/notifications", headers=headers)
    group_key = list_response.json()["notification_groups"][0]["group_key"]

    show_response = client.get(f"/api/v2/notifications/{group_key}", headers=headers)
    assert show_response.status_code == 200
    show_data = show_response.json()
    assert len(show_data["notification_groups"]) == 1
    assert show_data["notification_groups"][0]["group_key"] == group_key
    assert "page_min_id" not in show_data["notification_groups"][0]
    assert "page_max_id" not in show_data["notification_groups"][0]
    assert "latest_page_notification_at" not in show_data["notification_groups"][0]

    missing_response = client.get(
        "/api/v2/notifications/favourite-999999999", headers=headers
    )
    assert missing_response.status_code == 404


@pytest.mark.asyncio
async def test_dismiss_group_deletes_exactly_that_groups_rows(
    client: TestClient, async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    post_a = await _make_post(async_db_session, "a")
    post_b = await _make_post(async_db_session, "b")
    actor = _make_actor(respx_mock, 0)
    like_a = _like(actor.id, post_a.id)
    like_b = _like(actor.id, post_b.id)
    async_db_session.add_all([like_a, like_b])
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "read write")
    headers = {"Authorization": f"Bearer {token}"}

    dismiss_response = client.post(
        f"/api/v2/notifications/favourite-{post_a.id}/dismiss", headers=headers
    )
    assert dismiss_response.status_code == 200
    assert dismiss_response.json() == {}

    remaining_ids = (
        (await async_db_session.execute(select(models.Notification.id))).scalars().all()
    )
    assert remaining_ids == [like_b.id]


@pytest.mark.asyncio
async def test_group_accounts_and_unread_count(
    client: TestClient, async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    post = await _make_post(async_db_session)
    actors = [_make_actor(respx_mock, i) for i in range(3)]
    async_db_session.add_all([_like(a.id, post.id) for a in actors])
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "read:notifications")
    headers = {"Authorization": f"Bearer {token}"}

    unread_before = client.get("/api/v2/notifications/unread_count", headers=headers)
    assert unread_before.status_code == 200
    # Three favourites of one post is one *group* -- not three.
    assert unread_before.json()["count"] == 1

    accounts_response = client.get(
        f"/api/v2/notifications/favourite-{post.id}/accounts", headers=headers
    )
    assert accounts_response.status_code == 200
    accounts_data = accounts_response.json()
    assert len(accounts_data) == 3
    assert {a["id"] for a in accounts_data} == {str(a.id) for a in actors}

    # Listing marks the underlying notifications read, same as v1.
    list_response = client.get("/api/v2/notifications", headers=headers)
    assert list_response.status_code == 200
    unread_after = client.get("/api/v2/notifications/unread_count", headers=headers)
    assert unread_after.json()["count"] == 0


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/v2/notifications"),
        ("get", "/api/v2/notifications/unread_count"),
        ("get", "/api/v2/notifications/favourite-1"),
        ("get", "/api/v2/notifications/favourite-1/accounts"),
        ("post", "/api/v2/notifications/favourite-1/dismiss"),
    ],
)
def test_v2_notifications_require_auth(
    client: TestClient, method: str, path: str
) -> None:
    response = getattr(client, method)(path)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_dismiss_requires_write_scope(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read:notifications")
    response = client.post(
        "/api/v2/notifications/favourite-1/dismiss",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
