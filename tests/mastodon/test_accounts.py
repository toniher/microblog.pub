import secrets

import httpx
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


def test_accounts_show_remote_actor_fetches_counts(
    client: TestClient, respx_mock: respx.MockRouter
) -> None:
    # Regression: a non-followed/never-interacted-with actor's profile used
    # to always show 0 posts/followers/following, even though the remote
    # actor exposes real totals on their AP collections.
    #
    # Must be registered before `setup_remote_actor`'s own mock for the bare
    # actor URL: respx matches routes in registration order, and a route for
    # a path-less URL matches any path under that host, so it would
    # otherwise shadow these more specific ones.
    respx_mock.get("https://example.com/followers").mock(
        return_value=httpx.Response(
            200, json={"type": "OrderedCollection", "totalItems": 12}
        )
    )
    respx_mock.get("https://example.com/following").mock(
        return_value=httpx.Response(
            200, json={"type": "OrderedCollection", "totalItems": 34}
        )
    )
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)

    response = client.get(f"/api/v1/accounts/{ids.encode_account_id(follower.actor)}")

    assert response.status_code == 200
    data = response.json()
    assert data["followers_count"] == 12
    assert data["following_count"] == 34

    # A second view within the throttle window must not refetch.
    call_count_after_first_view = respx_mock.calls.call_count
    response = client.get(f"/api/v1/accounts/{ids.encode_account_id(follower.actor)}")
    assert response.json()["followers_count"] == 12
    assert respx_mock.calls.call_count == call_count_after_first_view


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


def test_accounts_index_requires_auth(client: TestClient) -> None:
    response = client.get(f"/api/v1/accounts?id[]={ids.LOCAL_ACTOR_ID}")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_accounts_index_owner(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read:accounts")

    response = client.get(
        f"/api/v1/accounts?id[]={ids.LOCAL_ACTOR_ID}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["id"] == ids.LOCAL_ACTOR_ID
    assert data[0]["username"] == config.USERNAME


@pytest.mark.asyncio
async def test_accounts_index_unknown_id_silently_skipped(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read:accounts")

    response = client.get(
        f"/api/v1/accounts?id[]=999999&id[]={ids.LOCAL_ACTOR_ID}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["id"] == ids.LOCAL_ACTOR_ID


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


@pytest.mark.asyncio
async def test_accounts_statuses_owner_includes_own_boosts(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    # Regression test: the owner's own boosts are stored as an OutboxObject
    # with ap_type="Announce", but this endpoint's owner-branch query used to
    # hardcode ["Note", "Article", "Question"], silently excluding them from
    # the owner's own profile (unlike a remote actor's, which already
    # included "Announce").
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    remote_note = RemoteObject(
        factories.build_note_object(from_remote_actor=ra, content="From afar"),
        ra,
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        remote_note, follower.actor
    )

    await boxes.send_announce(async_db_session, inbox_object.ap_id)

    response = client.get(f"/api/v1/accounts/{ids.LOCAL_ACTOR_ID}/statuses")

    assert response.status_code == 200
    reblogged_uris = [
        status["reblog"]["uri"] for status in response.json() if status["reblog"]
    ]
    assert inbox_object.ap_id in reblogged_uris


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


def test_accounts_statuses_non_followed_actor_backfills_from_outbox(
    client: TestClient, respx_mock: respx.MockRouter
) -> None:
    # Regression test: an actor we've never followed (nor who follows us) has
    # no InboxObject cached locally, so this used to silently return an empty
    # list even though the actor has posts. We should backfill on demand.
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)

    note = factories.build_note_object(from_remote_actor=ra, content="From a stranger")
    create_activity = factories.build_create_activity(note)
    respx_mock.get(ra.ap_id + "/outbox").mock(
        return_value=httpx.Response(
            200,
            json={
                "@context": ap.AS_EXTENDED_CTX,
                "id": ra.ap_id + "/outbox",
                "type": "OrderedCollection",
                "totalItems": 1,
                "orderedItems": [create_activity],
            },
        )
    )

    # setup_remote_actor's actor mock matches the bare host with no path
    # constraint, so it would otherwise shadow any route registered after it
    # for the activity fetch below (respx matches in registration order).
    # Re-mock it in place with a side effect that also serves the activity.
    def _serve_actor_or_activity(request: httpx.Request) -> httpx.Response:
        if str(request.url) == create_activity["id"]:
            return httpx.Response(200, json=create_activity)
        return httpx.Response(200, json=ra.ap_actor)

    respx_mock.get(ra.ap_id).mock(side_effect=_serve_actor_or_activity)

    response = client.get(f"/api/v1/accounts/{ids.encode_account_id(actor)}/statuses")

    assert response.status_code == 200
    returned_uris = [status["uri"] for status in response.json()]
    assert note["id"] in returned_uris


def test_accounts_statuses_non_followed_actor_backfill_is_throttled(
    client: TestClient, respx_mock: respx.MockRouter
) -> None:
    # A client polling a stranger's profile repeatedly shouldn't trigger a
    # live outbox fetch on every request.
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)
    # Re-declaring the same route without immediately chaining .mock() would
    # reset it to unconfigured, so re-supply the same empty-collection body
    # setup_remote_actor already installed, purely to get a handle for
    # call_count below.
    outbox_route = respx_mock.get(ra.ap_id + "/outbox").mock(
        return_value=httpx.Response(
            200,
            json={
                "@context": ap.AS_EXTENDED_CTX,
                "id": ra.ap_id + "/outbox",
                "type": "OrderedCollection",
                "totalItems": 0,
                "orderedItems": [],
            },
        )
    )

    url = f"/api/v1/accounts/{ids.encode_account_id(actor)}/statuses"
    first = client.get(url)
    second = client.get(url)

    assert first.status_code == 200
    assert second.status_code == 200
    assert outbox_route.call_count == 1


def test_accounts_statuses_backfill_races_concurrent_writer(
    client: TestClient,
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression test: two clients (or a concurrent backfill and a normal
    # inbox delivery) can both see "not cached yet" for the same post and
    # both try to save it, so the loser's INSERT hits inbox.ap_id's unique
    # constraint. That used to crash the whole request with a
    # PendingRollbackError, because the exception handler logged
    # `actor.ap_id` -- an ORM attribute access that can trigger a reload --
    # before rolling back the now-broken session. Simulate the race by
    # forcing prefetch's "already saved?" check to say no even though a row
    # with the same ap_id already exists.
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)

    note = factories.build_note_object(from_remote_actor=ra, content="Raced post")
    create_activity = factories.build_create_activity(note)
    respx_mock.get(ra.ap_id + "/outbox").mock(
        return_value=httpx.Response(
            200,
            json={
                "@context": ap.AS_EXTENDED_CTX,
                "id": ra.ap_id + "/outbox",
                "type": "OrderedCollection",
                "totalItems": 1,
                "orderedItems": [create_activity],
            },
        )
    )

    def _serve_actor_or_activity(request: httpx.Request) -> httpx.Response:
        if str(request.url) == create_activity["id"]:
            return httpx.Response(200, json=create_activity)
        return httpx.Response(200, json=ra.ap_actor)

    respx_mock.get(ra.ap_id).mock(side_effect=_serve_actor_or_activity)

    # The "concurrent writer" that already saved this exact post.
    existing = factories.InboxObjectFactory.from_remote_object(
        RemoteObject(note, ra), actor
    )

    async def _always_missing(db_session, ap_id):
        return None

    monkeypatch.setattr(boxes, "get_inbox_object_by_ap_id", _always_missing)

    response = client.get(f"/api/v1/accounts/{ids.encode_account_id(actor)}/statuses")

    assert response.status_code == 200
    returned_ids = [status["id"] for status in response.json()]
    assert ids.encode_inbox_id(existing) in returned_ids


def test_accounts_statuses_backfill_failure_does_not_crash(
    client: TestClient, respx_mock: respx.MockRouter
) -> None:
    # Regression test: a failed backfill attempt (bad remote response, or a
    # concurrent writer racing us to save the same post) used to crash with a
    # PendingRollbackError because the exception handler logged actor.ap_id
    # -- an ORM attribute access -- before rolling back the broken session.
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)
    respx_mock.get(ra.ap_id + "/outbox").mock(
        return_value=httpx.Response(200, json={"no": "type field"})
    )

    response = client.get(f"/api/v1/accounts/{ids.encode_account_id(actor)}/statuses")

    assert response.status_code == 200
    assert response.json() == []


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
