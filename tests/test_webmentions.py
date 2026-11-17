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


@pytest.mark.asyncio
async def test_send_create_queues_webmentions_after_commit(
    async_db_session, respx_mock
) -> None:
    # Endpoint discovery fetches every linked page; it must run after the
    # note is committed, not with its writes pending (guarded by the autouse
    # `no_network_while_writing`).
    from sqlalchemy import select

    import activitypub.models
    from activitypub.ap_object import ObjectType
    from activitypub.boxes import send_create

    url = "https://news.example/story"
    page_route = respx_mock.route(url=url).mock(
        return_value=httpx.Response(
            200,
            text=(
                "<html><head><title>Story</title>"
                '<link rel="webmention" href="/wm"></head></html>'
            ),
            headers={"content-type": "text/html"},
        )
    )

    _, outbox_object = await send_create(
        async_db_session,
        ObjectType.NOTE.value,
        f"Read [this]({url})",
        uploads=[],
        in_reply_to=None,
        visibility=ap.VisibilityEnum.PUBLIC,
    )

    outgoing = (
        await async_db_session.scalars(
            select(activitypub.models.OutgoingActivity).where(
                activitypub.models.OutgoingActivity.webmention_target == url
            )
        )
    ).all()
    assert [o.recipient for o in outgoing] == ["https://news.example/wm"]
    # One fetch for both the link card and the webmention endpoint
    assert page_route.call_count == 1
    assert [m["title"] for m in outbox_object.og_meta or []] == ["Story"]


@pytest.mark.asyncio
async def test_fetch_link_info(respx_mock) -> None:
    from app.utils import opengraph

    page = (
        "<html><head><title>{}</title>"
        '<link rel="webmention" href="/wm"></head></html>'
    )
    routes = [
        respx_mock.get(f"https://{host}.example/p").mock(
            return_value=httpx.Response(
                200, text=page.format(host), headers={"content-type": "text/html"}
            )
        )
        for host in ("a", "b")
    ]
    urls = ["https://a.example/p", "https://b.example/p"]

    links = await opengraph.fetch_link_info(urls, discover_webmentions=True)

    assert {u: (i.og_meta or {}).get("title") for u, i in links.items()} == {
        "https://a.example/p": "a",
        "https://b.example/p": "b",
    }
    assert links["https://a.example/p"].webmention_endpoint == "https://a.example/wm"
    assert [r.call_count for r in routes] == [1, 1]

    links = await opengraph.fetch_link_info(urls, discover_webmentions=False)
    assert links["https://a.example/p"].webmention_endpoint is None
