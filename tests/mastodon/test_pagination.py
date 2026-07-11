from urllib.parse import urlsplit

from starlette.requests import Request

from app.mastodon import pagination


def _make_request(url: str) -> Request:
    parts = urlsplit(url)
    scope = {
        "type": "http",
        "method": "GET",
        "scheme": parts.scheme or "http",
        "server": (parts.hostname or "testserver", parts.port or 80),
        "path": parts.path,
        "query_string": parts.query.encode(),
        "headers": [],
    }
    return Request(scope)


def test_parse_pagination_defaults() -> None:
    params = pagination.parse_pagination(
        _make_request("https://example.com/api/v1/timelines/home")
    )
    assert params == pagination.PaginationParams(
        max_id=None,
        since_id=None,
        min_id=None,
        limit=pagination.DEFAULT_LIMIT,
    )


def test_parse_pagination_reads_cursor_params() -> None:
    params = pagination.parse_pagination(
        _make_request(
            "https://example.com/api/v1/timelines/home?max_id=42&since_id=1&min_id=2&limit=5"
        )
    )
    assert params.max_id == "42"
    assert params.since_id == "1"
    assert params.min_id == "2"
    assert params.limit == 5


def test_parse_pagination_clamps_limit_to_max() -> None:
    params = pagination.parse_pagination(
        _make_request("https://example.com/api/v1/timelines/home?limit=1000")
    )
    assert params.limit == pagination.MAX_LIMIT


def test_parse_pagination_ignores_invalid_limit() -> None:
    params = pagination.parse_pagination(
        _make_request("https://example.com/api/v1/timelines/home?limit=not-a-number")
    )
    assert params.limit == pagination.DEFAULT_LIMIT


def test_build_link_header_empty_page_returns_none() -> None:
    request = _make_request("https://example.com/api/v1/timelines/home")
    assert pagination.build_link_header(request, []) is None


def test_build_link_header_uses_first_and_last_ids() -> None:
    request = _make_request("https://example.com/api/v1/timelines/home?limit=2")
    link = pagination.build_link_header(request, ["10", "8"])

    assert link is not None
    assert 'rel="next"' in link
    assert 'rel="prev"' in link
    assert "max_id=8" in link
    assert "min_id=10" in link
    # Existing query params (limit) must be preserved on both links.
    assert link.count("limit=2") == 2


def test_build_link_header_strips_existing_cursor_params() -> None:
    request = _make_request(
        "https://example.com/api/v1/timelines/home?max_id=999&since_id=1"
    )
    link = pagination.build_link_header(request, ["10", "8"])

    assert link is not None
    assert "max_id=999" not in link
    assert "since_id=1" not in link
