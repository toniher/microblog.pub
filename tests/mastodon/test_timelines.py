import secrets

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from activitypub import activitypub as ap
from activitypub import boxes
from activitypub.ap_object import ObjectType
from activitypub.ap_object import RemoteObject
from activitypub.tests import factories
from app import models
from app.mastodon import ids
from tests.utils import setup_remote_actor
from tests.utils import setup_remote_actor_as_follower
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


def test_timelines_home_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/timelines/home")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_timelines_home_merges_own_posts_and_followed_notes(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    _, own_post = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "My own post",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    remote_note = RemoteObject(
        factories.build_note_object(from_remote_actor=ra, content="Followed note"),
        ra,
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        remote_note, follower.actor
    )

    token = await _make_access_token(async_db_session, "read:statuses")
    response = client.get(
        "/api/v1/timelines/home", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    assert {status["id"] for status in response.json()} == {
        ids.encode_outbox_id(own_post),
        ids.encode_inbox_id(inbox_object),
    }


@pytest.mark.asyncio
async def test_timelines_home_excludes_replies(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    _, root_object = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Root",
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

    token = await _make_access_token(async_db_session, "read:statuses")
    response = client.get(
        "/api/v1/timelines/home", headers={"Authorization": f"Bearer {token}"}
    )

    returned_ids = {status["id"] for status in response.json()}
    assert ids.encode_outbox_id(root_object) in returned_ids
    assert ids.encode_outbox_id(reply_object) not in returned_ids


@pytest.mark.asyncio
async def test_timelines_public_local_only(
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
    _, unlisted_post = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Unlisted post",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.UNLISTED,
    )

    response = client.get("/api/v1/timelines/public?local=true")

    assert response.status_code == 200
    returned_ids = [status["id"] for status in response.json()]
    assert ids.encode_outbox_id(public_post) in returned_ids
    assert ids.encode_outbox_id(unlisted_post) not in returned_ids


@pytest.mark.asyncio
async def test_timelines_public_federated_includes_remote_public_notes(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    remote_note = RemoteObject(
        factories.build_note_object(
            from_remote_actor=ra,
            content="Public federated note",
            to=[ap.AS_PUBLIC],
        ),
        ra,
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        remote_note, follower.actor
    )
    assert inbox_object.visibility == ap.VisibilityEnum.PUBLIC

    response = client.get("/api/v1/timelines/public")

    assert response.status_code == 200
    returned_ids = {status["id"] for status in response.json()}
    assert ids.encode_inbox_id(inbox_object) in returned_ids


@pytest.mark.asyncio
async def test_timelines_tag_filters_by_hashtag(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    _, tagged_post = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Post about #microblogging",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    _, untagged_post = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Just a regular post",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    response = client.get("/api/v1/timelines/tag/microblogging")

    assert response.status_code == 200
    returned_ids = {status["id"] for status in response.json()}
    assert ids.encode_outbox_id(tagged_post) in returned_ids
    assert ids.encode_outbox_id(untagged_post) not in returned_ids


@pytest.mark.asyncio
async def test_timelines_home_pagination_max_id(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    _, first_post = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "First",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )
    _, second_post = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Second",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    token = await _make_access_token(async_db_session, "read:statuses")
    headers = {"Authorization": f"Bearer {token}"}

    full_page = client.get("/api/v1/timelines/home", headers=headers).json()
    assert [s["id"] for s in full_page] == [
        ids.encode_outbox_id(second_post),
        ids.encode_outbox_id(first_post),
    ]

    older_page = client.get(
        f"/api/v1/timelines/home?max_id={ids.encode_outbox_id(second_post)}",
        headers=headers,
    ).json()
    assert [s["id"] for s in older_page] == [ids.encode_outbox_id(first_post)]


@pytest.mark.asyncio
async def test_timelines_home_ids_sort_descending_across_inbox_and_outbox(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    # Regression test for the id-monotonicity bug: InboxObject/OutboxObject
    # have independent PK sequences, so interleaved inserts used to produce
    # ids like [5, 6, 4, 2] once merged by publish time — not descending, even
    # though the array itself was correctly ordered by publish time. Ids are
    # now timestamp-prefixed so id order always matches array order.
    _, first_post = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "First own post",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    remote_note = RemoteObject(
        factories.build_note_object(from_remote_actor=ra, content="Followed note"),
        ra,
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        remote_note, follower.actor
    )

    _, second_post = await boxes.send_create(
        async_db_session,
        ObjectType.NOTE.value,
        "Second own post",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    token = await _make_access_token(async_db_session, "read:statuses")
    response = client.get(
        "/api/v1/timelines/home", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    data = response.json()
    returned_ids = {status["id"] for status in data}
    assert returned_ids == {
        ids.encode_outbox_id(first_post),
        ids.encode_inbox_id(inbox_object),
        ids.encode_outbox_id(second_post),
    }
    numeric_ids = [int(status["id"]) for status in data]
    assert numeric_ids == sorted(numeric_ids, reverse=True)


@pytest.mark.asyncio
async def test_timelines_home_coerces_null_sensitive_to_bool(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    # Some AP servers send an explicit `"sensitive": null`. If serialized as
    # `null`, strict Mastodon clients (Tusky/Fedilab) fail to deserialize the
    # non-null boolean and silently drop the entire timeline page.
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    note_data = factories.build_note_object(
        from_remote_actor=ra,
        content="Explicit null sensitive",
        to=[ap.AS_PUBLIC],
    )
    note_data["sensitive"] = None
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        RemoteObject(note_data, ra), follower.actor
    )

    token = await _make_access_token(async_db_session, "read:statuses")
    response = client.get(
        "/api/v1/timelines/home",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    status = next(
        s for s in response.json() if s["id"] == ids.encode_inbox_id(inbox_object)
    )
    assert status["sensitive"] is False


@pytest.mark.asyncio
async def test_timelines_home_serializes_reblog_target_url_list_with_strings(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    remote_note_data = factories.build_note_object(
        from_remote_actor=ra,
        content="Boosted note",
    )
    remote_note_data["url"] = [
        remote_note_data["url"],
        {
            "type": "Link",
            "href": remote_note_data["url"] + "/html",
            "mediaType": "text/html",
        },
    ]
    remote_note = RemoteObject(remote_note_data, ra)
    factories.InboxObjectFactory.from_remote_object(remote_note, follower.actor)

    reblog = RemoteObject(
        {
            "@context": ap.AS_CTX,
            "type": "Announce",
            "id": f"{ra.ap_id}/announce/with-string-url-list",
            "actor": ra.ap_id,
            "object": remote_note.ap_id,
            "to": [ap.AS_PUBLIC],
            "cc": [],
            "published": remote_note_data["published"],
            "url": f"{ra.ap_id}/announce/with-string-url-list",
        },
        ra,
    )
    factories.InboxObjectFactory.from_remote_object(reblog, follower.actor)

    token = await _make_access_token(async_db_session, "read:statuses")
    response = client.get(
        "/api/v1/timelines/home",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert any(
        status["reblog"] is not None
        and status["reblog"]["url"] == remote_note_data["url"][0]
        for status in response.json()
    )


@pytest.mark.asyncio
async def test_timelines_home_serializes_reblog_target_dict_in_reply_to(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    root_note = RemoteObject(
        factories.build_note_object(
            from_remote_actor=ra,
            content="Root remote note",
        ),
        ra,
    )
    root_inbox_object = factories.InboxObjectFactory.from_remote_object(
        root_note, follower.actor
    )

    reply_note_data = factories.build_note_object(
        from_remote_actor=ra,
        content="Reply remote note",
    )
    reply_note_data["inReplyTo"] = {"id": root_note.ap_id}
    reply_note = RemoteObject(reply_note_data, ra)
    factories.InboxObjectFactory.from_remote_object(reply_note, follower.actor)

    reblog = RemoteObject(
        {
            "@context": ap.AS_CTX,
            "type": "Announce",
            "id": f"{ra.ap_id}/announce/with-dict-in-reply-to",
            "actor": ra.ap_id,
            "object": reply_note.ap_id,
            "to": [ap.AS_PUBLIC],
            "cc": [],
            "published": reply_note_data["published"],
            "url": f"{ra.ap_id}/announce/with-dict-in-reply-to",
        },
        ra,
    )
    factories.InboxObjectFactory.from_remote_object(reblog, follower.actor)

    token = await _make_access_token(async_db_session, "read:statuses")
    response = client.get(
        "/api/v1/timelines/home",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert any(
        status["reblog"] is not None
        and status["reblog"]["in_reply_to_id"] == ids.encode_inbox_id(root_inbox_object)
        for status in response.json()
    )


@pytest.mark.asyncio
async def test_timelines_home_hides_muted_actor(
    client: TestClient,
    db: Session,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    muted_ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    muted_follower = setup_remote_actor_as_follower(muted_ra)
    assert muted_follower.actor is not None
    muted_note = factories.InboxObjectFactory.from_remote_object(
        RemoteObject(
            factories.build_note_object(from_remote_actor=muted_ra, content="Noisy"),
            muted_ra,
        ),
        muted_follower.actor,
    )

    other_ra = setup_remote_actor(respx_mock, base_url="https://example.org")
    other_follower = setup_remote_actor_as_follower(other_ra)
    assert other_follower.actor is not None
    other_note = factories.InboxObjectFactory.from_remote_object(
        RemoteObject(
            factories.build_note_object(from_remote_actor=other_ra, content="Quiet"),
            other_ra,
        ),
        other_follower.actor,
    )

    muted_follower.actor.is_muted = True
    db.commit()

    token = await _make_access_token(async_db_session, "read:statuses")
    response = client.get(
        "/api/v1/timelines/home", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    returned_ids = {status["id"] for status in response.json()}
    assert ids.encode_inbox_id(muted_note) not in returned_ids
    assert ids.encode_inbox_id(other_note) in returned_ids


@pytest.mark.asyncio
async def test_timelines_home_hides_boost_of_muted_actor(
    client: TestClient,
    db: Session,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    muted_ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    muted_follower = setup_remote_actor_as_follower(muted_ra)
    assert muted_follower.actor is not None
    muted_note = RemoteObject(
        factories.build_note_object(from_remote_actor=muted_ra, content="Noisy"),
        muted_ra,
    )
    muted_inbox_object = factories.InboxObjectFactory.from_remote_object(
        muted_note, muted_follower.actor
    )

    booster_ra = setup_remote_actor(respx_mock, base_url="https://example.org")
    booster = setup_remote_actor_as_follower(booster_ra)
    assert booster.actor is not None
    boost = RemoteObject(
        {
            "@context": ap.AS_CTX,
            "type": "Announce",
            "id": f"{booster_ra.ap_id}/announce/muted",
            "actor": booster_ra.ap_id,
            "object": muted_note.ap_id,
            "to": [ap.AS_PUBLIC],
            "cc": [],
            "published": muted_note.ap_object["published"],
            "url": f"{booster_ra.ap_id}/announce/muted",
        },
        booster_ra,
    )
    boost_object = factories.InboxObjectFactory.from_remote_object(
        boost, booster.actor, relates_to_inbox_object_id=muted_inbox_object.id
    )

    muted_follower.actor.is_muted = True
    db.commit()

    token = await _make_access_token(async_db_session, "read:statuses")
    response = client.get(
        "/api/v1/timelines/home", headers={"Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200
    returned_ids = {status["id"] for status in response.json()}
    assert ids.encode_inbox_id(boost_object) not in returned_ids


@pytest.mark.asyncio
async def test_timelines_home_hides_and_unhides_boost_of_reblogs_hidden_actor(
    client: TestClient,
    db: Session,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    booster_ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    following = setup_remote_actor_as_following(booster_ra)
    assert following.actor is not None
    booster_actor = following.actor

    original_note = RemoteObject(
        factories.build_note_object(from_remote_actor=booster_ra, content="Original"),
        booster_ra,
    )
    original_inbox_object = factories.InboxObjectFactory.from_remote_object(
        original_note, booster_actor
    )
    boost = RemoteObject(
        {
            "@context": ap.AS_CTX,
            "type": "Announce",
            "id": f"{booster_ra.ap_id}/announce/reblogs_hidden",
            "actor": booster_ra.ap_id,
            "object": original_note.ap_id,
            "to": [ap.AS_PUBLIC],
            "cc": [],
            "published": original_note.ap_object["published"],
            "url": f"{booster_ra.ap_id}/announce/reblogs_hidden",
        },
        booster_ra,
    )
    boost_object = factories.InboxObjectFactory.from_remote_object(
        boost, booster_actor, relates_to_inbox_object_id=original_inbox_object.id
    )

    token = await _make_access_token(async_db_session, "read:statuses")

    booster_actor.are_announces_hidden_from_stream = True
    db.commit()

    response = client.get(
        "/api/v1/timelines/home", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    returned_ids = {status["id"] for status in response.json()}
    assert ids.encode_inbox_id(boost_object) not in returned_ids

    # Retroactive: toggling the flag off surfaces the already-ingested boost.
    booster_actor.are_announces_hidden_from_stream = False
    db.commit()

    response = client.get(
        "/api/v1/timelines/home", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    returned_ids = {status["id"] for status in response.json()}
    assert ids.encode_inbox_id(boost_object) in returned_ids


@pytest.mark.asyncio
async def test_timelines_tag_any_all_none(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    async def _post(source: str):
        _, obj = await boxes.send_create(
            async_db_session,
            ObjectType.NOTE.value,
            source,
            uploads=[],
            in_reply_to=None,
            visibility=ap.VisibilityEnum.PUBLIC,
        )
        return ids.encode_outbox_id(obj)

    alpha = await _post("Only #alpha here")
    alpha_beta = await _post("Both #alpha and #beta")
    gamma = await _post("Just #gamma")

    def _ids(url: str) -> set[str]:
        response = client.get(url)
        assert response.status_code == 200
        return {status["id"] for status in response.json()}

    # any[] widens the match, on top of the path hashtag.
    assert _ids("/api/v1/timelines/tag/alpha?any[]=gamma") == {
        alpha,
        alpha_beta,
        gamma,
    }

    # all[] narrows it: every listed tag must be present.
    assert _ids("/api/v1/timelines/tag/alpha?all[]=beta") == {alpha_beta}

    # none[] excludes.
    assert _ids("/api/v1/timelines/tag/alpha?none[]=beta") == {alpha}

    # Clients that omit the trailing `[]`, and a leading `#`, work too.
    assert _ids("/api/v1/timelines/tag/alpha?any=%23gamma") == {
        alpha,
        alpha_beta,
        gamma,
    }
