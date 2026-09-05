import secrets

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import activitypub.models
from activitypub.tests import factories
from app import models
from app.database import SessionLocal
from app.mastodon import ids
from tests.utils import setup_inbox_note
from tests.utils import setup_outbox_note
from tests.utils import setup_remote_actor
from tests.utils import setup_remote_actor_as_following


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


def _unhide_from_stream(inbox_object: activitypub.models.InboxObject) -> None:
    """The test factory hides every reply from the stream unconditionally
    (`InboxObjectFactory.from_remote_object`), which is a simplification of
    the real ingestion heuristic (`app.customization.default_stream_visibility_callback`).
    Every list-timeline query still goes through `fetch_inbox_timeline_page`,
    which filters on that flag first — so a reply exercising
    `list_timeline_where`'s own policy branches has to get past it, exactly
    as a real mention/local-reply/self-reply already would in production.
    """
    with SessionLocal() as session:
        row = session.get(activitypub.models.InboxObject, inbox_object.id)
        assert row is not None
        row.is_hidden_from_stream = False
        session.commit()


async def _make_list(client: TestClient, token: str, **params: str) -> dict:
    response = client.post(
        "/api/v1/lists",
        headers={"Authorization": f"Bearer {token}"},
        data=params,
    )
    assert response.status_code == 200
    return response.json()


# --- CRUD --------------------------------------------------------------------


def test_lists_endpoints_require_auth(client: TestClient) -> None:
    assert client.get("/api/v1/lists").status_code == 401
    assert client.post("/api/v1/lists").status_code == 401
    assert client.get("/api/v1/lists/1").status_code == 401
    assert client.put("/api/v1/lists/1").status_code == 401
    assert client.delete("/api/v1/lists/1").status_code == 401
    assert client.get("/api/v1/lists/1/accounts").status_code == 401
    assert client.post("/api/v1/lists/1/accounts").status_code == 401
    assert client.delete("/api/v1/lists/1/accounts").status_code == 401
    assert client.get("/api/v1/timelines/list/1").status_code == 401


@pytest.mark.asyncio
async def test_lists_write_endpoints_require_write_scope(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read:lists")
    headers = {"Authorization": f"Bearer {token}"}
    assert (
        client.post("/api/v1/lists", headers=headers, data={"title": "x"}).status_code
        == 403
    )
    assert client.put("/api/v1/lists/1", headers=headers).status_code == 403
    assert client.delete("/api/v1/lists/1", headers=headers).status_code == 403
    assert client.post("/api/v1/lists/1/accounts", headers=headers).status_code == 403
    assert client.delete("/api/v1/lists/1/accounts", headers=headers).status_code == 403


@pytest.mark.asyncio
async def test_lists_create_defaults_and_show(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read:lists write:lists")
    headers = {"Authorization": f"Bearer {token}"}

    created = await _make_list(client, token, title="Friends")
    assert created["title"] == "Friends"
    assert created["replies_policy"] == "list"
    assert created["exclusive"] is False
    assert isinstance(created["id"], str)

    shown = client.get(f"/api/v1/lists/{created['id']}", headers=headers)
    assert shown.status_code == 200
    assert shown.json() == created

    indexed = client.get("/api/v1/lists", headers=headers)
    assert indexed.status_code == 200
    assert indexed.json() == [created]

    assert client.get("/api/v1/lists/404", headers=headers).status_code == 404


@pytest.mark.asyncio
async def test_lists_create_validates_title_and_replies_policy(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:lists")
    headers = {"Authorization": f"Bearer {token}"}

    blank = client.post("/api/v1/lists", headers=headers, data={"title": "  "})
    assert blank.status_code == 422

    bad_policy = client.post(
        "/api/v1/lists",
        headers=headers,
        data={"title": "x", "replies_policy": "nonsense"},
    )
    assert bad_policy.status_code == 422

    custom = client.post(
        "/api/v1/lists",
        headers=headers,
        data={"title": "Close friends", "replies_policy": "none", "exclusive": "true"},
    )
    assert custom.status_code == 200
    assert custom.json()["replies_policy"] == "none"
    assert custom.json()["exclusive"] is True


@pytest.mark.asyncio
async def test_lists_update_is_partial(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read:lists write:lists")
    headers = {"Authorization": f"Bearer {token}"}
    created = await _make_list(client, token, title="Friends")

    # Toggling just `exclusive` doesn't require resending `title`.
    updated = client.put(
        f"/api/v1/lists/{created['id']}", headers=headers, data={"exclusive": "true"}
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "Friends"
    assert updated.json()["exclusive"] is True

    blank_title = client.put(
        f"/api/v1/lists/{created['id']}", headers=headers, data={"title": ""}
    )
    assert blank_title.status_code == 422

    bad_policy = client.put(
        f"/api/v1/lists/{created['id']}",
        headers=headers,
        data={"replies_policy": "nonsense"},
    )
    assert bad_policy.status_code == 422

    assert client.put("/api/v1/lists/404", headers=headers).status_code == 404


@pytest.mark.asyncio
async def test_lists_delete_removes_list_and_members(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    token = await _make_access_token(async_db_session, "read:lists write:lists")
    headers = {"Authorization": f"Bearer {token}"}
    created = await _make_list(client, token, title="Friends")

    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    following = setup_remote_actor_as_following(ra)
    assert following.actor is not None
    account_id = ids.encode_account_id(following.actor)
    add = client.post(
        f"/api/v1/lists/{created['id']}/accounts",
        headers=headers,
        data={"account_ids[]": account_id},
    )
    assert add.status_code == 200

    deleted = client.delete(f"/api/v1/lists/{created['id']}", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json() == {}
    assert (
        client.get(f"/api/v1/lists/{created['id']}", headers=headers).status_code == 404
    )

    remaining = (
        await async_db_session.scalars(select(models.MastodonListMember))
    ).all()
    assert remaining == []


# --- Membership ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_lists_accounts_add_requires_following(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    token = await _make_access_token(async_db_session, "read:lists write:lists")
    headers = {"Authorization": f"Bearer {token}"}
    created = await _make_list(client, token, title="Friends")

    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)
    account_id = ids.encode_account_id(actor)

    response = client.post(
        f"/api/v1/lists/{created['id']}/accounts",
        headers=headers,
        data={"account_ids[]": account_id},
    )
    assert response.status_code == 404

    accounts = client.get(f"/api/v1/lists/{created['id']}/accounts", headers=headers)
    assert accounts.json() == []


@pytest.mark.asyncio
async def test_lists_accounts_add_404s_for_unknown_account(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read:lists write:lists")
    headers = {"Authorization": f"Bearer {token}"}
    created = await _make_list(client, token, title="Friends")

    response = client.post(
        f"/api/v1/lists/{created['id']}/accounts",
        headers=headers,
        data={"account_ids[]": "999999"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_lists_accounts_add_rejects_duplicate_membership(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    token = await _make_access_token(async_db_session, "read:lists write:lists")
    headers = {"Authorization": f"Bearer {token}"}
    created = await _make_list(client, token, title="Friends")

    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    following = setup_remote_actor_as_following(ra)
    assert following.actor is not None
    account_id = ids.encode_account_id(following.actor)

    first = client.post(
        f"/api/v1/lists/{created['id']}/accounts",
        headers=headers,
        data={"account_ids[]": account_id},
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/v1/lists/{created['id']}/accounts",
        headers=headers,
        data={"account_ids[]": account_id},
    )
    assert second.status_code == 422


@pytest.mark.asyncio
async def test_lists_accounts_remove_is_idempotent(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    token = await _make_access_token(async_db_session, "read:lists write:lists")
    headers = {"Authorization": f"Bearer {token}"}
    created = await _make_list(client, token, title="Friends")

    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    following = setup_remote_actor_as_following(ra)
    assert following.actor is not None
    account_id = ids.encode_account_id(following.actor)
    client.post(
        f"/api/v1/lists/{created['id']}/accounts",
        headers=headers,
        data={"account_ids[]": account_id},
    )

    first = client.request(
        "DELETE",
        f"/api/v1/lists/{created['id']}/accounts",
        headers=headers,
        data={"account_ids[]": account_id},
    )
    assert first.status_code == 200
    assert first.json() == {}

    second = client.request(
        "DELETE",
        f"/api/v1/lists/{created['id']}/accounts",
        headers=headers,
        data={"account_ids[]": account_id},
    )
    assert second.status_code == 200

    accounts = client.get(
        f"/api/v1/lists/{created['id']}/accounts", headers=headers
    ).json()
    assert accounts == []


@pytest.mark.asyncio
async def test_lists_accounts_index_paginates_and_limit_zero_is_unpaginated(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    token = await _make_access_token(async_db_session, "read:lists write:lists")
    headers = {"Authorization": f"Bearer {token}"}
    created = await _make_list(client, token, title="Friends")

    account_ids = []
    for i in range(3):
        ra = setup_remote_actor(respx_mock, base_url=f"https://example{i}.com")
        following = setup_remote_actor_as_following(ra)
        assert following.actor is not None
        account_id = ids.encode_account_id(following.actor)
        account_ids.append(account_id)
        client.post(
            f"/api/v1/lists/{created['id']}/accounts",
            headers=headers,
            data={"account_ids[]": account_id},
        )

    paged = client.get(
        f"/api/v1/lists/{created['id']}/accounts?limit=2", headers=headers
    )
    assert paged.status_code == 200
    assert len(paged.json()) == 2
    assert "Link" in paged.headers

    unpaginated = client.get(
        f"/api/v1/lists/{created['id']}/accounts?limit=0", headers=headers
    )
    assert unpaginated.status_code == 200
    assert {a["id"] for a in unpaginated.json()} == set(account_ids)
    assert "Link" not in unpaginated.headers


@pytest.mark.asyncio
async def test_accounts_lists_returns_containing_lists(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    token = await _make_access_token(async_db_session, "read:lists write:lists")
    headers = {"Authorization": f"Bearer {token}"}
    created = await _make_list(client, token, title="Friends")

    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    following = setup_remote_actor_as_following(ra)
    assert following.actor is not None
    account_id = ids.encode_account_id(following.actor)
    client.post(
        f"/api/v1/lists/{created['id']}/accounts",
        headers=headers,
        data={"account_ids[]": account_id},
    )

    response = client.get(f"/api/v1/accounts/{account_id}/lists", headers=headers)
    assert response.status_code == 200
    assert response.json() == [created]


@pytest.mark.asyncio
async def test_accounts_lists_404s_for_the_local_actor(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read:lists")
    response = client.get(
        f"/api/v1/accounts/{ids.LOCAL_ACTOR_ID}/lists",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404


# --- Timeline: membership + replies_policy --------------------------------------


@pytest.mark.asyncio
async def test_timelines_list_restricted_to_members(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    token = await _make_access_token(async_db_session, "read:lists write:lists")
    headers = {"Authorization": f"Bearer {token}"}
    created = await _make_list(client, token, title="Friends")

    member_ra = setup_remote_actor(respx_mock, base_url="https://member.example.com")
    member_following = setup_remote_actor_as_following(member_ra)
    assert member_following.actor is not None
    member_account_id = ids.encode_account_id(member_following.actor)
    client.post(
        f"/api/v1/lists/{created['id']}/accounts",
        headers=headers,
        data={"account_ids[]": member_account_id},
    )
    member_note = setup_inbox_note(member_following.actor, content="From a member")

    outsider_ra = setup_remote_actor(
        respx_mock, base_url="https://outsider.example.com"
    )
    outsider_following = setup_remote_actor_as_following(outsider_ra)
    assert outsider_following.actor is not None
    outsider_note = setup_inbox_note(outsider_following.actor, content="Not a member")

    response = client.get(f"/api/v1/timelines/list/{created['id']}", headers=headers)
    assert response.status_code == 200
    returned_ids = {status["id"] for status in response.json()}
    assert ids.encode_inbox_id(member_note) in returned_ids
    assert ids.encode_inbox_id(outsider_note) not in returned_ids


@pytest.mark.asyncio
async def test_timelines_list_unknown_id_404s(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read:lists")
    response = client.get(
        "/api/v1/timelines/list/404",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404


async def _list_with_member_and_stranger(
    client: TestClient,
    token: str,
    respx_mock: respx.MockRouter,
    replies_policy: str,
):
    headers = {"Authorization": f"Bearer {token}"}
    created = await _make_list(
        client, token, title=f"List-{replies_policy}", replies_policy=replies_policy
    )

    member_ra = setup_remote_actor(
        respx_mock, base_url=f"https://member-{replies_policy}.example.com"
    )
    member_following = setup_remote_actor_as_following(member_ra)
    assert member_following.actor is not None
    member_account_id = ids.encode_account_id(member_following.actor)
    client.post(
        f"/api/v1/lists/{created['id']}/accounts",
        headers=headers,
        data={"account_ids[]": member_account_id},
    )

    other_member_ra = setup_remote_actor(
        respx_mock, base_url=f"https://other-member-{replies_policy}.example.com"
    )
    other_member_following = setup_remote_actor_as_following(other_member_ra)
    assert other_member_following.actor is not None
    other_member_account_id = ids.encode_account_id(other_member_following.actor)
    client.post(
        f"/api/v1/lists/{created['id']}/accounts",
        headers=headers,
        data={"account_ids[]": other_member_account_id},
    )

    stranger_ra = setup_remote_actor(
        respx_mock, base_url=f"https://stranger-{replies_policy}.example.com"
    )
    stranger_following = setup_remote_actor_as_following(stranger_ra)
    assert stranger_following.actor is not None

    return (
        created,
        member_following.actor,
        other_member_following.actor,
        stranger_following.actor,
    )


@pytest.mark.asyncio
async def test_timelines_list_replies_policy_followed_shows_every_reply(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    token = await _make_access_token(async_db_session, "read:lists write:lists")
    headers = {"Authorization": f"Bearer {token}"}
    created, member, other_member, stranger = await _list_with_member_and_stranger(
        client, token, respx_mock, "followed"
    )
    assert other_member is not None

    stranger_root = setup_inbox_note(stranger, content="Stranger's root")
    reply_to_stranger = setup_inbox_note(
        member, content="Reply to a non-member", in_reply_to=stranger_root.ap_id
    )
    _unhide_from_stream(reply_to_stranger)

    response = client.get(f"/api/v1/timelines/list/{created['id']}", headers=headers)
    returned_ids = {status["id"] for status in response.json()}
    assert ids.encode_inbox_id(reply_to_stranger) in returned_ids


@pytest.mark.asyncio
async def test_timelines_list_replies_policy_list_exemptions(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    token = await _make_access_token(async_db_session, "read:lists write:lists")
    headers = {"Authorization": f"Bearer {token}"}
    created, member, other_member, stranger = await _list_with_member_and_stranger(
        client, token, respx_mock, "list"
    )

    owner_post = setup_outbox_note(content="Owner's post")

    member_root = setup_inbox_note(member, content="Member's own root")

    self_reply = setup_inbox_note(
        member, content="Self reply", in_reply_to=member_root.ap_id
    )
    _unhide_from_stream(self_reply)

    reply_to_owner = setup_inbox_note(
        member, content="Reply to owner", in_reply_to=owner_post.ap_id
    )
    _unhide_from_stream(reply_to_owner)

    other_member_root = setup_inbox_note(other_member, content="Other member's root")
    reply_to_other_member = setup_inbox_note(
        member,
        content="Reply to another member",
        in_reply_to=other_member_root.ap_id,
    )
    _unhide_from_stream(reply_to_other_member)

    stranger_root = setup_inbox_note(stranger, content="Stranger's root")
    reply_to_stranger = setup_inbox_note(
        member, content="Reply to a stranger", in_reply_to=stranger_root.ap_id
    )
    _unhide_from_stream(reply_to_stranger)

    response = client.get(f"/api/v1/timelines/list/{created['id']}", headers=headers)
    returned_ids = {status["id"] for status in response.json()}

    assert ids.encode_inbox_id(member_root) in returned_ids
    assert ids.encode_inbox_id(self_reply) in returned_ids
    assert ids.encode_inbox_id(reply_to_owner) in returned_ids
    assert ids.encode_inbox_id(reply_to_other_member) in returned_ids
    assert ids.encode_inbox_id(reply_to_stranger) not in returned_ids


@pytest.mark.asyncio
async def test_timelines_list_replies_policy_none_exemptions(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    token = await _make_access_token(async_db_session, "read:lists write:lists")
    headers = {"Authorization": f"Bearer {token}"}
    created, member, other_member, _stranger = await _list_with_member_and_stranger(
        client, token, respx_mock, "none"
    )

    owner_post = setup_outbox_note(content="Owner's post")
    member_root = setup_inbox_note(member, content="Member's own root")

    self_reply = setup_inbox_note(
        member, content="Self reply", in_reply_to=member_root.ap_id
    )
    _unhide_from_stream(self_reply)

    reply_to_owner = setup_inbox_note(
        member, content="Reply to owner", in_reply_to=owner_post.ap_id
    )
    _unhide_from_stream(reply_to_owner)

    other_member_root = setup_inbox_note(other_member, content="Other member's root")
    reply_to_other_member = setup_inbox_note(
        member,
        content="Reply to another member",
        in_reply_to=other_member_root.ap_id,
    )
    _unhide_from_stream(reply_to_other_member)

    response = client.get(f"/api/v1/timelines/list/{created['id']}", headers=headers)
    returned_ids = {status["id"] for status in response.json()}

    assert ids.encode_inbox_id(member_root) in returned_ids
    assert ids.encode_inbox_id(self_reply) in returned_ids
    assert ids.encode_inbox_id(reply_to_owner) in returned_ids
    # `none` doesn't exempt a reply to another list member, unlike `list`.
    assert ids.encode_inbox_id(reply_to_other_member) not in returned_ids


# --- exclusive -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exclusive_list_hides_member_from_home_not_list_or_owner(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    token = await _make_access_token(
        async_db_session, "read:lists write:lists read:statuses"
    )
    headers = {"Authorization": f"Bearer {token}"}
    created = await _make_list(client, token, title="Inner circle", exclusive="true")

    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    following = setup_remote_actor_as_following(ra)
    assert following.actor is not None
    account_id = ids.encode_account_id(following.actor)
    client.post(
        f"/api/v1/lists/{created['id']}/accounts",
        headers=headers,
        data={"account_ids[]": account_id},
    )

    member_note = setup_inbox_note(following.actor, content="From the inner circle")
    owner_post = setup_outbox_note(content="Owner's own post")

    home = client.get("/api/v1/timelines/home", headers=headers)
    home_ids = {status["id"] for status in home.json()}
    assert ids.encode_inbox_id(member_note) not in home_ids
    assert ids.encode_outbox_id(owner_post) in home_ids

    list_timeline = client.get(
        f"/api/v1/timelines/list/{created['id']}", headers=headers
    )
    list_ids = {status["id"] for status in list_timeline.json()}
    assert ids.encode_inbox_id(member_note) in list_ids
