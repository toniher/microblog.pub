import base64
import hashlib
from urllib.parse import parse_qs
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.config import generate_csrf_token
from app.config import session_serializer

_LOGGED_IN_COOKIE = {"session": session_serializer.dumps({"is_logged_in": True})}


def _extract_code(refresh_header: str) -> str:
    url = refresh_header.split("url=", 1)[1]
    return parse_qs(urlsplit(url).query)["code"][0]


def _s256_challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )


async def _register_client(
    db_session: AsyncSession,
    client_id: str = "mastodon-client",
    client_secret: str = "mastodon-secret",
    redirect_uri: str = "https://client.example/callback",
) -> models.OAuthClient:
    client = models.OAuthClient(
        client_name="Test Mastodon Client",
        redirect_uris=[redirect_uri],
        client_id=client_id,
        client_secret=client_secret,
        scope="read write follow",
    )
    db_session.add(client)
    await db_session.commit()
    return client


def _authorize(
    client: TestClient,
    *,
    client_id: str,
    redirect_uri: str,
    scope: str = "read write",
    code_challenge: str = "",
    code_challenge_method: str = "",
):
    return client.post(
        "/admin/indieauth",
        headers={"charset": "utf-8"},
        cookies=_LOGGED_IN_COOKIE,
        data={
            "client_id": client_id,
            "scopes": scope,
            "redirect_uri": redirect_uri,
            "csrf_token": generate_csrf_token(),
            "state": "xyz",
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
        },
    )


def test_apps_create_returns_application_shape(client: TestClient) -> None:
    response = client.post(
        "/api/v1/apps",
        data={
            "client_name": "Elk",
            "redirect_uris": "https://elk.zone/oauth/callback",
            "scopes": "read write follow push",
            "website": "https://elk.zone",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Elk"
    assert data["website"] == "https://elk.zone"
    assert data["redirect_uri"] == "https://elk.zone/oauth/callback"
    assert "client_id" in data
    assert "client_secret" in data
    assert data["vapid_key"] == ""


def test_apps_create_requires_client_name(client: TestClient) -> None:
    response = client.post(
        "/api/v1/apps",
        data={"redirect_uris": "https://elk.zone/oauth/callback"},
    )
    assert response.status_code == 422
    assert response.json()["error"] == "validation_failed"


@pytest.mark.asyncio
async def test_oauth_authorize_alias_renders_consent_page(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    # A registered client_id resolves from the DB (app/indieauth.py's
    # fallback path otherwise tries to fetch client_id as a profile URL,
    # which "testclientid" isn't).
    await _register_client(
        async_db_session, client_id="testclientid", redirect_uri="testuri"
    )

    response = client.get(
        "/oauth/authorize"
        "?client_id=testclientid&redirect_uri=testuri"
        "&response_type=code&scope=read+write",
        cookies=_LOGGED_IN_COOKIE,
    )
    assert response.status_code == 200
    assert "testclientid" in response.text
    assert "testuri" in response.text


@pytest.mark.asyncio
async def test_oauth_token_pkce_s256_valid(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    await _register_client(async_db_session)
    verifier = "unit-test-code-verifier-1234567890"

    authorize_response = _authorize(
        client,
        client_id="mastodon-client",
        redirect_uri="https://client.example/callback",
        code_challenge=_s256_challenge(verifier),
        code_challenge_method="S256",
    )
    assert authorize_response.status_code == 200
    code = _extract_code(authorize_response.headers["refresh"])

    token_response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://client.example/callback",
            "client_id": "mastodon-client",
            "code_verifier": verifier,
        },
    )

    assert token_response.status_code == 200
    data = token_response.json()
    assert "access_token" in data
    assert data["token_type"] == "Bearer"
    assert "created_at" in data
    # Mastodon tokens don't expire and clients never refresh them.
    assert "refresh_token" not in data
    assert "expires_in" not in data


@pytest.mark.asyncio
async def test_oauth_token_pkce_wrong_verifier_rejected(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    await _register_client(async_db_session)
    verifier = "unit-test-code-verifier-1234567890"

    authorize_response = _authorize(
        client,
        client_id="mastodon-client",
        redirect_uri="https://client.example/callback",
        code_challenge=_s256_challenge(verifier),
        code_challenge_method="S256",
    )
    code = _extract_code(authorize_response.headers["refresh"])

    token_response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://client.example/callback",
            "client_id": "mastodon-client",
            "code_verifier": "wrong-verifier",
        },
    )

    assert token_response.status_code == 400
    assert token_response.json()["error"] == "invalid_grant"


@pytest.mark.asyncio
async def test_oauth_token_without_pkce_still_works(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    # Backward compat: clients that never send code_challenge/code_verifier
    # (plain IndieAuth flow) must be unaffected by the PKCE fix.
    await _register_client(async_db_session)

    authorize_response = _authorize(
        client,
        client_id="mastodon-client",
        redirect_uri="https://client.example/callback",
    )
    code = _extract_code(authorize_response.headers["refresh"])

    token_response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://client.example/callback",
            "client_id": "mastodon-client",
        },
    )

    assert token_response.status_code == 200
    assert "access_token" in token_response.json()


@pytest.mark.asyncio
async def test_redirect_uri_mismatch_is_rejected_for_registered_client(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    await _register_client(async_db_session)

    response = _authorize(
        client,
        client_id="mastodon-client",
        redirect_uri="https://attacker.example/steal-the-code",
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_oauth_token_client_credentials_grant_is_rejected(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    # `POST /api/v1/apps` is unauthenticated by design (any client
    # self-registers and gets its own client_secret back), so a
    # client_credentials grant checked only against that secret would let
    # anyone mint a token acting as the (single) blog owner. Every token
    # must instead be traced back to the admin's own login/consent.
    await _register_client(async_db_session)

    response = client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "mastodon-client",
            "client_secret": "mastodon-secret",
            "scope": "read write follow",
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_grant_type"


@pytest.mark.asyncio
async def test_apps_verify_credentials_with_authorized_token(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    await _register_client(async_db_session)
    verifier = "unit-test-code-verifier-1234567890"

    authorize_response = _authorize(
        client,
        client_id="mastodon-client",
        redirect_uri="https://client.example/callback",
        code_challenge=_s256_challenge(verifier),
        code_challenge_method="S256",
    )
    code = _extract_code(authorize_response.headers["refresh"])

    token_response = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://client.example/callback",
            "client_id": "mastodon-client",
            "code_verifier": verifier,
        },
    )
    access_token = token_response.json()["access_token"]

    response = client.get(
        "/api/v1/apps/verify_credentials",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Test Mastodon Client"
    assert data["vapid_key"] == ""
