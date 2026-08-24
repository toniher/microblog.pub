import secrets
from datetime import timedelta

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

import activitypub.models
from activitypub import activitypub as ap
from activitypub import boxes
from activitypub.actor import LOCAL_ACTOR
from activitypub.ap_object import ObjectType
from activitypub.ap_object import RemoteObject
from activitypub.tests import factories
from app import config
from app import models
from app.mastodon import ids
from app.utils.datetime import now
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
async def test_follow_with_reblogs_and_notify_params(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)
    account_id = ids.encode_account_id(actor)

    token = await _make_access_token(async_db_session, "write:follows")
    followed = client.post(
        f"/api/v1/accounts/{account_id}/follow",
        headers={"Authorization": f"Bearer {token}"},
        json={"reblogs": False, "notify": True},
    ).json()

    assert followed["showing_reblogs"] is False
    assert followed["notifying"] is True


@pytest.mark.asyncio
async def test_follow_absent_params_take_mastodon_defaults(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)
    account_id = ids.encode_account_id(actor)

    token = await _make_access_token(async_db_session, "write:follows")
    followed = client.post(
        f"/api/v1/accounts/{account_id}/follow",
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    assert followed["showing_reblogs"] is True
    assert followed["notifying"] is False


@pytest.mark.asyncio
async def test_reposting_follow_toggles_flags_without_a_second_follow_activity(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)
    account_id = ids.encode_account_id(actor)

    token = await _make_access_token(async_db_session, "write:follows")
    headers = {"Authorization": f"Bearer {token}"}

    client.post(f"/api/v1/accounts/{account_id}/follow", headers=headers)
    toggled = client.post(
        f"/api/v1/accounts/{account_id}/follow",
        headers=headers,
        json={"reblogs": False, "notify": True},
    ).json()

    assert toggled["showing_reblogs"] is False
    assert toggled["notifying"] is True

    follow_activities = (
        await async_db_session.scalars(
            select(activitypub.models.OutboxObject).where(
                activitypub.models.OutboxObject.ap_type == "Follow",
                activitypub.models.OutboxObject.activity_object_ap_id == actor.ap_id,
            )
        )
    ).all()
    assert len(follow_activities) == 1


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
async def test_blocks_list(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)
    account_id = ids.encode_account_id(actor)

    token = await _make_access_token(async_db_session, "read:blocks write:blocks")
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/v1/blocks", headers=headers).json() == []

    client.post(f"/api/v1/accounts/{account_id}/block", headers=headers)

    listed = client.get("/api/v1/blocks", headers=headers)
    assert listed.status_code == 200
    assert [account["id"] for account in listed.json()] == [account_id]

    client.post(f"/api/v1/accounts/{account_id}/unblock", headers=headers)

    assert client.get("/api/v1/blocks", headers=headers).json() == []


def test_blocks_requires_auth(client: TestClient) -> None:
    assert client.get("/api/v1/blocks").status_code == 401


@pytest.mark.asyncio
async def test_blocks_requires_read_blocks_scope(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:blocks")
    response = client.get(
        "/api/v1/blocks", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_domain_blocks_list(
    client: TestClient,
    async_db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config.CONFIG,
        "blocked_servers",
        [
            config._BlockedServer(hostname="spam.example"),
            config._BlockedServer(hostname="abuse.example"),
        ],
    )

    token = await _make_access_token(async_db_session, "read:blocks")
    listed = client.get(
        "/api/v1/domain_blocks", headers={"Authorization": f"Bearer {token}"}
    )
    assert listed.status_code == 200
    assert listed.json() == ["abuse.example", "spam.example"]


def test_domain_blocks_requires_auth(client: TestClient) -> None:
    assert client.get("/api/v1/domain_blocks").status_code == 401


@pytest.mark.asyncio
async def test_domain_blocks_requires_read_blocks_scope(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:blocks")
    response = client.get(
        "/api/v1/domain_blocks", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_mute_and_unmute(
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
    assert muted["muting"] is True
    # Mastodon's default: a mute hides notifications too.
    assert muted["muting_notifications"] is True

    unmuted = client.post(
        f"/api/v1/accounts/{account_id}/unmute", headers=headers
    ).json()
    assert unmuted["muting"] is False
    assert unmuted["muting_notifications"] is False


@pytest.mark.asyncio
async def test_mute_without_notifications(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)
    account_id = ids.encode_account_id(actor)

    token = await _make_access_token(async_db_session, "write:mutes")
    muted = client.post(
        f"/api/v1/accounts/{account_id}/mute",
        headers={"Authorization": f"Bearer {token}"},
        json={"notifications": False},
    ).json()

    assert muted["muting"] is True
    assert muted["muting_notifications"] is False


@pytest.mark.asyncio
async def test_mute_with_duration_expires(
    client: TestClient,
    db: Session,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)
    account_id = ids.encode_account_id(actor)

    token = await _make_access_token(
        async_db_session, "read:accounts read:mutes write:mutes"
    )
    headers = {"Authorization": f"Bearer {token}"}

    muted = client.post(
        f"/api/v1/accounts/{account_id}/mute",
        headers=headers,
        data={"duration": "3600"},
    ).json()
    assert muted["muting"] is True
    listed = client.get("/api/v1/mutes", headers=headers).json()
    assert [account["id"] for account in listed] == [account_id]

    # Backdate the expiry rather than waiting an hour: an elapsed mute is
    # applied at read time, no sweep involved.
    actor.muted_until = now() - timedelta(seconds=1)
    db.commit()

    assert client.get("/api/v1/mutes", headers=headers).json() == []
    relationships = client.get(
        f"/api/v1/accounts/relationships?id[]={account_id}", headers=headers
    ).json()
    assert relationships[0]["muting"] is False


@pytest.mark.asyncio
async def test_mute_with_zero_duration_never_expires(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)
    account_id = ids.encode_account_id(actor)

    token = await _make_access_token(async_db_session, "write:mutes")
    muted = client.post(
        f"/api/v1/accounts/{account_id}/mute",
        headers={"Authorization": f"Bearer {token}"},
        # What Mastodon clients send for "mute indefinitely".
        data={"duration": "0"},
    ).json()

    assert muted["muting"] is True
    assert actor.muted_until is None


@pytest.mark.asyncio
async def test_mutes_list(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)
    account_id = ids.encode_account_id(actor)

    token = await _make_access_token(async_db_session, "read:mutes write:mutes")
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/v1/mutes", headers=headers).json() == []

    client.post(f"/api/v1/accounts/{account_id}/mute", headers=headers)

    listed = client.get("/api/v1/mutes", headers=headers)
    assert listed.status_code == 200
    assert [account["id"] for account in listed.json()] == [account_id]

    client.post(f"/api/v1/accounts/{account_id}/unmute", headers=headers)

    assert client.get("/api/v1/mutes", headers=headers).json() == []


def test_mutes_requires_auth(client: TestClient) -> None:
    assert client.get("/api/v1/mutes").status_code == 401


@pytest.mark.asyncio
async def test_mutes_requires_read_mutes_scope(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:mutes")
    response = client.get("/api/v1/mutes", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_account_note_is_persisted(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)
    account_id = ids.encode_account_id(actor)

    token = await _make_access_token(async_db_session, "read:accounts write:accounts")
    headers = {"Authorization": f"Bearer {token}"}
    response = client.post(
        f"/api/v1/accounts/{account_id}/note",
        headers=headers,
        data={"comment": "met them at a conference"},
    )

    assert response.status_code == 200
    assert response.json()["note"] == "met them at a conference"

    # Persisted, not just echoed for this response — a fresh relationship
    # lookup still reports it.
    relationships = client.get(
        "/api/v1/accounts/relationships",
        headers=headers,
        params={"id[]": [account_id]},
    ).json()
    assert relationships[0]["note"] == "met them at a conference"


@pytest.mark.asyncio
async def test_account_note_empty_comment_clears_it(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)
    account_id = ids.encode_account_id(actor)

    token = await _make_access_token(async_db_session, "write:accounts")
    headers = {"Authorization": f"Bearer {token}"}
    client.post(
        f"/api/v1/accounts/{account_id}/note",
        headers=headers,
        data={"comment": "met them at a conference"},
    )

    response = client.post(
        f"/api/v1/accounts/{account_id}/note",
        headers=headers,
        data={"comment": ""},
    )

    assert response.status_code == 200
    assert response.json()["note"] == ""


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
async def test_follow_requests_count_in_verify_credentials(
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
    token = await _make_access_token(
        async_db_session, "read:accounts read:follows write:follows"
    )
    headers = {"Authorization": f"Bearer {token}"}

    response = client.get("/api/v1/accounts/verify_credentials", headers=headers)
    assert response.json()["source"]["follow_requests_count"] == 1

    authorized = client.post(
        f"/api/v1/follow_requests/{account_id}/authorize", headers=headers
    )
    assert authorized.status_code == 200

    response_after = client.get("/api/v1/accounts/verify_credentials", headers=headers)
    assert response_after.json()["source"]["follow_requests_count"] == 0


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
async def test_search_statuses_reaches_past_the_first_page(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    """Matching is done by the query, not over a fixed window of recent rows:
    the old implementation scanned the newest 100 statuses per box and filtered
    in Python, so anything older was unfindable."""
    _, buried = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "A very unique searchable phrase",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    for i in range(120):
        await boxes.send_create(
            async_db_session,
            ObjectType.NOTE.value,
            f"filler {i}",
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
    assert ids.encode_outbox_id(buried) in {
        s["id"] for s in response.json()["statuses"]
    }


@pytest.mark.asyncio
async def test_search_escapes_sql_wildcards(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    """`%` and `_` are literal characters in a search box, not LIKE wildcards."""
    _, outbox_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "battery at 100% today",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    token = await _make_access_token(async_db_session, "read:search")
    headers = {"Authorization": f"Bearer {token}"}

    hit = client.get("/api/v2/search?q=100%25+today&type=statuses", headers=headers)
    assert hit.status_code == 200
    assert ids.encode_outbox_id(outbox_object) in {
        s["id"] for s in hit.json()["statuses"]
    }

    # A bare wildcard must not match everything.
    miss = client.get("/api/v2/search?q=%25zzz%25&type=statuses", headers=headers)
    assert miss.status_code == 200
    assert miss.json()["statuses"] == []


@pytest.mark.asyncio
async def test_search_local_accounts_unicode_case_folding(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    """SQLite's `LIKE`/`lower()` fold ASCII case only; `search_text` is
    normalized with NFC + `casefold()` at write time instead (see
    `app/utils/search_text.py`), so an actor named `José` is found searching
    either case of the accented letter -- the regression this whole plan
    exists to fix."""
    ra = factories.RemoteActorFactory(
        base_url="https://example.com/users/jose",
        username="José",
        public_key="pk",
    )
    actor = factories.ActorFactory.from_remote_actor(ra)

    token = await _make_access_token(async_db_session, "read:search")
    headers = {"Authorization": f"Bearer {token}"}

    for query in ("JOSÉ", "josé"):
        response = client.get(
            "/api/v2/search",
            params={"q": query, "type": "accounts"},
            headers=headers,
        )
        assert response.status_code == 200
        assert ids.encode_account_id(actor) in {
            a["id"] for a in response.json()["accounts"]
        }, query


@pytest.mark.asyncio
async def test_search_escapes_glob_metacharacters(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    """`*`, `?` and `[` are literal characters in a search box, not `GLOB`
    metacharacters -- mirrors `test_search_escapes_sql_wildcards` for the old
    `LIKE` form."""
    _, outbox_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "5*3=15? see [final] score",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    _, filler = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "just a filler status",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    token = await _make_access_token(async_db_session, "read:search")
    headers = {"Authorization": f"Bearer {token}"}

    for query in ("5*3=15", "15?", "[final]"):
        hit = client.get(
            "/api/v2/search",
            params={"q": query, "type": "statuses"},
            headers=headers,
        )
        assert hit.status_code == 200
        assert ids.encode_outbox_id(outbox_object) in {
            s["id"] for s in hit.json()["statuses"]
        }, query

    # A bare `*` must not match every status just because none contain one.
    miss = client.get(
        "/api/v2/search", params={"q": "*", "type": "statuses"}, headers=headers
    )
    assert miss.status_code == 200
    statuses = {s["id"] for s in miss.json()["statuses"]}
    assert ids.encode_outbox_id(outbox_object) in statuses
    assert ids.encode_outbox_id(filler) not in statuses


@pytest.mark.asyncio
async def test_search_index_stays_in_sync_with_edits_and_deletes(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    """The FTS5 shadow index is kept in sync by the SQL triggers in
    `activitypub.models.fts5_ddl_statements`, not just the mapper events that
    populate `search_text` -- this is what those triggers exist to
    guarantee."""
    _, outbox_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "before the edit",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    token = await _make_access_token(async_db_session, "read:search")
    headers = {"Authorization": f"Bearer {token}"}

    found = client.get(
        "/api/v2/search",
        params={"q": "before the edit", "type": "statuses"},
        headers=headers,
    )
    assert ids.encode_outbox_id(outbox_object) in {
        s["id"] for s in found.json()["statuses"]
    }

    await boxes.send_update(async_db_session, outbox_object.ap_id, "after the edit")

    stale = client.get(
        "/api/v2/search",
        params={"q": "before the edit", "type": "statuses"},
        headers=headers,
    )
    assert ids.encode_outbox_id(outbox_object) not in {
        s["id"] for s in stale.json()["statuses"]
    }

    updated = client.get(
        "/api/v2/search",
        params={"q": "after the edit", "type": "statuses"},
        headers=headers,
    )
    assert ids.encode_outbox_id(outbox_object) in {
        s["id"] for s in updated.json()["statuses"]
    }

    await boxes.send_delete(async_db_session, outbox_object.ap_id)

    gone = client.get(
        "/api/v2/search",
        params={"q": "after the edit", "type": "statuses"},
        headers=headers,
    )
    assert ids.encode_outbox_id(outbox_object) not in {
        s["id"] for s in gone.json()["statuses"]
    }


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


@pytest.mark.asyncio
async def test_remove_from_followers(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    account_id = ids.encode_account_id(follower.actor)

    token = await _make_access_token(
        async_db_session, "read:accounts read:follows write:follows"
    )
    headers = {"Authorization": f"Bearer {token}"}

    before = client.get(
        f"/api/v1/accounts/relationships?id[]={account_id}", headers=headers
    ).json()
    assert before[0]["followed_by"] is True

    response = client.post(
        f"/api/v1/accounts/{account_id}/remove_from_followers", headers=headers
    )

    assert response.status_code == 200
    assert response.json()["followed_by"] is False
    assert (
        await async_db_session.scalars(select(activitypub.models.Follower))
    ).all() == []

    # The remote server is told with a Reject of their original Follow.
    rejects = (
        await async_db_session.scalars(
            select(activitypub.models.OutboxObject).where(
                activitypub.models.OutboxObject.ap_type == "Reject"
            )
        )
    ).all()
    assert len(rejects) == 1

    # Removing a non-follower is a no-op, not an error.
    again = client.post(
        f"/api/v1/accounts/{account_id}/remove_from_followers", headers=headers
    )
    assert again.status_code == 200
    assert again.json()["followed_by"] is False


@pytest.mark.asyncio
async def test_remove_from_followers_requires_auth(client: TestClient) -> None:
    assert client.post("/api/v1/accounts/1/remove_from_followers").status_code == 401
