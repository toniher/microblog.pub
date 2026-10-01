import secrets
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

import activitypub.models
from activitypub import activitypub as ap
from activitypub import boxes
from activitypub.ap_object import ObjectType
from app import config
from app import models
from app.mastodon import ids
from app.mastodon import serializers
from app.utils.datetime import now


async def _headers(db_session: AsyncSession, scope: str) -> dict[str, str]:
    token = models.IndieAuthAccessToken(
        access_token=secrets.token_urlsafe(16),
        refresh_token=None,
        expires_in=3600,
        scope=scope,
    )
    db_session.add(token)
    await db_session.commit()
    return {"Authorization": f"Bearer {token.access_token}"}


async def _own_note(
    db_session: AsyncSession,
    visibility: ap.VisibilityEnum = ap.VisibilityEnum.PUBLIC,
) -> activitypub.models.OutboxObject:
    _, note = await boxes.send_create(
        db_session,
        ObjectType.NOTE.value,
        "Original",
        uploads=[],
        in_reply_to=None,
        visibility=visibility,
    )
    return note


async def _remote_quote_of(
    db_session: AsyncSession, quoted: activitypub.models.OutboxObject
) -> activitypub.models.InboxObject:
    """A remote note quoting `quoted`, carrying a stamp we minted."""
    actor = activitypub.models.Actor(
        ap_id="https://example.com/users/alice",
        ap_actor={
            "id": "https://example.com/users/alice",
            "type": "Person",
            "inbox": "https://example.com/users/alice/inbox",
            "preferredUsername": "alice",
        },
        ap_type="Person",
    )
    db_session.add(actor)
    await db_session.flush()

    quoting_ap_id = "https://example.com/users/alice/notes/2"
    stamp = await boxes._mint_quote_authorization(
        db_session,
        quoting_object_ap_id=quoting_ap_id,
        quoted_object_ap_id=quoted.ap_id,
    )
    quoting = activitypub.models.InboxObject(
        server="example.com",
        actor_id=actor.id,
        ap_actor_id=actor.ap_id,
        ap_type="Note",
        ap_id=quoting_ap_id,
        ap_context=None,
        ap_published_at=now(),
        ap_object={
            "id": quoting_ap_id,
            "type": "Note",
            "attributedTo": actor.ap_id,
            "content": "RE: ...",
            "to": [ap.AS_PUBLIC],
            "quote": quoted.ap_id,
            "quoteAuthorization": stamp.ap_id,
        },
        visibility=ap.VisibilityEnum.PUBLIC,
        is_hidden_from_stream=False,
        quote_ap_id=quoted.ap_id,
        quote_authorization_ap_id=stamp.ap_id,
        quote_is_verified=True,
    )
    db_session.add(quoting)
    await db_session.flush()
    quoted.quotes_count = await boxes._get_quotes_count(db_session, quoted.ap_id)
    await db_session.commit()
    # Reload so `ap_published_at` has made the same SQLite round trip the API
    # reads it through; the ids encode it.
    await db_session.refresh(quoting)
    return quoting


@pytest.mark.asyncio
async def test_own_status_reports_quote_policy_and_count(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    headers = await _headers(async_db_session, "read")
    note = await _own_note(async_db_session)

    data = client.get(
        f"/api/v1/statuses/{ids.encode_outbox_id(note)}", headers=headers
    ).json()

    assert data["quotes_count"] == 0
    assert data["quote_approval"] == {
        "automatic": [config.CONFIG.quote_policy],
        "manual": [],
        "current_user": "automatic",
    }


@pytest.mark.asyncio
async def test_quotes_list_and_revoke(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    headers = await _headers(async_db_session, "read write")
    quoted = await _own_note(async_db_session)
    quoting = await _remote_quote_of(async_db_session, quoted)
    quoted_id = ids.encode_outbox_id(quoted)
    quoting_id = ids.encode_inbox_id(quoting)

    listed = client.get(f"/api/v1/statuses/{quoted_id}/quotes", headers=headers)
    assert [s["id"] for s in listed.json()] == [quoting_id]
    status = client.get(f"/api/v1/statuses/{quoted_id}", headers=headers).json()
    assert status["quotes_count"] == 1

    revoked = client.post(
        f"/api/v1/statuses/{quoted_id}/quotes/{quoting_id}/revoke", headers=headers
    )
    assert revoked.status_code == 200
    assert revoked.json()["quote"]["state"] == "unauthorized"
    assert (
        client.get(f"/api/v1/statuses/{quoted_id}/quotes", headers=headers).json() == []
    )

    # Already revoked: nothing left to revoke.
    again = client.post(
        f"/api/v1/statuses/{quoted_id}/quotes/{quoting_id}/revoke", headers=headers
    )
    assert again.status_code == 422


@pytest.mark.asyncio
async def test_revoke_rejects_a_status_that_does_not_quote_it(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    headers = await _headers(async_db_session, "write")
    quoted = await _own_note(async_db_session)
    other = await _own_note(async_db_session)
    quoting = await _remote_quote_of(async_db_session, other)

    response = client.post(
        f"/api/v1/statuses/{ids.encode_outbox_id(quoted)}"
        f"/quotes/{ids.encode_inbox_id(quoting)}/revoke",
        headers=headers,
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_quoted_status_id_is_the_official_alias_of_quote_id(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    headers = await _headers(async_db_session, "write:statuses")
    quoted = await _own_note(async_db_session)
    quoted_id = ids.encode_outbox_id(quoted)

    response = client.post(
        "/api/v1/statuses",
        headers=headers,
        data={"status": "Look", "quoted_status_id": quoted_id},
    )

    assert response.status_code == 200
    assert response.json()["quote"]["quoted_status"]["id"] == quoted_id


@pytest.mark.asyncio
async def test_quote_approval_policy_must_match_the_instance_setting(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    headers = await _headers(async_db_session, "write:statuses")
    default = client.get(
        "/api/v1/preferences",
        headers=await _headers(async_db_session, "read"),
    ).json()["posting:default:quote_policy"]
    other = "nobody" if default != "nobody" else "public"

    def post(policy: str, visibility: str = "public") -> int:
        return client.post(
            "/api/v1/statuses",
            headers=headers,
            data={
                "status": f"policy {policy} {visibility}",
                "quote_approval_policy": policy,
                "visibility": visibility,
            },
        ).status_code

    assert post(default) == 200
    assert post(other) == 422
    # Ignored for private posts, as in Mastodon.
    assert post(other, "private") == 200


def test_remote_quote_approval_reads_the_published_policy() -> None:
    def approval(policy: dict | None) -> dict:
        ap_object = {"interactionPolicy": policy} if policy else {}
        obj = SimpleNamespace(ap_object=ap_object, ap_actor_id="https://r.example/a")
        return serializers._serialize_quote_approval(cast(Any, obj))

    assert approval(None) == {
        "automatic": ["unsupported_policy"],
        "manual": [],
        "current_user": "unknown",
    }
    assert approval({"canQuote": {"automaticApproval": [ap.AS_PUBLIC]}}) == {
        "automatic": ["public"],
        "manual": [],
        "current_user": "automatic",
    }
    assert approval(
        {
            "canQuote": {
                "automaticApproval": ["https://r.example/a"],
                "manualApproval": [ap.AS_PUBLIC],
            }
        }
    ) == {"automatic": [], "manual": ["public"], "current_user": "manual"}
    # Author-only: nobody else, including us.
    assert approval({"canQuote": {"automaticApproval": ["https://r.example/a"]}}) == {
        "automatic": [],
        "manual": [],
        "current_user": "denied",
    }
    assert (
        approval(
            {"canQuote": {"automaticApproval": ["https://r.example/a/followers"]}}
        )["current_user"]
        == "unknown"
    )
