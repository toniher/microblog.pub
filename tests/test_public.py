from datetime import timedelta
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import activitypub.models
from activitypub import activitypub as ap
from activitypub.actor import LOCAL_ACTOR
from activitypub.tests import factories
from app import config
from app import templates
from app.utils.datetime import now

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
