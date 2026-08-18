import io
import secrets
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import activitypub.models
from activitypub import activitypub as ap
from app import models
from app import scheduled_statuses
from app.utils.datetime import as_utc
from app.utils.datetime import now


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


def _png_bytes(size: tuple[int, int] = (16, 12)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(0, 128, 0)).save(buf, format="PNG")
    return buf.getvalue()


def _in(minutes: int) -> str:
    return (now() + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


async def _scheduled_rows(
    db_session: AsyncSession,
) -> list[models.ScheduledStatus]:
    return list(
        (
            await db_session.scalars(
                select(models.ScheduledStatus).order_by(models.ScheduledStatus.id)
            )
        ).all()
    )


async def _outbox_notes(
    db_session: AsyncSession,
) -> list[activitypub.models.OutboxObject]:
    return list(
        (
            await db_session.scalars(
                select(activitypub.models.OutboxObject).where(
                    activitypub.models.OutboxObject.ap_type == "Note"
                )
            )
        ).all()
    )


@pytest.mark.asyncio
async def test_scheduled_status_is_queued_not_published(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:statuses")

    response = client.post(
        "/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "status": "Later, alligator",
            "visibility": "unlisted",
            "spoiler_text": "cw",
            "language": "ca",
            "scheduled_at": _in(30),
        },
    )

    assert response.status_code == 200
    entity = response.json()
    assert entity["id"] == "1"
    assert entity["scheduled_at"].endswith("Z")
    assert entity["media_attachments"] == []
    assert entity["params"]["text"] == "Later, alligator"
    assert entity["params"]["visibility"] == "unlisted"
    assert entity["params"]["spoiler_text"] == "cw"
    assert entity["params"]["language"] == "ca"
    assert entity["params"]["sensitive"] is True
    assert entity["params"]["poll"] is None
    # Mastodon keeps the time in the top-level field only.
    assert entity["params"]["scheduled_at"] is None

    # Nothing published yet.
    assert await _outbox_notes(async_db_session) == []
    rows = await _scheduled_rows(async_db_session)
    assert len(rows) == 1
    assert rows[0].tries == 0
    assert rows[0].next_try is not None


@pytest.mark.asyncio
async def test_scheduled_status_accepts_json_body_with_poll(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:statuses")

    response = client.post(
        "/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "status": "Pick one",
            "scheduled_at": _in(10),
            "poll": {"options": ["a", "b"], "expires_in": 600, "multiple": True},
        },
    )

    assert response.status_code == 200
    params = response.json()["params"]
    assert params["poll"] == {
        "options": ["a", "b"],
        "expires_in": 600,
        "multiple": True,
    }


@pytest.mark.asyncio
async def test_scheduled_status_rejects_past_and_invalid_dates(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:statuses")
    headers = {"Authorization": f"Bearer {token}"}

    past = client.post(
        "/api/v1/statuses",
        headers=headers,
        data={"status": "Too late", "scheduled_at": _in(-5)},
    )
    assert past.status_code == 422
    assert "future" in past.json()["error_description"]

    garbage = client.post(
        "/api/v1/statuses",
        headers=headers,
        data={"status": "Nope", "scheduled_at": "next tuesday"},
    )
    assert garbage.status_code == 422

    assert await _scheduled_rows(async_db_session) == []


@pytest.mark.asyncio
async def test_empty_scheduled_at_posts_immediately(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    """Some clients send `scheduled_at=""` for an ordinary post."""
    token = await _make_access_token(async_db_session, "write:statuses")

    response = client.post(
        "/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}"},
        data={"status": "Right now", "scheduled_at": ""},
    )

    assert response.status_code == 200
    assert response.json()["content"] != ""
    assert await _scheduled_rows(async_db_session) == []
    assert len(await _outbox_notes(async_db_session)) == 1


@pytest.mark.asyncio
async def test_scheduled_status_validates_media_ids_upfront(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:statuses")

    response = client.post(
        "/api/v1/statuses",
        headers={"Authorization": f"Bearer {token}"},
        data={"status": "With media", "scheduled_at": _in(30), "media_ids[]": "404"},
    )

    assert response.status_code == 422
    assert await _scheduled_rows(async_db_session) == []


@pytest.mark.asyncio
async def test_scheduled_status_idempotency_key(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "write:statuses")
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "abc123"}
    body = {"status": "Only once", "scheduled_at": _in(30)}

    first = client.post("/api/v1/statuses", headers=headers, data=body)
    second = client.post("/api/v1/statuses", headers=headers, data=body)

    assert first.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert first.json()["params"]["idempotency"] == "abc123"
    assert len(await _scheduled_rows(async_db_session)) == 1


@pytest.mark.asyncio
async def test_scheduled_statuses_list_show_and_delete(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read:statuses write:statuses")
    headers = {"Authorization": f"Bearer {token}"}

    first = client.post(
        "/api/v1/statuses",
        headers=headers,
        data={"status": "1", "scheduled_at": _in(10)},
    ).json()
    second = client.post(
        "/api/v1/statuses",
        headers=headers,
        data={"status": "2", "scheduled_at": _in(20)},
    ).json()

    listed = client.get("/api/v1/scheduled_statuses", headers=headers)
    assert listed.status_code == 200
    # Newest queued first, like every other paginated list here.
    assert [entity["id"] for entity in listed.json()] == [second["id"], first["id"]]
    assert "Link" in listed.headers

    paged = client.get("/api/v1/scheduled_statuses?limit=1", headers=headers).json()
    assert [entity["id"] for entity in paged] == [second["id"]]
    next_page = client.get(
        f"/api/v1/scheduled_statuses?limit=1&max_id={second['id']}", headers=headers
    ).json()
    assert [entity["id"] for entity in next_page] == [first["id"]]

    shown = client.get(f"/api/v1/scheduled_statuses/{first['id']}", headers=headers)
    assert shown.status_code == 200
    assert shown.json()["params"]["text"] == "1"

    assert (
        client.get("/api/v1/scheduled_statuses/12345", headers=headers).status_code
        == 404
    )

    deleted = client.delete(
        f"/api/v1/scheduled_statuses/{first['id']}", headers=headers
    )
    assert deleted.status_code == 200
    assert deleted.json() == {}
    remaining = client.get("/api/v1/scheduled_statuses", headers=headers).json()
    assert [entity["id"] for entity in remaining] == [second["id"]]


@pytest.mark.asyncio
async def test_scheduled_statuses_update_resets_retry_state(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    token = await _make_access_token(async_db_session, "read:statuses write:statuses")
    headers = {"Authorization": f"Bearer {token}"}

    created = client.post(
        "/api/v1/statuses",
        headers=headers,
        data={"status": "Reschedule me", "scheduled_at": _in(10)},
    ).json()

    # Pretend the worker gave up on it.
    row = (await _scheduled_rows(async_db_session))[0]
    row.tries = scheduled_statuses.MAX_TRIES
    row.next_try = None
    row.last_error = "boom"
    await async_db_session.commit()

    updated = client.put(
        f"/api/v1/scheduled_statuses/{created['id']}",
        headers=headers,
        data={"scheduled_at": _in(120)},
    )
    assert updated.status_code == 200
    assert updated.json()["scheduled_at"] != created["scheduled_at"]

    await async_db_session.refresh(row)
    assert row.tries == 0
    assert row.next_try is not None
    assert row.last_error is None

    missing_param = client.put(
        f"/api/v1/scheduled_statuses/{created['id']}", headers=headers, data={}
    )
    assert missing_param.status_code == 422


@pytest.mark.asyncio
async def test_scheduled_statuses_endpoints_require_auth(client: TestClient) -> None:
    assert client.get("/api/v1/scheduled_statuses").status_code == 401
    assert client.get("/api/v1/scheduled_statuses/1").status_code == 401
    assert client.put("/api/v1/scheduled_statuses/1").status_code == 401
    assert client.delete("/api/v1/scheduled_statuses/1").status_code == 401


# --- The worker pass -------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_publishes_due_rows_only(
    async_db_session: AsyncSession,
) -> None:
    due = await scheduled_statuses.schedule(
        async_db_session,
        scheduled_statuses.ComposeParams(
            content="Due now", visibility=ap.VisibilityEnum.UNLISTED
        ),
        now() - timedelta(minutes=1),
    )
    not_due = await scheduled_statuses.schedule(
        async_db_session,
        scheduled_statuses.ComposeParams(content="Later"),
        now() + timedelta(hours=1),
    )

    published = await scheduled_statuses.publish_due_scheduled_statuses(
        async_db_session
    )

    assert published == 1
    notes = await _outbox_notes(async_db_session)
    assert len(notes) == 1
    assert "Due now" in (notes[0].content or "")
    assert notes[0].visibility == ap.VisibilityEnum.UNLISTED
    assert notes[0].source == "Due now"

    remaining = await _scheduled_rows(async_db_session)
    assert [row.id for row in remaining] == [not_due.id]
    assert due.id not in {row.id for row in remaining}


@pytest.mark.asyncio
async def test_worker_keeps_and_backs_off_a_failing_row(
    async_db_session: AsyncSession,
) -> None:
    # A media id that no longer resolves — the row was queued while the upload
    # existed, and it has since been deleted.
    row = models.ScheduledStatus(
        scheduled_at=now() - timedelta(minutes=1),
        next_try=now() - timedelta(minutes=1),
        params=scheduled_statuses.ComposeParams(
            content="Orphaned attachment", media_ids=["999"]
        ).to_json(),
    )
    async_db_session.add(row)
    await async_db_session.commit()

    published = await scheduled_statuses.publish_due_scheduled_statuses(
        async_db_session
    )

    assert published == 0
    assert await _outbox_notes(async_db_session) == []

    await async_db_session.refresh(row)
    assert row.tries == 1
    assert row.next_try is not None
    # SQLite hands back naive datetimes (stored as UTC wall clock, like every
    # other timestamp column here).
    assert as_utc(row.next_try) > now()
    assert "999" in (row.last_error or "")


@pytest.mark.asyncio
async def test_worker_gives_up_after_max_tries(
    async_db_session: AsyncSession,
) -> None:
    row = models.ScheduledStatus(
        scheduled_at=now() - timedelta(minutes=1),
        next_try=now() - timedelta(minutes=1),
        tries=scheduled_statuses.MAX_TRIES - 1,
        params=scheduled_statuses.ComposeParams(
            content="Doomed", media_ids=["999"]
        ).to_json(),
    )
    async_db_session.add(row)
    await async_db_session.commit()

    await scheduled_statuses.publish_due_scheduled_statuses(async_db_session)

    await async_db_session.refresh(row)
    assert row.tries == scheduled_statuses.MAX_TRIES
    # NULL next_try: no longer picked up, but still listed so it can be
    # rescheduled from a client.
    assert row.next_try is None
    assert await scheduled_statuses.fetch_due_scheduled_statuses(async_db_session) == []


def test_compose_params_json_round_trip_tolerates_unknown_keys() -> None:
    params = scheduled_statuses.ComposeParams(
        content="hi",
        visibility=ap.VisibilityEnum.FOLLOWERS_ONLY,
        media_ids=["1", "2"],
        poll_options=["a", "b"],
        poll_expires_in=600,
    )
    blob = params.to_json()
    assert blob["visibility"] == "followers-only"

    blob["some_future_param"] = "ignored"
    assert scheduled_statuses.ComposeParams.from_json(blob) == params


@pytest.mark.asyncio
async def test_worker_pass_survives_a_failing_row(
    async_db_session: AsyncSession,
) -> None:
    """A rollback for one row must not poison the rest of the pass."""
    broken = models.ScheduledStatus(
        scheduled_at=now() - timedelta(minutes=2),
        next_try=now() - timedelta(minutes=2),
        params=scheduled_statuses.ComposeParams(
            content="Broken", media_ids=["999"]
        ).to_json(),
    )
    async_db_session.add(broken)
    await async_db_session.commit()
    await scheduled_statuses.schedule(
        async_db_session,
        scheduled_statuses.ComposeParams(content="Fine"),
        now() - timedelta(minutes=1),
    )

    published = await scheduled_statuses.publish_due_scheduled_statuses(
        async_db_session
    )

    assert published == 1
    notes = await _outbox_notes(async_db_session)
    assert [note.source for note in notes] == ["Fine"]
    assert [row.id for row in await _scheduled_rows(async_db_session)] == [broken.id]


@pytest.mark.asyncio
async def test_worker_rate_limits_the_scheduled_pass(
    async_db_session: AsyncSession,
) -> None:
    """The pass is throttled so a delivery backlog isn't interleaved with
    publishing on every single loop iteration."""
    from activitypub.outgoing_activities import OutgoingActivityWorker

    worker = OutgoingActivityWorker()

    await scheduled_statuses.schedule(
        async_db_session,
        scheduled_statuses.ComposeParams(content="First"),
        now() - timedelta(minutes=1),
    )
    await worker._publish_due_scheduled_statuses(async_db_session)
    assert await _scheduled_rows(async_db_session) == []

    # Due, but the interval hasn't elapsed: skipped without touching the DB.
    await scheduled_statuses.schedule(
        async_db_session,
        scheduled_statuses.ComposeParams(content="Second"),
        now() - timedelta(minutes=1),
    )
    await worker._publish_due_scheduled_statuses(async_db_session)
    assert len(await _scheduled_rows(async_db_session)) == 1

    worker.scheduled_statuses_interval = 0.0
    await worker._publish_due_scheduled_statuses(async_db_session)
    assert await _scheduled_rows(async_db_session) == []


@pytest.mark.asyncio
async def test_scheduled_statuses_list_serializes_attachments(
    client: TestClient, async_db_session: AsyncSession
) -> None:
    """Covers the batched upload prefetch the list endpoint runs."""
    token = await _make_access_token(
        async_db_session, "read:statuses write:statuses write:media"
    )
    headers = {"Authorization": f"Bearer {token}"}

    media_ids = []
    for _ in range(2):
        upload = client.post(
            "/api/v2/media",
            headers=headers,
            files={"file": ("img.png", _png_bytes(), "image/png")},
            data={"description": "alt text"},
        )
        assert upload.status_code == 200
        media_ids.append(upload.json()["id"])

    for media_id in media_ids:
        queued = client.post(
            "/api/v1/statuses",
            headers=headers,
            data={
                "status": "With media",
                "scheduled_at": _in(30),
                "media_ids[]": media_id,
            },
        )
        assert queued.status_code == 200

    listed = client.get("/api/v1/scheduled_statuses", headers=headers).json()
    assert len(listed) == 2
    for entity in listed:
        assert len(entity["media_attachments"]) == 1
        assert entity["media_attachments"][0]["description"] == "alt text"
        assert entity["params"]["media_ids"] == [entity["media_attachments"][0]["id"]]
