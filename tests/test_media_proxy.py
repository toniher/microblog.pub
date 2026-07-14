import base64
import time

from fastapi.testclient import TestClient

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
