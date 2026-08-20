import io
import secrets
from datetime import timedelta
from uuid import uuid4

import pytest
import respx
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

import activitypub.models
from activitypub import activitypub as ap
from activitypub import boxes
from activitypub.ap_object import ObjectType
from activitypub.ap_object import RemoteObject
from activitypub.tests import factories
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


def _png_bytes(size: tuple[int, int] = (16, 12)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def _build_poll_object(
    from_remote_actor, options: list[str], multiple: bool = False, expired: bool = False
) -> ap.RawObject:
    published = now().replace(microsecond=0).isoformat().replace("+00:00", "Z")
    context = from_remote_actor.ap_id + "/ctx/" + uuid4().hex
    poll_id = uuid4().hex
    end_delta = timedelta(seconds=-60) if expired else timedelta(hours=1)
    end_time = (
        (now() + end_delta).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    key = "anyOf" if multiple else "oneOf"
    return {
        "@context": ap.AS_CTX,
        "type": "Question",
        "id": from_remote_actor.ap_id + "/poll/" + poll_id,
        "attributedTo": from_remote_actor.ap_id,
        "content": "Pick one",
        "to": [ap.AS_PUBLIC],
        "cc": [],
        "published": published,
        "context": context,
        "conversation": context,
        "url": from_remote_actor.ap_id + "/poll/" + poll_id,
        "tag": [],
        "summary": None,
        "sensitive": False,
        "endTime": end_time,
        key: [
            {
                "type": "Note",
                "name": option,
                "replies": {"type": "Collection", "totalItems": 0},
            }
            for option in options
        ],
    }


@pytest.mark.asyncio
async def test_statuses_create_basic(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:statuses")

    response = client.post(
        "/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}"},
        data={"status": "Hello from Tusky", "visibility": "unlisted"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["content"] == "<p>Hello from Tusky</p>\n"
    assert data["visibility"] == "unlisted"
    assert data["account"]["id"] == ids.LOCAL_ACTOR_ID


@pytest.mark.asyncio
async def test_statuses_create_json_body(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    # Regression test: some clients (e.g. Tusky) POST this endpoint as
    # `application/json` rather than form-encoded. Starlette's `Request.form()`
    # silently returns empty data for a JSON body, which used to turn this
    # into a 422 "status is required".
    token = await _make_access_token(async_db_session, "write:statuses")

    response = client.post(
        "/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "Hello from Tusky", "visibility": "unlisted"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["content"] == "<p>Hello from Tusky</p>\n"
    assert data["visibility"] == "unlisted"
    assert data["account"]["id"] == ids.LOCAL_ACTOR_ID


def test_statuses_create_requires_auth(client: TestClient) -> None:
    response = client.post("/api/v1/statuses", data={"status": "no token"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_statuses_create_requires_status_or_media(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:statuses")

    response = client.post(
        "/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}"},
        data={},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_statuses_create_invalid_visibility(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:statuses")

    response = client.post(
        "/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}"},
        data={"status": "hi", "visibility": "bogus"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_statuses_create_with_reply(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:statuses")
    headers = {"Authorization": f"Bearer {token}"}

    _, root = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Root",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    root_id = ids.encode_outbox_id(root)

    response = client.post(
        "/api/v1/statuses",
        headers=headers,
        data={"status": "A reply", "in_reply_to_id": root_id},
    )

    assert response.status_code == 200
    assert response.json()["in_reply_to_id"] == root_id


@pytest.mark.asyncio
async def test_statuses_create_with_quote(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:statuses")
    headers = {"Authorization": f"Bearer {token}"}

    _, quoted = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Original",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    quoted_id = ids.encode_outbox_id(quoted)

    response = client.post(
        "/api/v1/statuses",
        headers=headers,
        data={"status": "Check this out", "quote_id": quoted_id},
    )

    assert response.status_code == 200
    data = response.json()
    # Quoting our own post is auto-authorized -- no round trip needed.
    assert data["quote"]["state"] == "accepted"
    assert data["quote"]["quoted_status"]["id"] == quoted_id


@pytest.mark.asyncio
async def test_statuses_create_with_unknown_quote_id(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:statuses")

    response = client.post(
        "/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}"},
        data={"status": "Check this out", "quote_id": "999999999"},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_statuses_create_with_poll(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:statuses")

    response = client.post(
        "/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "status": "Pick one",
            "poll[options][]": ["Cats", "Dogs"],
            "poll[expires_in]": "3600",
        },
    )

    assert response.status_code == 200
    poll = response.json()["poll"]
    assert poll is not None
    assert [o["title"] for o in poll["options"]] == ["Cats", "Dogs"]
    assert poll["multiple"] is False


@pytest.mark.asyncio
async def test_statuses_create_with_poll_json_body(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:statuses")

    response = client.post(
        "/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "status": "Pick one",
            "poll": {"options": ["Cats", "Dogs"], "expires_in": 3600},
        },
    )

    assert response.status_code == 200
    poll = response.json()["poll"]
    assert poll is not None
    assert [o["title"] for o in poll["options"]] == ["Cats", "Dogs"]
    assert poll["multiple"] is False


@pytest.mark.asyncio
async def test_statuses_create_idempotency_key_avoids_duplicate(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:statuses")
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "retry-key-1",
    }

    first = client.post(
        "/api/v1/statuses", headers=headers, data={"status": "Only once"}
    )
    second = client.post(
        "/api/v1/statuses", headers=headers, data={"status": "Only once"}
    )

    assert first.json()["id"] == second.json()["id"]


@pytest.mark.asyncio
async def test_statuses_delete_own_status(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:statuses")

    _, outbox_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Delete me",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    status_id = ids.encode_outbox_id(outbox_object)

    response = client.delete(
        f"/api/v1/statuses/{status_id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["text"] == "Delete me"

    assert client.get(f"/api/v1/statuses/{status_id}").status_code == 404


@pytest.mark.asyncio
async def test_statuses_delete_not_found(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:statuses")
    response = client.delete(
        "/api/v1/statuses/999999", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_favourite_and_unfavourite_remote_note(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    remote_note = RemoteObject(
        factories.build_note_object(from_remote_actor=ra, content="Nice post"), ra
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        remote_note, follower.actor
    )
    status_id = ids.encode_inbox_id(inbox_object)

    token = await _make_access_token(async_db_session, "write:favourites")
    headers = {"Authorization": f"Bearer {token}"}

    faved = client.post(
        f"/api/v1/statuses/{status_id}/favourite", headers=headers
    ).json()
    assert faved["favourited"] is True
    assert faved["favourites_count"] == 0  # count is only tracked for own posts

    unfaved = client.post(
        f"/api/v1/statuses/{status_id}/unfavourite", headers=headers
    ).json()
    assert unfaved["favourited"] is False


@pytest.mark.asyncio
async def test_reblog_and_unreblog_remote_note(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    remote_note = RemoteObject(
        factories.build_note_object(from_remote_actor=ra, content="Boost me"), ra
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        remote_note, follower.actor
    )
    status_id = ids.encode_inbox_id(inbox_object)

    token = await _make_access_token(async_db_session, "write:statuses")
    headers = {"Authorization": f"Bearer {token}"}

    reblogged = client.post(
        f"/api/v1/statuses/{status_id}/reblog", headers=headers
    ).json()
    assert reblogged["reblogged"] is True

    unreblogged = client.post(
        f"/api/v1/statuses/{status_id}/unreblog", headers=headers
    ).json()
    assert unreblogged["reblogged"] is False


@pytest.mark.asyncio
async def test_bookmark_and_unbookmark(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    remote_note = RemoteObject(
        factories.build_note_object(from_remote_actor=ra, content="Save this"), ra
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        remote_note, follower.actor
    )
    status_id = ids.encode_inbox_id(inbox_object)

    token = await _make_access_token(async_db_session, "write:bookmarks read:bookmarks")
    headers = {"Authorization": f"Bearer {token}"}

    bookmarked = client.post(
        f"/api/v1/statuses/{status_id}/bookmark", headers=headers
    ).json()
    assert bookmarked["bookmarked"] is True

    listed = client.get("/api/v1/bookmarks", headers=headers).json()
    assert status_id in {s["id"] for s in listed}

    unbookmarked = client.post(
        f"/api/v1/statuses/{status_id}/unbookmark", headers=headers
    ).json()
    assert unbookmarked["bookmarked"] is False


@pytest.mark.asyncio
async def test_mute_and_unmute_conversation(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    remote_note = RemoteObject(
        factories.build_note_object(from_remote_actor=ra, content="Noisy thread"), ra
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        remote_note, follower.actor
    )
    status_id = ids.encode_inbox_id(inbox_object)

    token = await _make_access_token(async_db_session, "write:mutes")
    headers = {"Authorization": f"Bearer {token}"}

    muted = client.post(f"/api/v1/statuses/{status_id}/mute", headers=headers).json()
    assert muted["muted"] is True

    # Idempotent: muting an already-muted conversation doesn't error.
    muted_again = client.post(
        f"/api/v1/statuses/{status_id}/mute", headers=headers
    ).json()
    assert muted_again["muted"] is True

    unmuted = client.post(
        f"/api/v1/statuses/{status_id}/unmute", headers=headers
    ).json()
    assert unmuted["muted"] is False


@pytest.mark.asyncio
async def test_pin_and_unpin_own_status(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:accounts")
    headers = {"Authorization": f"Bearer {token}"}

    _, outbox_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Pin me",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    status_id = ids.encode_outbox_id(outbox_object)

    pinned = client.post(f"/api/v1/statuses/{status_id}/pin", headers=headers).json()
    assert pinned["pinned"] is True

    unpinned = client.post(
        f"/api/v1/statuses/{status_id}/unpin", headers=headers
    ).json()
    assert unpinned["pinned"] is False


@pytest.mark.asyncio
async def test_pin_enforces_max_pinned_limit(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:accounts")
    headers = {"Authorization": f"Bearer {token}"}

    for i in range(activitypub.models.MAX_PINNED_OBJECTS):
        _, outbox_object = await boxes.send_create(
            async_db_session,
            ObjectType.NOTE.value,
            f"Pin me {i}",
            uploads=[],
            in_reply_to=None,
            visibility=ap.VisibilityEnum.PUBLIC,
        )
        status_id = ids.encode_outbox_id(outbox_object)
        pinned = client.post(
            f"/api/v1/statuses/{status_id}/pin", headers=headers
        ).json()
        assert pinned["pinned"] is True

    _, one_too_many = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "One too many",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    status_id = ids.encode_outbox_id(one_too_many)
    response = client.post(f"/api/v1/statuses/{status_id}/pin", headers=headers)
    assert response.status_code == 422
    assert "maximum number" in response.json()["error_description"]


@pytest.mark.asyncio
async def test_pin_remote_status_rejected(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    remote_note = RemoteObject(factories.build_note_object(from_remote_actor=ra), ra)
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        remote_note, follower.actor
    )
    status_id = ids.encode_inbox_id(inbox_object)

    token = await _make_access_token(async_db_session, "write:accounts")
    response = client.post(
        f"/api/v1/statuses/{status_id}/pin",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_poll_vote_single_choice(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    poll_object = RemoteObject(_build_poll_object(ra, ["Cats", "Dogs"]), ra)
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        poll_object, follower.actor
    )
    poll_id = ids.encode_inbox_id(inbox_object)

    token = await _make_access_token(async_db_session, "write:statuses")
    headers = {"Authorization": f"Bearer {token}"}

    response = client.post(
        f"/api/v1/polls/{poll_id}/votes",
        headers=headers,
        data={"choices[]": "1"},
    )

    assert response.status_code == 200
    poll = response.json()
    assert poll["own_votes"] == [1]
    assert poll["voted"] is True

    refetched = client.get(f"/api/v1/polls/{poll_id}", headers=headers).json()
    assert refetched["own_votes"] == [1]


@pytest.mark.asyncio
async def test_poll_vote_with_choices_in_query_string(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    """Ice Cubes votes via `?choices[]=0` with an empty body (as Rails, and so
    Mastodon, makes no distinction between query and body params)."""
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    poll_object = RemoteObject(_build_poll_object(ra, ["Cats", "Dogs"]), ra)
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        poll_object, follower.actor
    )
    poll_id = ids.encode_inbox_id(inbox_object)

    token = await _make_access_token(async_db_session, "write:statuses")
    headers = {"Authorization": f"Bearer {token}"}

    response = client.post(
        f"/api/v1/polls/{poll_id}/votes?choices%5B%5D=0",
        headers=headers,
    )

    assert response.status_code == 200
    poll = response.json()
    assert poll["own_votes"] == [0]
    assert poll["voted"] is True


@pytest.mark.asyncio
async def test_poll_vote_rejects_multiple_choices_on_single_choice_poll(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    poll_object = RemoteObject(
        _build_poll_object(ra, ["Cats", "Dogs", "Birds"], multiple=False), ra
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        poll_object, follower.actor
    )
    poll_id = ids.encode_inbox_id(inbox_object)

    token = await _make_access_token(async_db_session, "write:statuses")
    response = client.post(
        f"/api/v1/polls/{poll_id}/votes",
        headers={"Authorization": f"Bearer {token}"},
        data={"choices[]": ["0", "1"]},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_poll_vote_rejects_ended_poll(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    poll_object = RemoteObject(
        _build_poll_object(ra, ["Cats", "Dogs"], expired=True), ra
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        poll_object, follower.actor
    )
    poll_id = ids.encode_inbox_id(inbox_object)

    token = await _make_access_token(async_db_session, "write:statuses")
    response = client.post(
        f"/api/v1/polls/{poll_id}/votes",
        headers={"Authorization": f"Bearer {token}"},
        data={"choices[]": "0"},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_statuses_source(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read:statuses write:statuses")

    create_response = client.post(
        "/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}"},
        data={"status": "Original text", "spoiler_text": "cw"},
    )
    status_id = create_response.json()["id"]

    response = client.get(
        f"/api/v1/statuses/{status_id}/source",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data == {"id": status_id, "text": "Original text", "spoiler_text": "cw"}


def test_statuses_source_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/statuses/1/source")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_statuses_source_not_found(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read:statuses")
    response = client.get(
        "/api/v1/statuses/999999/source",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_statuses_update_basic(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    # Regression test: Tusky/Fedilab both call GET .../source then
    # PUT /api/v1/statuses/{id} to edit a post — neither endpoint existed
    # before, so editing always failed.
    token = await _make_access_token(async_db_session, "read:statuses write:statuses")

    create_response = client.post(
        "/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}"},
        data={"status": "Original text"},
    )
    status_id = create_response.json()["id"]
    assert create_response.json()["edited_at"] is None

    response = client.put(
        f"/api/v1/statuses/{status_id}",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "status": "Edited text",
            "spoiler_text": "now sensitive",
            "sensitive": "true",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == status_id
    assert data["content"] == "<p>Edited text</p>\n"
    assert data["spoiler_text"] == "now sensitive"
    assert data["sensitive"] is True
    assert data["edited_at"] is not None

    source_response = client.get(
        f"/api/v1/statuses/{status_id}/source",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert source_response.json()["text"] == "Edited text"


@pytest.mark.asyncio
async def test_statuses_update_json_body(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:statuses")

    create_response = client.post(
        "/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "Original text"},
    )
    status_id = create_response.json()["id"]

    response = client.put(
        f"/api/v1/statuses/{status_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "Edited via JSON"},
    )

    assert response.status_code == 200
    assert response.json()["content"] == "<p>Edited via JSON</p>\n"


@pytest.mark.asyncio
async def test_statuses_update_without_media_ids_preserves_attachments(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    media_token = await _make_access_token(async_db_session, "write:media")
    media_response = client.post(
        "/api/v2/media",
        headers={"Authorization": f"Bearer {media_token}"},
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    )
    media_id = media_response.json()["id"]

    token = await _make_access_token(async_db_session, "write:statuses")
    create_response = client.post(
        "/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}"},
        data={"status": "With media", "media_ids[]": [media_id]},
    )
    status_id = create_response.json()["id"]
    assert len(create_response.json()["media_attachments"]) == 1

    response = client.put(
        f"/api/v1/statuses/{status_id}",
        headers={"Authorization": f"Bearer {token}"},
        data={"status": "Edited, media untouched"},
    )

    assert response.status_code == 200
    assert len(response.json()["media_attachments"]) == 1


@pytest.mark.asyncio
async def test_status_media_attachment_carries_focus(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    media_token = await _make_access_token(async_db_session, "write:media")
    media_response = client.post(
        "/api/v2/media",
        headers={"Authorization": f"Bearer {media_token}"},
        files={"file": ("photo.png", _png_bytes(), "image/png")},
        data={"focus": "-0.5,0.7"},
    )
    media_id = media_response.json()["id"]

    token = await _make_access_token(async_db_session, "write:statuses")
    create_response = client.post(
        "/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}"},
        data={"status": "With a focal point", "media_ids[]": [media_id]},
    )

    assert create_response.status_code == 200
    attachment = create_response.json()["media_attachments"][0]
    assert attachment["meta"]["focus"] == {"x": -0.5, "y": 0.7}


@pytest.mark.asyncio
async def test_statuses_update_replaces_media_ids(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    media_token = await _make_access_token(async_db_session, "write:media")
    media_response = client.post(
        "/api/v2/media",
        headers={"Authorization": f"Bearer {media_token}"},
        files={"file": ("photo.png", _png_bytes(), "image/png")},
    )
    media_id = media_response.json()["id"]

    token = await _make_access_token(async_db_session, "write:statuses")
    create_response = client.post(
        "/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}"},
        data={"status": "With media", "media_ids[]": [media_id]},
    )
    status_id = create_response.json()["id"]

    # An explicitly-empty list only round-trips over JSON: `multipart/form-data`
    # has no way to distinguish a present-but-empty array from an absent field
    # (httpx drops a `media_ids[]: []` form field entirely), so clearing every
    # attachment requires a JSON-body edit.
    response = client.put(
        f"/api/v1/statuses/{status_id}",
        headers={"Authorization": f"Bearer {token}"},
        json={"status": "No more media", "media_ids": []},
    )

    assert response.status_code == 200
    assert response.json()["media_attachments"] == []


def test_statuses_update_requires_auth(client: TestClient) -> None:
    response = client.put("/api/v1/statuses/1", data={"status": "no token"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_statuses_update_not_found(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:statuses")
    response = client.put(
        "/api/v1/statuses/999999",
        headers={"Authorization": f"Bearer {token}"},
        data={"status": "hi"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_statuses_update_requires_status(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:statuses")
    create_response = client.post(
        "/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}"},
        data={"status": "Original text"},
    )
    status_id = create_response.json()["id"]

    response = client.put(
        f"/api/v1/statuses/{status_id}",
        headers={"Authorization": f"Bearer {token}"},
        data={"status": ""},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_statuses_history_never_edited_returns_single_entry(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:statuses")
    create_response = client.post(
        "/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}"},
        data={"status": "Original text"},
    )
    status_id = create_response.json()["id"]

    response = client.get(f"/api/v1/statuses/{status_id}/history")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["content"] == "<p>Original text</p>\n"
    assert data[0]["account"]["id"] == create_response.json()["account"]["id"]


@pytest.mark.asyncio
async def test_statuses_history_lists_all_edits_in_ascending_order(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read:statuses write:statuses")
    create_response = client.post(
        "/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}"},
        data={"status": "Version one"},
    )
    status_id = create_response.json()["id"]

    client.put(
        f"/api/v1/statuses/{status_id}",
        headers={"Authorization": f"Bearer {token}"},
        data={"status": "Version two"},
    )
    client.put(
        f"/api/v1/statuses/{status_id}",
        headers={"Authorization": f"Bearer {token}"},
        data={"status": "Version three", "sensitive": "true"},
    )

    response = client.get(f"/api/v1/statuses/{status_id}/history")

    assert response.status_code == 200
    data = response.json()
    assert [entry["content"] for entry in data] == [
        "<p>Version one</p>\n",
        "<p>Version two</p>\n",
        "<p>Version three</p>\n",
    ]
    assert data[-1]["sensitive"] is True
    assert data[0]["sensitive"] is False
    # Ascending order: each entry's created_at is <= the next's.
    assert data[0]["created_at"] <= data[1]["created_at"] <= data[2]["created_at"]


def test_statuses_history_not_found_for_remote_status(
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    remote_note = RemoteObject(
        factories.build_note_object(from_remote_actor=ra, content="From afar"),
        ra,
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        remote_note, follower.actor
    )

    status_id = ids.encode_inbox_id(inbox_object)
    response = client.get(f"/api/v1/statuses/{status_id}/history")
    assert response.status_code == 404


def test_statuses_history_not_found(client: TestClient) -> None:
    response = client.get("/api/v1/statuses/999999/history")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_statuses_create_with_poll_indexed_form_keys(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    """Some HTTP clients encode a list as `poll[options][0]`, `poll[options][1]`.

    Unrecognized, the poll used to vanish from the request and the status posted
    as a plain note.
    """
    token = await _make_access_token(async_db_session, "write:statuses")

    response = client.post(
        "/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "status": "Pick one",
            "poll[options][0]": "Cats",
            "poll[options][1]": "Dogs",
            "poll[options][2]": "Birds",
            "poll[expires_in]": "3600",
        },
    )

    assert response.status_code == 200
    poll = response.json()["poll"]
    assert poll is not None
    assert [o["title"] for o in poll["options"]] == ["Cats", "Dogs", "Birds"]


@pytest.mark.asyncio
async def test_statuses_create_poll_enforces_advertised_limits(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    """The limits `/api/v1/instance` advertises are the limits actually applied."""
    token = await _make_access_token(async_db_session, "write:statuses")
    headers = {"Authorization": f"Bearer {token}"}

    advertised = client.get("/api/v1/instance").json()["configuration"]["polls"]
    assert advertised == {
        "max_options": 4,
        "max_characters_per_option": 100,
        "min_expiration": 300,
        "max_expiration": 2_629_746,
    }

    too_many = client.post(
        "/api/v1/statuses",
        headers=headers,
        json={
            "status": "Too many",
            "poll": {"options": ["a", "b", "c", "d", "e"], "expires_in": 3600},
        },
    )
    assert too_many.status_code == 422
    assert "at most 4" in too_many.json()["error_description"]

    too_long = client.post(
        "/api/v1/statuses",
        headers=headers,
        json={
            "status": "Too long",
            "poll": {"options": ["a" * 101, "b"], "expires_in": 3600},
        },
    )
    assert too_long.status_code == 422

    too_soon = client.post(
        "/api/v1/statuses",
        headers=headers,
        json={"status": "Too soon", "poll": {"options": ["a", "b"], "expires_in": 60}},
    )
    assert too_soon.status_code == 422

    too_late = client.post(
        "/api/v1/statuses",
        headers=headers,
        json={
            "status": "Too late",
            "poll": {"options": ["a", "b"], "expires_in": 3_000_000},
        },
    )
    assert too_late.status_code == 422


@pytest.mark.asyncio
async def test_poll_vote_accepts_json_body(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    """JSON clients (Ice Cubes and friends) POST `{"choices": [n]}`."""
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    poll_object = RemoteObject(_build_poll_object(ra, ["Cats", "Dogs"]), ra)
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        poll_object, follower.actor
    )
    poll_id = ids.encode_inbox_id(inbox_object)

    token = await _make_access_token(async_db_session, "write:statuses")
    response = client.post(
        f"/api/v1/polls/{poll_id}/votes",
        headers={"Authorization": f"Bearer {token}"},
        json={"choices": [1]},
    )

    assert response.status_code == 200
    assert response.json()["own_votes"] == [1]


@pytest.mark.asyncio
async def test_poll_vote_rejects_a_second_vote(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    """A re-vote would deliver a second set of vote activities to the author."""
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    poll_object = RemoteObject(_build_poll_object(ra, ["Cats", "Dogs"]), ra)
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        poll_object, follower.actor
    )
    poll_id = ids.encode_inbox_id(inbox_object)

    token = await _make_access_token(async_db_session, "write:statuses")
    headers = {"Authorization": f"Bearer {token}"}

    first = client.post(
        f"/api/v1/polls/{poll_id}/votes", headers=headers, json={"choices": [0]}
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/v1/polls/{poll_id}/votes", headers=headers, json={"choices": [1]}
    )
    assert second.status_code == 422
    assert "already voted" in second.json()["error_description"]

    # The recorded answer is still the first one, not the rejected re-vote.
    refetched = client.get(f"/api/v1/polls/{poll_id}", headers=headers).json()
    assert refetched["own_votes"] == [0]


@pytest.mark.asyncio
async def test_poll_vote_on_own_poll_is_a_422(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:statuses")
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post(
        "/api/v1/statuses",
        headers=headers,
        json={
            "status": "Pick one",
            "poll": {"options": ["Cats", "Dogs"], "expires_in": 3600},
        },
    ).json()

    response = client.post(
        f"/api/v1/polls/{created['poll']['id']}/votes",
        headers=headers,
        json={"choices": [0]},
    )

    assert response.status_code == 422
    assert "own poll" in response.json()["error_description"]


@pytest.mark.asyncio
async def test_poll_counts_distinguish_votes_from_voters(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    """On a multiple-choice poll, votes cast exceed the number of voters."""
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    raw_poll = _build_poll_object(ra, ["Cats", "Dogs"], multiple=True)
    raw_poll["votersCount"] = 5
    raw_poll["anyOf"][0]["replies"]["totalItems"] = 4
    raw_poll["anyOf"][1]["replies"]["totalItems"] = 3
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        RemoteObject(raw_poll, ra), follower.actor
    )
    poll_id = ids.encode_inbox_id(inbox_object)

    token = await _make_access_token(async_db_session, "read:statuses")
    poll = client.get(
        f"/api/v1/polls/{poll_id}", headers={"Authorization": f"Bearer {token}"}
    ).json()

    assert poll["multiple"] is True
    assert poll["voters_count"] == 5
    assert poll["votes_count"] == 7
    assert [o["votes_count"] for o in poll["options"]] == [4, 3]
