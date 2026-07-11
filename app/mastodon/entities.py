"""Mastodon API response entities (pydantic v2 models).

Grown incrementally across build phases — only entities needed by endpoints
that already exist are defined here. Instances are serialized via
`model_dump(mode="json")` rather than FastAPI's `response_model=` (see
`app/mastodon/oauth.py` for why: it keeps the app's existing
`response_model=None` convention and avoids a second validation pass).
"""

import pydantic


class Application(pydantic.BaseModel):
    id: str
    name: str
    website: str | None = None
    redirect_uri: str
    redirect_uris: list[str]
    client_id: str | None = None
    client_secret: str | None = None
    vapid_key: str = ""
