"""max_id / since_id / min_id + Link-header pagination for Mastodon-style
numeric-id list endpoints.

Distinct from `app/utils/pagination.py`, which implements the opaque
published_at cursor used by the existing HTML admin UI.
"""

from dataclasses import dataclass
from typing import Sequence

from fastapi import Request

DEFAULT_LIMIT = 20
MAX_LIMIT = 40


@dataclass(frozen=True)
class PaginationParams:
    max_id: str | None
    since_id: str | None
    min_id: str | None
    limit: int


def parse_pagination(
    request: Request,
    default_limit: int = DEFAULT_LIMIT,
    max_limit: int = MAX_LIMIT,
) -> PaginationParams:
    q = request.query_params
    limit = default_limit
    if raw_limit := q.get("limit"):
        try:
            limit = max(1, min(int(raw_limit), max_limit))
        except ValueError:
            pass

    return PaginationParams(
        max_id=q.get("max_id"),
        since_id=q.get("since_id"),
        min_id=q.get("min_id"),
        limit=limit,
    )


def build_link_header(request: Request, item_ids: Sequence[str]) -> str | None:
    """Build a Mastodon-style `Link` header from the ids of the current page.

    `item_ids` must be in the same (descending) order returned to the client;
    other query params (e.g. `limit`) on the request are preserved.
    """
    if not item_ids:
        return None

    base_url = request.url.remove_query_params(["max_id", "since_id", "min_id"])
    next_url = base_url.include_query_params(max_id=item_ids[-1])
    prev_url = base_url.include_query_params(min_id=item_ids[0])

    return f'<{next_url}>; rel="next", <{prev_url}>; rel="prev"'
