"""Mastodon OAuth scope vocabulary layered onto the existing space-delimited
`scope` string (see `app/indieauth.py`'s `AccessTokenInfo`).

Scopes are stored verbatim in the same column Micropub uses for its own
`create`/`update`/`delete` scopes — no schema change. `require_scope()` runs
parallel to Micropub's inline scope checks; neither affects the other.
"""

from typing import Callable
from typing import Coroutine

from fastapi import Depends
from fastapi import Request
from fastapi.exceptions import HTTPException

from app.database import AsyncSession
from app.database import get_db_session
from app.indieauth import AccessTokenInfo
from app.indieauth import enforce_access_token
from app.mastodon.errors import MastodonError

# Legacy Mastodon scope that predates the read:follows/write:follows/
# write:blocks/write:mutes split; a token granted just `follow` still
# satisfies any of these granular scopes.
FOLLOW_COVERED_PREFIXES = (
    "read:follows",
    "write:follows",
    "write:blocks",
    "write:mutes",
)


def has_scope(token_info: AccessTokenInfo, required: str) -> bool:
    if required in token_info.scopes:
        return True

    top_level = required.split(":", 1)[0]
    if top_level in token_info.scopes:
        return True

    if required.startswith(FOLLOW_COVERED_PREFIXES) and "follow" in token_info.scopes:
        return True

    return False


def require_scope(
    required: str,
) -> Callable[..., Coroutine[None, None, AccessTokenInfo]]:
    async def _dependency(
        request: Request,
        db_session: AsyncSession = Depends(get_db_session),
    ) -> AccessTokenInfo:
        try:
            token_info = await enforce_access_token(request, db_session)
        except HTTPException as exc:
            raise MastodonError(
                status_code=exc.status_code,
                error="unauthorized",
                error_description=str(exc.detail),
            ) from exc

        if not has_scope(token_info, required):
            raise MastodonError(
                status_code=403,
                error="insufficient_scope",
                error_description=f"This action requires the {required} scope",
                headers={"WWW-Authenticate": f'Bearer scope="{required}"'},
            )

        return token_info

    return _dependency
