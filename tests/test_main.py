"""Regression coverage for `app.main._check_0rtt_early_data`.

No test existed for this app-level dependency before the streaming feature
required rewriting it (`Request` -> `HTTPConnection`, so it can also run for
WebSocket routes). It is an app-level dependency, so a mistake here affects
every route on the instance — worth a few cheap, direct tests.
"""

from fastapi.testclient import TestClient


def test_post_with_early_data_header_is_425(client: TestClient) -> None:
    response = client.post(
        "/api/v1/apps",
        headers={"Early-Data": "1"},
        json={"client_name": "test", "redirect_uris": "https://example.com/"},
    )
    assert response.status_code == 425


def test_post_without_early_data_header_is_not_425(client: TestClient) -> None:
    response = client.post(
        "/api/v1/apps",
        json={"client_name": "test", "redirect_uris": "https://example.com/"},
    )
    assert response.status_code != 425


def test_get_with_early_data_header_is_not_425(client: TestClient) -> None:
    response = client.get("/api/v1/instance", headers={"Early-Data": "1"})
    assert response.status_code != 425
