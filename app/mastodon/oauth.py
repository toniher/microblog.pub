"""Mastodon-shaped OAuth adapter.

Adapts the existing IndieAuth OAuth2 server (`app/indieauth.py`) in place
rather than forking a parallel stack: `/oauth/authorize` aliases the
existing `GET /auth` consent flow, and `/oauth/token` shares the
`issue_access_token` grant core with the legacy `/token` endpoint. See
PLAN-0.md ("OAuth deltas") for the rationale.
"""

import secrets
from datetime import timezone

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from sqlalchemy import select
from starlette.responses import JSONResponse

from app import models
from app.admin import user_session_or_redirect
from app.database import AsyncSession
from app.database import get_db_session
from app.indieauth import TokenGrantError
from app.indieauth import enforce_access_token
from app.indieauth import indieauth_authorization_endpoint
from app.indieauth import issue_access_token
from app.mastodon.entities import Application
from app.mastodon.errors import MastodonError

router = APIRouter()

# Real Mastodon access tokens don't expire and clients never call a refresh
# grant against them; ~100 years emulates "non-expiring" without a schema
# change (IndieAuthAccessToken.expires_in is NOT NULL).
_NON_EXPIRING_TOKEN_SECONDS = 100 * 365 * 24 * 3600


@router.post("/api/v1/apps", response_model=None)
async def apps_create(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    form_data = await request.form()

    client_name = str(form_data.get("client_name", "")).strip()
    if not client_name:
        raise MastodonError(422, "validation_failed", "client_name is required")

    raw_redirect_uris = form_data.get("redirect_uris")
    if not raw_redirect_uris:
        raise MastodonError(422, "validation_failed", "redirect_uris is required")
    redirect_uris_str = str(raw_redirect_uris)
    redirect_uris = redirect_uris_str.split()

    website = form_data.get("website")
    scopes = str(form_data.get("scopes", "read"))

    client = models.OAuthClient(
        client_name=client_name,
        redirect_uris=redirect_uris,
        client_uri=str(website) if website else None,
        scope=scopes,
        client_id=secrets.token_hex(16),
        client_secret=secrets.token_hex(32),
    )
    db_session.add(client)
    await db_session.commit()

    application = Application(
        id=str(client.id),
        name=client.client_name,
        website=client.client_uri,
        redirect_uri=redirect_uris_str,
        redirect_uris=redirect_uris,
        client_id=client.client_id,
        client_secret=client.client_secret,
    )
    return JSONResponse(content=application.model_dump(mode="json"), status_code=200)


@router.get("/api/v1/apps/verify_credentials", response_model=None)
async def apps_verify_credentials(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    token_info = await enforce_access_token(request, db_session)
    if not token_info.client_id:
        raise MastodonError(401, "unauthorized", "token has no associated application")

    client = (
        await db_session.scalars(
            select(models.OAuthClient).where(
                models.OAuthClient.client_id == token_info.client_id
            )
        )
    ).one_or_none()
    if not client:
        raise MastodonError(401, "unauthorized", "unknown application")

    return JSONResponse(
        content={
            "name": client.client_name,
            "website": client.client_uri,
            "vapid_key": "",
        },
        status_code=200,
    )


@router.get("/oauth/authorize", response_model=None)
async def oauth_authorize(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
    _: None = Depends(user_session_or_redirect),
):
    """Mastodon-shaped alias for the existing `GET /auth` consent flow."""
    return await indieauth_authorization_endpoint(request, db_session)


@router.post("/oauth/token", response_model=None)
async def oauth_token(
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    form_data = await request.form()
    grant_type = str(form_data.get("grant_type", "authorization_code"))

    def _opt(key: str) -> str | None:
        value = form_data.get(key)
        return str(value) if value else None

    try:
        access_token = await issue_access_token(
            db_session,
            grant_type=grant_type,
            client_id=_opt("client_id"),
            code=_opt("code"),
            redirect_uri=_opt("redirect_uri"),
            code_verifier=_opt("code_verifier"),
            refresh_token_param=_opt("refresh_token"),
            expires_in=_NON_EXPIRING_TOKEN_SECONDS,
            issue_refresh_token=False,
        )
    except TokenGrantError as exc:
        raise MastodonError(exc.status_code, exc.error) from exc

    return JSONResponse(
        content={
            "access_token": access_token.access_token,
            "token_type": "Bearer",
            "scope": access_token.scope,
            "created_at": int(
                access_token.created_at.replace(tzinfo=timezone.utc).timestamp()
            ),
        },
        status_code=200,
    )
