import pytest
from fastapi import APIRouter
from fastapi import Depends
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app import models
from app.indieauth import AccessTokenInfo
from app.mastodon.errors import MastodonError
from app.mastodon.errors import mastodon_error_handler
from app.mastodon.scopes import has_scope
from app.mastodon.scopes import require_scope


def _token_info(*scopes: str) -> AccessTokenInfo:
    return AccessTokenInfo(
        scopes=list(scopes), client_id="testclient", access_token="tok", exp=0
    )


@pytest.mark.parametrize(
    "granted,required,expected",
    [
        (("write:statuses",), "write:statuses", True),
        (("write",), "write:statuses", True),
        (("read",), "write:statuses", False),
        (("follow",), "write:follows", True),
        (("follow",), "write:blocks", True),
        (("follow",), "write:mutes", True),
        (("follow",), "write:statuses", False),
        ((), "read", False),
        (("read",), "read", True),
    ],
)
def test_has_scope(granted: tuple, required: str, expected: bool) -> None:
    assert has_scope(_token_info(*granted), required) is expected


def _build_test_app() -> FastAPI:
    router = APIRouter()

    @router.get("/needs-write-statuses")
    async def _protected(
        token_info: AccessTokenInfo = Depends(require_scope("write:statuses")),
    ) -> dict:
        return {"scopes": token_info.scopes}

    test_app = FastAPI()
    test_app.include_router(router)
    test_app.add_exception_handler(MastodonError, mastodon_error_handler)
    return test_app


def test_require_scope_rejects_missing_token(async_db_session: AsyncSession) -> None:
    with TestClient(_build_test_app()) as client:
        response = client.get("/needs-write-statuses")

    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


@pytest.mark.asyncio
async def test_require_scope_rejects_insufficient_scope(
    async_db_session: AsyncSession,
) -> None:
    token = models.IndieAuthAccessToken(
        access_token="tok-read-only",
        refresh_token="refresh-read-only",
        expires_in=3600,
        scope="read",
    )
    async_db_session.add(token)
    await async_db_session.commit()

    with TestClient(_build_test_app()) as client:
        response = client.get(
            "/needs-write-statuses",
            headers={"Authorization": "Bearer tok-read-only"},
        )

    assert response.status_code == 403
    assert response.json()["error"] == "insufficient_scope"


@pytest.mark.asyncio
async def test_require_scope_accepts_matching_scope(
    async_db_session: AsyncSession,
) -> None:
    token = models.IndieAuthAccessToken(
        access_token="tok-write",
        refresh_token="refresh-write",
        expires_in=3600,
        scope="write",
    )
    async_db_session.add(token)
    await async_db_session.commit()

    with TestClient(_build_test_app()) as client:
        response = client.get(
            "/needs-write-statuses",
            headers={"Authorization": "Bearer tok-write"},
        )

    assert response.status_code == 200
    assert response.json() == {"scopes": ["write"]}
