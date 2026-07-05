from typing import Iterator

import starlette
from fastapi.testclient import TestClient

from activitypub import activitypub as ap
from app.config import generate_csrf_token
from app.main import app
from tests.utils import generate_admin_session_cookies


def _iter_endpoint_routes(
    routes: list, prefix: str = ""
) -> Iterator[tuple[str, set[str]]]:
    """Yield (full_path, methods) for every concrete endpoint route.

    FastAPI >=0.139 no longer flattens ``include_router`` calls into
    ``app.routes``; instead it inserts lazy ``_IncludedRouter`` wrappers that
    reference the original router and its prefix. Recurse through those so
    included (e.g. /admin) routes are still discoverable.
    """
    for route in routes:
        if isinstance(route, starlette.routing.Route):
            yield prefix + route.path, route.methods or set()
            continue

        include_context = getattr(route, "include_context", None)
        original_router = getattr(route, "original_router", None)
        if include_context is not None and original_router is not None:
            sub_prefix = prefix + getattr(include_context, "prefix", "")
            yield from _iter_endpoint_routes(original_router.routes, sub_prefix)


def test_admin_endpoints_are_authenticated(client: TestClient) -> None:
    routes_tested = []

    for path, methods in _iter_endpoint_routes(app.routes):
        if not path.startswith("/admin") or path == "/admin/login":
            continue

        for method in methods:
            resp = client.request(method, path, follow_redirects=False)

            # Admin routes should redirect to the login page
            assert resp.status_code == 302, f"{method} {path} is unauthenticated"
            assert resp.headers.get("Location", "").startswith(
                "http://testserver/admin/login"
            )
            routes_tested.append((method, path))

    assert len(routes_tested) > 0


def test_public_works_authenticated(client: TestClient) -> None:
    response = client.post(
        "/admin/actions/new",
        data={
            "content": "hello",
            "redirect_url": "http://testserver/",
            "visibility": ap.VisibilityEnum.PUBLIC.name,
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )
    assert response.status_code == 302
    resp = client.get("/", cookies=generate_admin_session_cookies())
    assert resp.status_code == 200
