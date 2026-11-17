import base64
import time

import pytest
from fastapi.testclient import TestClient

from app import config
from app import media


def _encode(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode()).decode()


def test_proxy_media_expired_signature_returns_404_not_500(client: TestClient) -> None:
    # A signature computed for a long-past `expires` day-index (e.g. a proxy
    # link embedded in a status/notification months ago and only fetched by
    # a client now) must fail cleanly, not crash the endpoint.
    url = "https://example.com/pic.png"
    expired_exp = 0
    sig = media.proxied_media_sig(expired_exp, url)

    response = client.get(f"/proxy/media/{expired_exp}/{sig}/{_encode(url)}")

    assert response.status_code == 404


def test_proxy_media_tampered_signature_returns_404_not_500(
    client: TestClient,
) -> None:
    url = "https://example.com/pic.png"
    exp = int(time.time() / media.EXPIRY_PERIOD) + media.EXPIRY_LENGTH

    response = client.get(f"/proxy/media/{exp}/not-the-real-signature/{_encode(url)}")

    assert response.status_code == 404


def test_proxy_media_resized_expired_signature_returns_404_not_500(
    client: TestClient,
) -> None:
    url = "https://example.com/pic.png"
    expired_exp = 0
    sig = media.proxied_media_sig(expired_exp, url)

    response = client.get(f"/proxy/media/{expired_exp}/{sig}/{_encode(url)}/50")

    assert response.status_code == 404


def test_resize_image_rejects_oversized_remote_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proxied image's pixel count is chosen by the remote instance. Pillow
    alone would happily decode ~179 MP (a ~0.5 GB bitmap) before objecting.

    The ValueError is the endpoint's existing "serve the original untouched"
    signal, so an oversized image is passed through rather than decoded.
    """
    import io

    from PIL import Image

    from app.main import _resize_image

    monkeypatch.setattr("app.main.config.MAX_IMAGE_PIXELS", 100)

    buf = io.BytesIO()
    Image.new("RGB", (40, 40)).save(buf, format="PNG")  # 1600 px, over the cap

    with pytest.raises(ValueError, match="megapixels"):
        _resize_image(buf.getvalue(), 50, True)


def test_resize_image_still_resizes_within_the_cap() -> None:
    import io

    from PIL import Image

    from app.main import _resize_image

    buf = io.BytesIO()
    Image.new("RGB", (400, 400), color=(10, 20, 30)).save(buf, format="PNG")

    content, mimetype = _resize_image(buf.getvalue(), 50, True)

    assert mimetype == "image/webp"
    with Image.open(io.BytesIO(content)) as out:
        assert max(out.size) <= 50


def _at_day(monkeypatch: pytest.MonkeyPatch, day: int) -> None:
    # Midday, so float rounding can't push it across a day boundary.
    monkeypatch.setattr(
        "app.media.time.time", lambda: (day + 0.5) * media.EXPIRY_PERIOD
    )


def _expires_of(proxied_url: str) -> int:
    return int(proxied_url.split("/proxy/media/")[1].split("/")[0])


def test_proxied_media_url_is_stable_within_a_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://example.com/pic.png"
    first_day = 7 * 3000  # first day of a bucket
    urls = set()
    for day in range(first_day, first_day + media.EXPIRY_LENGTH):
        _at_day(monkeypatch, day)
        urls.add(media.proxied_media_url(url))
    assert len(urls) == 1

    _at_day(monkeypatch, first_day + media.EXPIRY_LENGTH)
    assert media.proxied_media_url(url) not in urls


def test_proxied_media_url_keeps_at_least_expiry_length_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = "https://example.com/pic.png"
    first_day = 7 * 3000
    last_day = first_day + media.EXPIRY_LENGTH - 1

    _at_day(monkeypatch, first_day)
    assert _expires_of(media.proxied_media_url(url)) - first_day == 14

    _at_day(monkeypatch, last_day)
    proxied = media.proxied_media_url(url)
    expires = _expires_of(proxied)
    assert expires - last_day == 8
    sig = proxied.split("/proxy/media/")[1].split("/")[1]

    _at_day(monkeypatch, expires)
    media.verify_proxied_media_sig(expires, url, sig)  # last valid day

    _at_day(monkeypatch, expires + 1)
    with pytest.raises(media.InvalidProxySignatureError):
        media.verify_proxied_media_sig(expires, url, sig)


def test_previously_issued_daily_url_still_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Links minted before bucketing (`today + EXPIRY_LENGTH`) keep working.
    url = "https://example.com/pic.png"
    day = 7 * 3000 + 3
    _at_day(monkeypatch, day)
    old_expires = day + media.EXPIRY_LENGTH

    media.verify_proxied_media_sig(
        old_expires, url, media.proxied_media_sig(old_expires, url)
    )


def test_inline_imgs_cache_follows_the_expiry_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.templates import _update_inline_imgs

    content = '<p><img src="https://example.com/inline.png"></p>'
    first_day = 7 * 3000

    _at_day(monkeypatch, first_day)
    before = _update_inline_imgs(content)
    _at_day(monkeypatch, first_day + 1)
    assert _update_inline_imgs(content) is before  # same bucket: cache hit

    _at_day(monkeypatch, first_day + media.EXPIRY_LENGTH)
    after = _update_inline_imgs(content)
    assert after != before
    assert str(media.current_expiry()) in after


@pytest.mark.parametrize("accept", ["image/webp,*/*", "*/*"])
def test_proxy_media_resized_varies_on_accept(
    client: TestClient, respx_mock, accept: str
) -> None:
    # The resized body is webp or not depending on Accept, under a 1-year
    # max-age: shared caches (nginx proxy_cache) must key on Accept. Checked
    # on a fresh resize and on the in-process cache hit that follows.
    import io

    import httpx
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (100, 100)).save(buf, format="PNG")
    url = f"https://example.com/vary-{accept.count('webp')}.png"
    respx_mock.get(url).mock(
        return_value=httpx.Response(
            200, content=buf.getvalue(), headers={"content-type": "image/png"}
        )
    )
    path = media.resized_media_url(url, 50).removeprefix(config.BASE_URL)

    for _ in range(2):
        response = client.get(path, headers={"Accept": accept})
        assert response.status_code == 200
        assert "Accept" in response.headers["vary"].split(", ")
