from datetime import timedelta
from unittest import mock

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import activitypub.models
from activitypub import activitypub as ap
from activitypub.actor import LOCAL_ACTOR
from activitypub.tests import factories
from app import config
from app import templates
from app.utils.datetime import now
from tests.utils import setup_inbox_note
from tests.utils import setup_remote_actor

_ACCEPTED_AP_HEADERS = [
    "application/activity+json",
    "application/activity+json; charset=utf-8",
    "application/ld+json",
    'application/ld+json; profile="https://www.w3.org/ns/activitystreams"',
]


def _create_public_note(index: int) -> None:
    factories.OutboxObjectFactory(
        public_id=f"note-{index}",
        ap_type="Note",
        ap_id=f"http://localhost:8000/o/note-{index}",
        ap_object={"type": "Note", "content": f"hello {index}"},
        visibility=ap.VisibilityEnum.PUBLIC,
        ap_published_at=now() - timedelta(seconds=index),
    )


def test_index__html(db: Session, client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_index__html_has_next_page_when_more_than_a_page_of_notes(
    db: Session, client: TestClient
) -> None:
    # `index` fetches `page_size + 1` rows to derive `has_next_page` instead
    # of a separate `COUNT(*)` -- this locks in the off-by-one boundary.
    for i in range(21):
        _create_public_note(i)

    response = client.get("/")

    assert response.status_code == 200
    assert "?page=2" in response.text


def test_index__html_no_next_page_when_exactly_a_page_of_notes(
    db: Session, client: TestClient
) -> None:
    for i in range(20):
        _create_public_note(i)

    response = client.get("/")

    assert response.status_code == 200
    assert "?page=2" not in response.text


def _use_shipped_templates_only(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Ignore any local `data/templates` override so a test exercises the
    shipped `app/templates/*.html`, not an instance owner's fork of it.

    Jinja's `auto_reload` only re-checks the mtime of the file a template was
    *previously* resolved from — it doesn't re-scan the search path — so a
    template already cached from `data/templates` earlier in the test run
    would otherwise survive the `searchpath` swap below. Clearing the cache
    forces re-resolution against the new search path; the finalizer drops
    that override-free entry again once `searchpath` itself has been
    reverted, so later tests don't inherit it.
    """
    env = templates._templates.env
    assert env.cache is not None
    monkeypatch.setattr(env.loader, "searchpath", ["app/templates"])
    env.cache.clear()
    request.addfinalizer(env.cache.clear)


def test_index__html_shows_featured_tags(
    db: Session,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    monkeypatch.setitem(
        templates._templates.env.globals, "FEATURED_TAGS", ["Microblogging"]
    )
    _use_shipped_templates_only(monkeypatch, request)

    response = client.get("/")

    assert response.status_code == 200
    assert '<a href="http://localhost:8000/t/microblogging"' in response.text
    assert "#Microblogging" in response.text


def test_index__html_no_featured_tags_by_default(
    db: Session,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    _use_shipped_templates_only(monkeypatch, request)

    response = client.get("/")

    assert response.status_code == 200
    assert 'id="featured-tags"' not in response.text


@pytest.mark.parametrize("accept", _ACCEPTED_AP_HEADERS)
def test_index__ap(db: Session, client: TestClient, accept: str):
    response = client.get("/", headers={"Accept": accept})
    assert response.status_code == 200
    assert response.headers["content-type"] == ap.AP_CONTENT_TYPE
    assert response.json() == LOCAL_ACTOR.ap_actor


def test_index__ap_publishes_discovery_hints(db: Session, client: TestClient) -> None:
    # Both are config-driven (`data/profile.toml`), and both must carry a
    # declared JSON-LD term or a strict processor drops them.
    response = client.get("/", headers={"Accept": ap.AP_CONTENT_TYPE})

    assert response.status_code == 200
    actor = response.json()
    assert actor["discoverable"] is True
    assert actor["indexable"] is True
    assert actor["featuredTags"] == "http://localhost:8000/featured_tags"

    terms = actor["@context"][-1]
    assert terms["discoverable"] == "toot:discoverable"
    assert terms["indexable"] == "toot:indexable"
    assert terms["featuredTags"] == {"@id": "toot:featuredTags", "@type": "@id"}
    assert terms["focalPoint"] == {"@container": "@list", "@id": "toot:focalPoint"}


def test_featured_tags__ap_empty_by_default(db: Session, client: TestClient) -> None:
    response = client.get("/featured_tags", headers={"Accept": ap.AP_CONTENT_TYPE})

    assert response.status_code == 200
    assert response.headers["content-type"] == ap.AP_CONTENT_TYPE
    assert response.json() == {
        "@context": ap.AS_EXTENDED_CTX,
        "id": "http://localhost:8000/featured_tags",
        "type": "Collection",
        "totalItems": 0,
        "items": [],
    }


def test_featured_tags__ap(
    db: Session, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Tags are normalized the same way the client API does it: leading "#"
    # stripped, lowercased.
    monkeypatch.setattr(config, "FEATURED_TAGS", ["#Microblogging", "activitypub"])

    response = client.get("/featured_tags", headers={"Accept": ap.AP_CONTENT_TYPE})

    assert response.status_code == 200
    payload = response.json()
    assert payload["totalItems"] == 2
    assert payload["items"] == [
        {
            "type": "Hashtag",
            "href": "http://localhost:8000/t/microblogging",
            "name": "#microblogging",
        },
        {
            "type": "Hashtag",
            "href": "http://localhost:8000/t/activitypub",
            "name": "#activitypub",
        },
    ]


def test_followers__ap(client, db) -> None:
    response = client.get("/followers", headers={"Accept": ap.AP_CONTENT_TYPE})
    assert response.status_code == 200
    assert response.headers["content-type"] == ap.AP_CONTENT_TYPE
    json_resp = response.json()
    assert json_resp["id"].endswith("/followers")
    assert "first" in json_resp


def test_followers__ap_hides_followers(client, db) -> None:
    with mock.patch("app.main.config.HIDES_FOLLOWERS", True):
        response = client.get("/followers", headers={"Accept": ap.AP_CONTENT_TYPE})
    assert response.status_code == 200
    assert response.headers["content-type"] == ap.AP_CONTENT_TYPE
    json_resp = response.json()
    assert json_resp["id"].endswith("/followers")
    assert "first" not in json_resp


def test_followers__html(client, db) -> None:
    response = client.get("/followers")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_followers__html_hides_followers(client, db) -> None:
    with mock.patch("app.main.config.HIDES_FOLLOWERS", True):
        response = client.get("/followers", headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")


def test_following__ap(client, db) -> None:
    response = client.get("/following", headers={"Accept": ap.AP_CONTENT_TYPE})
    assert response.status_code == 200
    assert response.headers["content-type"] == ap.AP_CONTENT_TYPE
    json_resp = response.json()
    assert json_resp["id"].endswith("/following")
    assert "first" in json_resp


def test_following__ap_hides_following(client, db) -> None:
    with mock.patch("app.main.config.HIDES_FOLLOWING", True):
        response = client.get("/following", headers={"Accept": ap.AP_CONTENT_TYPE})
    assert response.status_code == 200
    assert response.headers["content-type"] == ap.AP_CONTENT_TYPE
    json_resp = response.json()
    assert json_resp["id"].endswith("/following")
    assert "first" not in json_resp


def test_following__html(client, db) -> None:
    response = client.get("/following")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_following__html_hides_following(client, db) -> None:
    with mock.patch("app.main.config.HIDES_FOLLOWING", True):
        response = client.get("/following", headers={"Accept": "text/html"})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")


def test_index__no_analytics_html_by_default(client, db) -> None:
    response = client.get("/")
    assert "__ANALYTICS_TEST__" not in response.text


def test_index__analytics_html_shown_on_public_page(client, db) -> None:
    with mock.patch(
        "app.templates.ANALYTICS_HTML", "<script>__ANALYTICS_TEST__=1;</script>"
    ):
        response = client.get("/")
    assert "__ANALYTICS_TEST__" in response.text


def test_admin_login__analytics_html_not_shown(client, db) -> None:
    with mock.patch(
        "app.templates.ANALYTICS_HTML", "<script>__ANALYTICS_TEST__=1;</script>"
    ):
        response = client.get("/admin/login")
    assert "__ANALYTICS_TEST__" not in response.text


def test_public_page_renders_video_attachment(db: Session, client: TestClient) -> None:
    upload = activitypub.models.Upload(
        content_type="video/mp4",
        content_hash="deadbeef" * 8,
        has_thumbnail=True,
        blurhash="U58E0g",
        width=1280,
        height=720,
        duration=12.5,
        has_audio=False,
    )
    db.add(upload)
    db.flush()

    note = factories.OutboxObjectFactory(
        public_id="video-note",
        ap_type="Note",
        ap_id="http://localhost:8000/o/video-note",
        ap_object={"type": "Note", "content": "a clip"},
        visibility=ap.VisibilityEnum.PUBLIC,
        ap_published_at=now(),
    )
    db.add(
        activitypub.models.OutboxObjectAttachment(
            filename="clip.mp4",
            outbox_object_id=note.id,
            upload_id=upload.id,
        )
    )
    db.commit()

    response = client.get(f"/o/{note.public_id}")

    assert response.status_code == 200
    html = response.text
    assert 'width="1280" height="720"' in html
    assert 'data-has-audio="false"' in html
    assert "/attachments/thumbnails/" in html  # poster= + resized image src


def _create_note_with_interactions(
    db: Session,
    respx_mock: respx.MockRouter,
) -> tuple[activitypub.models.OutboxObject, str, str]:
    """A public note with a remote reply, a local self-reply, and some counts."""
    note = factories.OutboxObjectFactory(
        public_id="note-with-interactions",
        ap_type="Note",
        ap_id="http://localhost:8000/o/note-with-interactions",
        ap_object={
            "type": "Note",
            "id": "http://localhost:8000/o/note-with-interactions",
            "content": "hello",
            "published": now().replace(microsecond=0).isoformat(),
        },
        visibility=ap.VisibilityEnum.PUBLIC,
        ap_published_at=now(),
        likes_count=3,
        announces_count=2,
        replies_count=2,
    )

    ra = setup_remote_actor(respx_mock, base_url="https://remote.example")
    remote_actor = factories.ActorFactory.from_remote_actor(ra)
    remote_reply = setup_inbox_note(
        remote_actor,
        content="me too",
        in_reply_to=note.ap_id,
    )

    self_reply = factories.OutboxObjectFactory(
        public_id="self-reply",
        ap_type="Note",
        ap_id="http://localhost:8000/o/self-reply",
        ap_object={
            "type": "Note",
            "id": "http://localhost:8000/o/self-reply",
            "content": "and one more thing",
            "inReplyTo": note.ap_id,
            "published": now().replace(microsecond=0).isoformat(),
        },
        visibility=ap.VisibilityEnum.PUBLIC,
        # Explicit ordering: the collection is sorted by publication date
        ap_published_at=now() + timedelta(seconds=10),
    )
    db.commit()

    return note, remote_reply.ap_id, self_reply.ap_id


def test_object__ap_has_interaction_collections(
    db: Session, client: TestClient, respx_mock: respx.MockRouter
) -> None:
    note, remote_reply_ap_id, self_reply_ap_id = _create_note_with_interactions(
        db, respx_mock
    )

    response = client.get(
        f"/o/{note.public_id}", headers={"Accept": ap.AP_CONTENT_TYPE}
    )

    assert response.status_code == 200
    payload = response.json()
    # The counts are the live ones, and the replies of both boxes are inlined so
    # a remote server can resolve the thread around a post it just discovered.
    assert payload["replies"] == {
        "id": f"{note.ap_id}/replies",
        "type": "Collection",
        "totalItems": 2,
        "items": [remote_reply_ap_id, self_reply_ap_id],
    }
    assert payload["likes"] == {
        "id": f"{note.ap_id}/likes",
        "type": "Collection",
        "totalItems": 3,
    }
    assert payload["shares"] == {
        "id": f"{note.ap_id}/shares",
        "type": "Collection",
        "totalItems": 2,
    }


def test_object_activity__ap_has_interaction_collections(
    db: Session, client: TestClient, respx_mock: respx.MockRouter
) -> None:
    note, _, _ = _create_note_with_interactions(db, respx_mock)

    response = client.get(
        f"/o/{note.public_id}/activity", headers={"Accept": ap.AP_CONTENT_TYPE}
    )

    assert response.status_code == 200
    assert response.json()["object"]["replies"]["totalItems"] == 2


def test_object_replies_collection__ap(
    db: Session, client: TestClient, respx_mock: respx.MockRouter
) -> None:
    note, remote_reply_ap_id, self_reply_ap_id = _create_note_with_interactions(
        db, respx_mock
    )

    response = client.get(
        f"/o/{note.public_id}/replies", headers={"Accept": ap.AP_CONTENT_TYPE}
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == ap.AP_CONTENT_TYPE
    assert response.json() == {
        "@context": ap.AS_EXTENDED_CTX,
        "id": f"{note.ap_id}/replies",
        "type": "Collection",
        "totalItems": 2,
        "items": [remote_reply_ap_id, self_reply_ap_id],
    }


def test_object_likes_and_shares_collections__ap(
    db: Session, client: TestClient, respx_mock: respx.MockRouter
) -> None:
    note, _, _ = _create_note_with_interactions(db, respx_mock)

    likes = client.get(
        f"/o/{note.public_id}/likes", headers={"Accept": ap.AP_CONTENT_TYPE}
    )
    shares = client.get(
        f"/o/{note.public_id}/shares", headers={"Accept": ap.AP_CONTENT_TYPE}
    )

    # Counts only, no items -- same as what Mastodon serves
    assert likes.json() == {
        "@context": ap.AS_EXTENDED_CTX,
        "id": f"{note.ap_id}/likes",
        "type": "Collection",
        "totalItems": 3,
    }
    assert shares.json() == {
        "@context": ap.AS_EXTENDED_CTX,
        "id": f"{note.ap_id}/shares",
        "type": "Collection",
        "totalItems": 2,
    }


def _create_quote_authorization_stamp() -> activitypub.models.OutboxObject:
    return factories.OutboxObjectFactory(
        public_id="stamp-1",
        ap_type="QuoteAuthorization",
        ap_id="http://localhost:8000/o/stamp-1",
        ap_object={
            "type": "QuoteAuthorization",
            "attributedTo": LOCAL_ACTOR.ap_id,
            "interactingObject": "https://example.com/users/alice/notes/1",
            "interactionTarget": "http://localhost:8000/o/some-note",
        },
        visibility=ap.VisibilityEnum.PUBLIC,
        is_hidden_from_homepage=True,
    )


def test_quote_authorization_stamp__ap(db: Session, client: TestClient) -> None:
    stamp = _create_quote_authorization_stamp()

    response = client.get(
        f"/o/{stamp.public_id}", headers={"Accept": ap.AP_CONTENT_TYPE}
    )

    assert response.status_code == 200
    assert response.json()["type"] == "QuoteAuthorization"


def test_quote_authorization_stamp__html_404s(db: Session, client: TestClient) -> None:
    # No HTML rendering exists for a stamp -- see PLAN-quote.md -- so a
    # browser hitting its permalink gets a 404 rather than a blank page.
    stamp = _create_quote_authorization_stamp()

    response = client.get(f"/o/{stamp.public_id}")

    assert response.status_code == 404


def test_quote_authorization_stamp__not_in_public_outbox(
    db: Session, client: TestClient
) -> None:
    _create_quote_authorization_stamp()

    response = client.get("/outbox", headers={"Accept": ap.AP_CONTENT_TYPE})

    assert response.status_code == 200
    ap_ids = {item["object"]["id"] for item in response.json()["orderedItems"]}
    assert "http://localhost:8000/o/stamp-1" not in ap_ids


def test_object_collections__ap_404_for_unknown_object(
    db: Session, client: TestClient, respx_mock: respx.MockRouter
) -> None:
    for collection in ["replies", "likes", "shares"]:
        response = client.get(
            f"/o/nope/{collection}", headers={"Accept": ap.AP_CONTENT_TYPE}
        )
        assert response.status_code == 404


def test_outbox__ap_advertises_interaction_collections(
    db: Session, client: TestClient, respx_mock: respx.MockRouter
) -> None:
    note, _, _ = _create_note_with_interactions(db, respx_mock)

    response = client.get("/outbox", headers={"Accept": ap.AP_CONTENT_TYPE})

    assert response.status_code == 200
    items = {
        item["object"]["id"]: item["object"] for item in response.json()["orderedItems"]
    }
    # Listings advertise the collections without inlining the replies: that
    # would cost a query per item.
    assert items[note.ap_id]["replies"] == {
        "id": f"{note.ap_id}/replies",
        "type": "Collection",
        "totalItems": 2,
    }
    assert items[note.ap_id]["likes"]["totalItems"] == 3
