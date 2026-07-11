from fastapi.testclient import TestClient

# CORS preflight is answered by the middleware itself (it never reaches
# routing), so any path works here. The actual-request test below uses an
# endpoint that already exists so header injection is observed on a real
# response rather than on a 404.
_EXISTING_ENDPOINT = "/.well-known/oauth-authorization-server"


def test_cors_preflight_allows_mastodon_client_origin(client: TestClient) -> None:
    response = client.options(
        "/api/v1/instance",
        headers={
            "Origin": "https://elk.zone",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert "authorization" in response.headers["access-control-allow-headers"].lower()


def test_cors_exposes_link_header_for_pagination(client: TestClient) -> None:
    response = client.get(
        _EXISTING_ENDPOINT,
        headers={"Origin": "https://elk.zone"},
    )

    assert response.status_code == 200
    assert "link" in response.headers.get("access-control-expose-headers", "").lower()
