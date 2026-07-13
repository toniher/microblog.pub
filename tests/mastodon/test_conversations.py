import secrets

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from activitypub import activitypub as ap
from activitypub.actor import LOCAL_ACTOR
from activitypub.tests import factories
from app import models
from app.mastodon import ids
from tests.utils import setup_inbox_note
from tests.utils import setup_outbox_note
from tests.utils import setup_remote_actor

# setup_inbox_note/setup_outbox_note persist (and commit) through the sync
# `db` session that `client` and the `factories` module share. Passing the
# rows they return to `async_db_session.add()` would attach the same object
# to two sessions at once and raise — so below, only newly constructed rows
# (Notification, IndieAuthAccessToken) go through `async_db_session`; any
# further mutation of a factory-built row is committed via `db` instead.


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


def test_conversations_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/conversations")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_conversations_list_groups_thread_and_reports_unread(
    client: TestClient,
    db,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)

    dm_reply = setup_inbox_note(actor, content="Hi there", to=[LOCAL_ACTOR.ap_id])
    dm_start = setup_outbox_note(content="Hello", to=[ra.ap_id])
    dm_reply.ap_context = dm_start.ap_context = "https://example.com/ctx/shared"
    db.commit()

    # A public reply in an unrelated thread must not leak into the DM list.
    setup_inbox_note(actor, content="Not a DM", to=[ap.AS_PUBLIC])

    async_db_session.add(
        models.Notification(
            notification_type=models.NotificationType.MENTION,
            actor_id=actor.id,
            inbox_object_id=dm_reply.id,
            is_new=True,
        )
    )
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "read:statuses")
    response = client.get(
        "/api/v1/conversations", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    convo = data[0]
    assert convo["id"] == ids.encode_inbox_id(dm_reply)
    assert convo["unread"] is True
    assert convo["last_status"]["id"] == ids.encode_inbox_id(dm_reply)
    assert [a["id"] for a in convo["accounts"]] == [str(actor.id)]


@pytest.mark.asyncio
async def test_conversations_outbox_only_thread_resolves_accounts_from_mentions(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)

    setup_outbox_note(
        content="Hello",
        to=[ra.ap_id],
        tags=[{"type": "Mention", "href": ra.ap_id, "name": "@toto"}],
    )

    token = await _make_access_token(async_db_session, "read:statuses")
    response = client.get(
        "/api/v1/conversations", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["unread"] is False
    assert [a["id"] for a in data[0]["accounts"]] == [str(actor.id)]


@pytest.mark.asyncio
async def test_conversations_read_marks_notification_and_returns_unread_false(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)

    dm_reply = setup_inbox_note(actor, content="Hi", to=[LOCAL_ACTOR.ap_id])

    notif = models.Notification(
        notification_type=models.NotificationType.MENTION,
        actor_id=actor.id,
        inbox_object_id=dm_reply.id,
        is_new=True,
    )
    async_db_session.add(notif)
    await async_db_session.commit()
    notif_id = notif.id

    token = await _make_access_token(async_db_session, "write:conversations")
    conversation_id = ids.encode_inbox_id(dm_reply)
    response = client.post(
        f"/api/v1/conversations/{conversation_id}/read",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["unread"] is False

    async_db_session.expire_all()
    refreshed = (
        await async_db_session.scalars(
            select(models.Notification).where(models.Notification.id == notif_id)
        )
    ).one()
    assert refreshed.is_new is False


@pytest.mark.asyncio
async def test_conversations_read_not_found(
    client: TestClient,
    async_db_session: AsyncSession,
) -> None:
    token = await _make_access_token(async_db_session, "write:conversations")
    response = client.post(
        "/api/v1/conversations/999999999/read",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404
