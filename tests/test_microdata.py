from unittest import mock

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import templates
from tests.utils import setup_outbox_note


def test_index__html_microdata_disabled_by_default(
    db: Session, client: TestClient
) -> None:
    setup_outbox_note()
    response = client.get("/")
    assert response.status_code == 200
    assert "itemscope" not in response.text
    assert "schema.org" not in response.text


def test_index__html_microdata_enabled(db: Session, client: TestClient) -> None:
    setup_outbox_note()
    with mock.patch.dict(templates._templates.env.globals, {"ENABLE_MICRODATA": True}):
        response = client.get("/")
    assert response.status_code == 200
    assert 'itemtype="https://schema.org/Blog"' in response.text
    assert 'itemtype="https://schema.org/SocialMediaPosting"' in response.text
    # Blog.blogPost only accepts BlogPosting, so a Note (mapped to
    # SocialMediaPosting) is linked via the generic CreativeWork.hasPart instead.
    assert 'itemprop="hasPart"' in response.text
    assert 'itemprop="blogPost"' not in response.text
    assert 'itemprop="datePublished"' in response.text
    assert 'itemprop="author"' in response.text


def test_object_page__html_microdata_enabled(db: Session, client: TestClient) -> None:
    outbox_object = setup_outbox_note()
    with mock.patch.dict(templates._templates.env.globals, {"ENABLE_MICRODATA": True}):
        response = client.get(f"/o/{outbox_object.public_id}")
    assert response.status_code == 200
    assert 'itemtype="https://schema.org/SocialMediaPosting"' in response.text
    assert 'itemprop="articleBody"' in response.text
