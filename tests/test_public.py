from unittest import mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from activitypub import activitypub as ap
from activitypub.actor import LOCAL_ACTOR
from app import templates

_ACCEPTED_AP_HEADERS = [
    "application/activity+json",
    "application/activity+json; charset=utf-8",
    "application/ld+json",
    'application/ld+json; profile="https://www.w3.org/ns/activitystreams"',
]


def test_index__html(db: Session, client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


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
