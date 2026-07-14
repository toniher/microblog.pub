"""Strict Mastodon-entity conformance checks for the serializers.

Strict Android clients (Tusky, Fedilab) deserialize the whole timeline page
into typed models via Moshi/Gson. A single status whose field is ``null`` where
the model expects a non-null value — or the wrong JSON type — makes the client
drop the *entire* page with no visible error. Federated data is wildly varied
(Misskey/Pleroma/Akkoma/PeerTube), so this test feeds deliberately hostile AP
objects through the real timeline endpoint and asserts every serialized
``Status``/``Account`` still satisfies the strict entity contract.
"""

import re
import secrets

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from activitypub import activitypub as ap
from activitypub.ap_object import RemoteObject
from activitypub.tests import factories
from app import models
from tests.utils import setup_remote_actor
from tests.utils import setup_remote_actor_as_follower

# Mastodon `created_at` is RFC3339 with exactly millisecond precision + `Z`.
_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

_VALID_VISIBILITY = {"public", "unlisted", "private", "direct"}

# field -> (allowed python type, nullable?)
_ACCOUNT_SPEC = {
    "id": (str, False),
    "username": (str, False),
    "acct": (str, False),
    "display_name": (str, False),
    "locked": (bool, False),
    "bot": (bool, False),
    "created_at": (str, False),
    "note": (str, False),
    "url": (str, False),
    "avatar": (str, False),
    "avatar_static": (str, False),
    "header": (str, False),
    "header_static": (str, False),
    "followers_count": (int, False),
    "following_count": (int, False),
    "statuses_count": (int, False),
    "emojis": (list, False),
    "fields": (list, False),
}

_STATUS_SPEC = {
    "id": (str, False),
    "uri": (str, False),
    "url": (str, True),
    "created_at": (str, False),
    "edited_at": (str, True),
    "content": (str, False),
    "visibility": (str, False),
    "sensitive": (bool, False),
    "spoiler_text": (str, False),
    "media_attachments": (list, False),
    "mentions": (list, False),
    "tags": (list, False),
    "emojis": (list, False),
    "reblogs_count": (int, False),
    "favourites_count": (int, False),
    "replies_count": (int, False),
    "favourited": (bool, False),
    "reblogged": (bool, False),
    "muted": (bool, False),
    "bookmarked": (bool, False),
    "filtered": (list, False),
    "in_reply_to_id": (str, True),
    "in_reply_to_account_id": (str, True),
    "language": (str, True),
}


def _check_entity(entity: dict, spec: dict, label: str) -> list[str]:
    errors = []
    for field, (expected_type, nullable) in spec.items():
        if field not in entity:
            errors.append(f"{label}.{field} is missing")
            continue
        value = entity[field]
        if value is None:
            if not nullable:
                errors.append(f"{label}.{field} is null but must be non-null")
            continue
        # bool is a subclass of int, so an int field must reject bool.
        if expected_type is int and isinstance(value, bool):
            errors.append(f"{label}.{field} is bool, expected int")
        elif not isinstance(value, expected_type):
            errors.append(
                f"{label}.{field}={value!r} is {type(value).__name__}, "
                f"expected {expected_type.__name__}"
            )
    return errors


# Real Mastodon always resolves these to an absolute placeholder URL (e.g.
# `.../avatars/original/missing.png`) when an actor has no icon/header set —
# never null, never `""`. Strict clients that type these as a non-optional
# `URL` (not `String`) fail their `URL(string:)` init on an empty string and
# drop the whole containing collection, exactly like a null-where-non-null
# violation.
_ACCOUNT_URL_FIELDS = ("avatar", "avatar_static", "header", "header_static")


def _validate_account(account: dict, label: str) -> list[str]:
    errors = _check_entity(account, _ACCOUNT_SPEC, label)
    if not _DATETIME_RE.match(account.get("created_at", "")):
        errors.append(
            f"{label}.created_at={account.get('created_at')!r} is not "
            "millisecond-precision RFC3339"
        )
    for field in _ACCOUNT_URL_FIELDS:
        value = account.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{label}.{field}={value!r} must be a non-empty URL")
    return errors


def _validate_status(status: dict, label: str) -> list[str]:
    errors = _check_entity(status, _STATUS_SPEC, label)
    if not _DATETIME_RE.match(status.get("created_at", "")):
        errors.append(
            f"{label}.created_at={status.get('created_at')!r} is not "
            "millisecond-precision RFC3339"
        )
    if status.get("visibility") not in _VALID_VISIBILITY:
        errors.append(f"{label}.visibility={status.get('visibility')!r} invalid")
    account = status.get("account")
    if not isinstance(account, dict):
        errors.append(f"{label}.account is not an object")
    else:
        errors += _validate_account(account, f"{label}.account")
    if status.get("reblog") is not None:
        errors += _validate_status(status["reblog"], f"{label}.reblog")
    return errors


def test_validator_rejects_nonconforming_status() -> None:
    # Guards the validator itself: a null non-nullable field, a bad datetime,
    # and an invalid visibility must all be reported.
    bad = {
        "id": "1",
        "uri": "x",
        "url": None,
        "created_at": "2024-01-01T00:00:00Z",  # no millisecond precision
        "content": "x",
        "visibility": "weird",
        "sensitive": None,  # non-nullable
        "spoiler_text": "",
        "account": {"id": "1"},
    }
    errors = _validate_status(bad, "s")
    joined = "\n".join(errors)
    assert "s.sensitive is null but must be non-null" in errors
    assert "millisecond-precision RFC3339" in joined
    assert "s.visibility='weird' invalid" in errors
    assert "s.account.username is missing" in errors


async def _make_access_token(db_session: AsyncSession, scope: str) -> str:
    token = models.IndieAuthAccessToken(
        access_token=secrets.token_urlsafe(16),
        refresh_token=None,
        expires_in=3600,
        scope=scope,
    )
    db_session.add(token)
    await db_session.commit()
    return token.access_token


@pytest.mark.asyncio
async def test_timeline_survives_hostile_federated_data(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    # Inject account-level shapes that violate the Mastodon string contract.
    ra.ap_actor["name"] = None  # display_name source is null
    ra.ap_actor["url"] = {"type": "Link", "href": "https://example.com/@toto"}
    ra.ap_actor["icon"] = "https://example.com/avatar.png"  # string, not object
    ra.ap_actor["attachment"] = [
        "not-a-dict",
        {"type": "PropertyValue", "name": "Web", "value": None},
    ]
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    def _note(**overrides: object) -> None:
        data = factories.build_note_object(
            from_remote_actor=ra, content="ok", to=[ap.AS_PUBLIC]
        )
        data.update(overrides)
        factories.InboxObjectFactory.from_remote_object(
            RemoteObject(data, ra), follower.actor
        )

    # sensitive explicitly null; summary null
    _note(sensitive=None, summary=None)
    # content null; url as a Link object list
    _note(
        content=None,
        url=[{"type": "Link", "href": "https://example.com/note/html"}],
    )
    # hostile tag list: non-dict entry, hashtag with null name, mention
    _note(
        tag=[
            "not-a-dict",
            {"type": "Hashtag", "name": None},
            {"type": "Hashtag", "name": "#ok", "href": None},
            {"type": "Mention", "href": "https://other.example/@x", "name": None},
        ]
    )
    # a well-formed image attachment (null description is allowed)
    _note(
        attachment=[
            {
                "type": "Document",
                "mediaType": "image/png",
                "url": "https://example.com/pic.png",
                "name": None,
            }
        ]
    )

    token = await _make_access_token(async_db_session, "read:statuses")
    response = client.get(
        "/api/v1/timelines/home?limit=40",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 4

    all_errors = []
    for i, status in enumerate(data):
        all_errors += _validate_status(status, f"status[{i}]")
    assert not all_errors, "conformance violations:\n" + "\n".join(all_errors)


@pytest.mark.asyncio
async def test_notifications_account_media_fields_are_never_empty(
    client: TestClient,
    async_db_session: AsyncSession,
    respx_mock: respx.MockRouter,
) -> None:
    # The common case, not a hostile one: a remote actor that simply never
    # set an icon/header (setup_remote_actor's default). Real Mastodon still
    # resolves avatar/avatar_static/header/header_static to a placeholder
    # URL here — a strict client's Account model has no reason to treat
    # these as optional.
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    async_db_session.add(
        models.Notification(
            notification_type=models.NotificationType.NEW_FOLLOWER,
            actor_id=follower.actor.id,
        )
    )
    await async_db_session.commit()

    token = await _make_access_token(async_db_session, "read:notifications")
    response = client.get(
        "/api/v1/notifications", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1

    errors = _validate_account(data[0]["account"], "notification[0].account")
    assert not errors, "conformance violations:\n" + "\n".join(errors)
