import base64
import secrets

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import activitypub.models
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


@pytest.mark.asyncio
async def test_statuses_show_public(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    _, outbox_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Hello, Mastodon",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    status_id = ids.encode_outbox_id(outbox_object)

    response = client.get(f"/api/v1/statuses/{status_id}")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == status_id
    assert data["content"] == "<p>Hello, Mastodon</p>\n"
    assert data["account"]["id"] == ids.LOCAL_ACTOR_ID
    assert data["visibility"] == "public"


def test_statuses_show_not_found(client: TestClient) -> None:
    response = client.get("/api/v1/statuses/999999")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_statuses_show_private_requires_auth(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    _, outbox_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Only for followers",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.FOLLOWERS_ONLY,
    )
    status_id = ids.encode_outbox_id(outbox_object)

    unauthorized = client.get(f"/api/v1/statuses/{status_id}")
    assert unauthorized.status_code == 404

    token = await _make_access_token(async_db_session, "read:statuses")
    authorized = client.get(
        f"/api/v1/statuses/{status_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert authorized.status_code == 200
    assert authorized.json()["visibility"] == "private"


@pytest.mark.asyncio
async def test_statuses_context(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    root_id, root_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Root post",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    _, reply_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "A reply",
        uploads=[],
        in_reply_to=root_object.ap_id,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    root_status_id = ids.encode_outbox_id(root_object)
    reply_status_id = ids.encode_outbox_id(reply_object)

    root_context = client.get(f"/api/v1/statuses/{root_status_id}/context").json()
    assert root_context["ancestors"] == []
    assert [s["id"] for s in root_context["descendants"]] == [reply_status_id]

    reply_context = client.get(f"/api/v1/statuses/{reply_status_id}/context").json()
    assert [s["id"] for s in reply_context["ancestors"]] == [root_status_id]
    assert reply_context["descendants"] == []


@pytest.mark.asyncio
async def test_statuses_favourited_by(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    _, outbox_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Like me",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    status_id = ids.encode_outbox_id(outbox_object)

    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    like_activity = RemoteObject(
        {
            "@context": ap.AS_CTX,
            "type": "Like",
            "id": ra.ap_id + "/like/1",
            "actor": ra.ap_id,
            "object": outbox_object.ap_id,
        },
        ra,
    )
    factories.InboxObjectFactory.from_remote_object(like_activity, follower.actor)

    response = client.get(f"/api/v1/statuses/{status_id}/favourited_by")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["id"] == ids.encode_account_id(follower.actor)


@pytest.mark.asyncio
async def test_statuses_reblogged_by_and_reblog_nesting(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    remote_note = RemoteObject(
        factories.build_note_object(from_remote_actor=ra, content="From afar"),
        ra,
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        remote_note, follower.actor
    )

    await boxes.send_announce(async_db_session, inbox_object.ap_id)

    announce_object = (
        await async_db_session.scalars(
            select(activitypub.models.OutboxObject)
            .where(activitypub.models.OutboxObject.ap_type == "Announce")
            .order_by(activitypub.models.OutboxObject.id.desc())
        )
    ).first()
    assert announce_object is not None
    announce_status_id = ids.encode_outbox_id(announce_object)

    reblogged_by = client.get(f"/api/v1/statuses/{announce_status_id}/reblogged_by")
    assert reblogged_by.status_code == 200
    assert reblogged_by.json() == []  # nobody has reblogged OUR announce (yet)

    status = client.get(f"/api/v1/statuses/{announce_status_id}").json()
    assert status["reblog"] is not None
    # Remote content is stored as published (no local markdown rendering).
    assert status["reblog"]["content"] == "From afar"
    assert status["reblog"]["account"]["id"] == ids.encode_account_id(follower.actor)


def _decode_proxied_media_url(proxied_url: str) -> str:
    # BASE_URL + "/proxy/media/{expires}/{sig}/" + base64(original_url)
    encoded = proxied_url.rstrip("/").rsplit("/", 1)[-1]
    padded = encoded + "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(padded).decode()


async def _send_note_with_og_meta(
    async_db_session: AsyncSession, og_meta: list[dict] | None
) -> str:
    """Create a status, then attach scraped OG metadata to it.

    `og_meta` is normally filled in by `app.utils.opengraph` at create time,
    which would hit the network — set it directly instead, exactly as that
    scraper would have stored it.
    """
    _, outbox_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Check this out",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    outbox_object.og_meta = og_meta
    await async_db_session.commit()
    return ids.encode_outbox_id(outbox_object)


@pytest.mark.asyncio
async def test_status_card_from_og_meta(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    status_id = await _send_note_with_og_meta(
        async_db_session,
        [
            {
                "url": "https://example.com/article",
                "title": "An article",
                "image": "https://example.com/thumb.jpg",
                "description": "What it is about",
                "site_name": "example.com",
            }
        ],
    )

    response = client.get(f"/api/v1/statuses/{status_id}")

    assert response.status_code == 200
    card = response.json()["card"]
    assert card is not None
    assert card["url"] == "https://example.com/article"
    assert card["title"] == "An article"
    assert card["description"] == "What it is about"
    assert card["provider_name"] == "example.com"
    assert card["type"] == "link"
    # The thumbnail goes through the media proxy like every other remote media
    # URL — a client rendering the card must not hit the linked host directly.
    assert card["image"].startswith(f"{config.BASE_URL}/proxy/media/")
    assert _decode_proxied_media_url(card["image"]) == "https://example.com/thumb.jpg"


@pytest.mark.asyncio
async def test_status_card_first_usable_entry_wins(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    # A post can link to several pages; Mastodon's `card` is singular. Entries
    # the scraper couldn't get a title/url for are skipped rather than
    # serialized as a blank card.
    status_id = await _send_note_with_og_meta(
        async_db_session,
        [
            {"url": "https://example.com/untitled", "title": "", "site_name": ""},
            {"url": "https://example.com/second", "title": "The second one"},
        ],
    )

    card = client.get(f"/api/v1/statuses/{status_id}").json()["card"]

    assert card is not None
    assert card["url"] == "https://example.com/second"
    assert card["title"] == "The second one"
    # Absent in the stored metadata -> empty/None, never missing from the entity.
    assert card["description"] == ""
    assert card["provider_name"] == ""
    assert card["image"] is None
    assert card["blurhash"] is None


@pytest.mark.asyncio
async def test_status_card_is_null_without_og_meta(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    # A post with no external links, and one whose scrape found nothing usable.
    cases: list[list[dict] | None] = [None, []]
    for og_meta in cases:
        status_id = await _send_note_with_og_meta(async_db_session, og_meta)
        response = client.get(f"/api/v1/statuses/{status_id}")
        assert response.status_code == 200
        assert response.json()["card"] is None
