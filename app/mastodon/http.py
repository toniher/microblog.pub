"""Request-body plumbing shared by the Mastodon API routers.

Clients disagree on which encoding they use for POST bodies, whatever the
endpoint, so every write handler has to sniff before it parses.
"""

from fastapi import Request


def is_json_request(request: Request) -> bool:
    """True when the body should be read with `await request.json()` rather
    than `await request.form()`."""
    content_type, _, _ = request.headers.get("Content-Type", "").partition(";")
    return content_type.strip().lower() == "application/json"
