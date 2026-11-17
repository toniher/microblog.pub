from unittest import mock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from activitypub import activitypub as ap
from activitypub.tests import factories
from app import config
from app import models


def test_webmention__resolves_target_by_alias(db: Session, client: TestClient) -> None:
    public_id = "note-with-alias"
    note = factories.OutboxObjectFactory(
        public_id=public_id,
        ap_type="Note",
        ap_id=f"http://localhost:8000/o/{public_id}",
        ap_object={"type": "Note", "content": "hello"},
        visibility=ap.VisibilityEnum.PUBLIC,
        alias="my-post",
    )
    target = f"http://localhost:8000/{config.ALIAS_URL_PREFIX}/{note.alias}"
    source = "https://example.com/reply"

    with mock.patch(
        "app.webmentions.microformats.fetch_and_parse",
        return_value=({}, f'<a href="{target}">mentioned</a>'),
    ):
        response = client.post(
            "/webmentions",
            data={"source": source, "target": target},
        )

    assert response.status_code == 200

    webmention = db.query(models.Webmention).one()
    assert webmention.outbox_object_id == note.id
    assert webmention.source == source
    assert webmention.target == target


def test_microformats_img_alt_is_flattened_to_url() -> None:
    import mf2py  # type: ignore

    from app.utils.microformats import _drop_img_alt

    parsed = _drop_img_alt(
        mf2py.parse(
            doc='<div class="h-card"><span class="p-name">A</span>'
            '<img class="u-photo" src="https://a.ex/a.jpg" alt="A"></div>'
        )
    )

    assert parsed["items"][0]["properties"]["photo"] == ["https://a.ex/a.jpg"]


@pytest.mark.asyncio
async def test_microformats_fetch_and_parse_caps_body(respx_mock) -> None:
    from app.utils import microformats

    url = "https://example.com/huge"
    respx_mock.get(url).mock(
        return_value=httpx.Response(
            200,
            content=b"<p>" + b"a" * (microformats._MAX_HTML_BYTES + 1024),
            headers={"content-type": "text/html; charset=utf-8"},
        )
    )

    _, html = await microformats.fetch_and_parse(url)

    assert len(html) == microformats._MAX_HTML_BYTES


@pytest.mark.asyncio
async def test_discover_webmention_endpoint_from_html(respx_mock) -> None:
    from app.utils import webmentions

    url = "https://example.com/post"
    respx_mock.get(url).mock(
        return_value=httpx.Response(
            200,
            text='<html><head><link rel="webmention" href="/wm"></head></html>',
            headers={"content-type": "text/html"},
        )
    )

    assert (
        await webmentions._discover_webmention_endoint(url) == "https://example.com/wm"
    )


@pytest.mark.asyncio
async def test_og_meta_from_url(respx_mock) -> None:
    from app.utils import opengraph

    url = "https://example.com/article"
    respx_mock.get(url).mock(
        return_value=httpx.Response(
            200,
            text=(
                "<html><head><title>T</title>"
                '<meta property="og:description" content="D"></head></html>'
            ),
            headers={"content-type": "text/html"},
        )
    )

    og_meta = await opengraph._og_meta_from_url(url)

    assert og_meta is not None
    assert (og_meta.title, og_meta.description) == ("T", "D")
