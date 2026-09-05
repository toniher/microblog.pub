"""Tests for the Mastodon Streaming API WebSocket endpoint.

Written as *sync* tests (`client`/`db` fixtures) rather than the house
`@pytest.mark.asyncio` + sync-`client.get()` pattern used elsewhere in
`tests/mastodon/`: `WebSocketTestSession.receive()` is a blocking call with
NO timeout parameter at all, so calling it from inside an async test would
block that test's own event loop, and a bug that never sends a frame would
hang the whole suite (`pytest-timeout` isn't installed). `_recv_json` below
runs the blocking receive in a background thread and bounds it with
`Future.result(timeout=...)`, which fails the test instead of hanging it —
the socket is closed at the end of every `with client.websocket_connect(...)`
block, which unblocks that background thread either way.
"""

import concurrent.futures
import json
import secrets

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import activitypub.models
from activitypub import activitypub as ap
from app import config
from app import models
from app.database import SessionLocal
from app.mastodon import streaming as streaming_module
from app.mastodon.streaming import hub
from app.utils.datetime import now
from tests.mastodon.test_conformance import _validate_status

_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=8)


def _recv_json(ws, timeout: float = 5.0) -> dict:
    future = _POOL.submit(ws.receive_json)
    return future.result(timeout=timeout)


def _make_access_token(db, scope: str) -> str:
    token = models.IndieAuthAccessToken(
        access_token=secrets.token_urlsafe(16),
        refresh_token=None,
        expires_in=3600,
        scope=scope,
    )
    db.add(token)
    db.commit()
    return token.access_token


def _make_public_note(
    public_id: str = "streaming-note",
) -> activitypub.models.OutboxObject:
    with SessionLocal() as session:
        obj = activitypub.models.OutboxObject(
            public_id=public_id,
            ap_type="Note",
            ap_id=f"https://example.test/objects/{public_id}",
            ap_object={
                "id": f"https://example.test/objects/{public_id}",
                "type": "Note",
                "content": "hello",
            },
            visibility=ap.VisibilityEnum.PUBLIC,
            ap_published_at=now(),
        )
        session.add(obj)
        session.commit()
        session.refresh(obj)
        session.expunge(obj)
        return obj


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    # Real polling would make every test in this module wait out the 1s
    # default; tests still bound their receive with a generous timeout on
    # top, since the pump is a background task the test doesn't control.
    monkeypatch.setattr(config, "STREAMING_POLL_INTERVAL", 0.02)
    monkeypatch.setattr(streaming_module.config, "STREAMING_POLL_INTERVAL", 0.02)
    yield


def test_streaming_health(client: TestClient) -> None:
    response = client.get("/api/v1/streaming/health")
    assert response.status_code == 200
    assert response.text == "OK"


def test_streaming_rejects_missing_token(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/api/v1/streaming?stream=public"):
            pass
    assert exc_info.value.code == 1008


def test_streaming_rejects_invalid_token(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/api/v1/streaming?stream=public&access_token=not-a-real-token"
        ):
            pass
    assert exc_info.value.code == 1008


def test_streaming_rejects_token_without_read_scope(client: TestClient, db) -> None:
    token = _make_access_token(db, "write")
    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(f"/api/v1/streaming?access_token={token}"):
            pass
    assert exc_info.value.code == 1008


def test_streaming_accepts_via_query_param(client: TestClient, db) -> None:
    token = _make_access_token(db, "read write follow push")
    with client.websocket_connect(f"/api/v1/streaming?access_token={token}"):
        pass


def test_streaming_accepts_via_authorization_header(client: TestClient, db) -> None:
    token = _make_access_token(db, "read write follow push")
    with client.websocket_connect(
        "/api/v1/streaming", headers={"Authorization": f"Bearer {token}"}
    ):
        pass


def test_streaming_echoes_subprotocol_from_sec_websocket_protocol(
    client: TestClient, db
) -> None:
    token = _make_access_token(db, "read write follow push")
    with client.websocket_connect("/api/v1/streaming", subprotocols=[token]) as ws:
        assert ws.accepted_subprotocol == token


def test_subscribe_to_unknown_stream_returns_error_frame(
    client: TestClient, db
) -> None:
    token = _make_access_token(db, "read write follow push")
    with client.websocket_connect(f"/api/v1/streaming?access_token={token}") as ws:
        ws.send_json({"type": "subscribe", "stream": "nonsense"})
        frame = _recv_json(ws)
        assert "error" in frame


def test_subscribe_to_unknown_list_returns_error_frame(client: TestClient, db) -> None:
    token = _make_access_token(db, "read write follow push")
    with client.websocket_connect(f"/api/v1/streaming?access_token={token}") as ws:
        ws.send_json({"type": "subscribe", "stream": "list", "list": "404"})
        frame = _recv_json(ws)
        assert frame["error"] == "Unknown list"


def test_subscribe_to_list_requires_scope(client: TestClient, db) -> None:
    token = _make_access_token(db, "read:statuses")
    with SessionLocal() as session:
        mastodon_list = models.MastodonList(title="Friends")
        session.add(mastodon_list)
        session.commit()
        list_id = str(mastodon_list.id)

    with client.websocket_connect(f"/api/v1/streaming?access_token={token}") as ws:
        ws.send_json({"type": "subscribe", "stream": "list", "list": list_id})
        frame = _recv_json(ws)
        assert frame["error"] == "This stream requires the read:lists scope"


def test_end_to_end_list_member_post_delivers_update_frame(
    client: TestClient, db
) -> None:
    token = _make_access_token(db, "read write follow push")

    with SessionLocal() as session:
        mastodon_list = models.MastodonList(title="Friends")
        session.add(mastodon_list)
        session.flush()
        actor = activitypub.models.Actor(
            ap_id="https://example.test/list-member",
            ap_actor={
                "id": "https://example.test/list-member",
                "type": "Person",
                "preferredUsername": "member",
                "inbox": "https://example.test/inbox",
            },
            ap_type="Person",
        )
        session.add(actor)
        session.flush()
        session.add(
            models.MastodonListMember(list_id=mastodon_list.id, actor_id=actor.id)
        )
        session.commit()
        list_id = str(mastodon_list.id)
        actor_ap_id = actor.ap_id

    with client.websocket_connect(f"/api/v1/streaming?access_token={token}") as ws:
        ws.send_json({"type": "subscribe", "stream": "list", "list": list_id})

        with SessionLocal() as session:
            actor = (
                session.query(activitypub.models.Actor)
                .filter(activitypub.models.Actor.ap_id == actor_ap_id)
                .one()
            )
            obj = activitypub.models.InboxObject(
                actor_id=actor.id,
                server="example.test",
                ap_actor_id=actor.ap_id,
                ap_type="Note",
                ap_id="https://example.test/objects/list-note",
                ap_object={
                    "id": "https://example.test/objects/list-note",
                    "type": "Note",
                    "content": "hello list",
                    "attributedTo": actor.ap_id,
                },
                ap_published_at=now(),
                visibility=ap.VisibilityEnum.PUBLIC,
            )
            session.add(obj)
            session.commit()

        frame = _recv_json(ws)
        assert frame["stream"] == ["list", list_id]
        assert frame["event"] == "update"
        payload = json.loads(frame["payload"])
        assert payload["content"] == "hello list"


def test_subscribe_to_user_notification_requires_scope(client: TestClient, db) -> None:
    token = _make_access_token(db, "read:statuses")
    with client.websocket_connect(f"/api/v1/streaming?access_token={token}") as ws:
        ws.send_json({"type": "subscribe", "stream": "user:notification"})
        frame = _recv_json(ws)
        assert "error" in frame


def test_end_to_end_public_post_delivers_update_frame(client: TestClient, db) -> None:
    token = _make_access_token(db, "read write follow push")
    with client.websocket_connect(
        f"/api/v1/streaming?stream=public&access_token={token}"
    ) as ws:
        _make_public_note("e2e-1")

        frame = _recv_json(ws)

        assert frame["event"] == "update"
        assert frame["stream"] == ["public"]
        assert isinstance(frame["payload"], str)
        payload = json.loads(frame["payload"])
        assert payload["id"]
        assert _validate_status(payload, "streaming_e2e") == []


def test_unsubscribe_stops_delivery(client: TestClient, db) -> None:
    token = _make_access_token(db, "read write follow push")
    with client.websocket_connect(
        f"/api/v1/streaming?stream=public&access_token={token}"
    ) as ws:
        ws.send_json({"type": "unsubscribe", "stream": "public"})

        _make_public_note("no-deliver")

        with pytest.raises(concurrent.futures.TimeoutError):
            _recv_json(ws, timeout=1.0)


def test_hub_has_no_lingering_task_after_last_disconnect(
    client: TestClient, db
) -> None:
    token = _make_access_token(db, "read write follow push")
    with client.websocket_connect(f"/api/v1/streaming?access_token={token}"):
        assert hub.subscriber_count() == 1
    assert hub.subscriber_count() == 0


def test_subscribing_past_the_stream_cap_is_rejected(client: TestClient, db) -> None:
    """`hashtag` keys carry a client-supplied tag, so without a cap one socket
    could grow `Subscriber.streams` (and thus `Hub.active_hashtags()`) without
    bound.
    """
    token = _make_access_token(db, "read write follow push")
    cap = streaming_module._MAX_STREAMS_PER_SUBSCRIBER
    with client.websocket_connect(f"/api/v1/streaming?access_token={token}") as ws:
        for i in range(cap):
            ws.send_json({"type": "subscribe", "stream": "hashtag", "tag": f"tag{i}"})
        ws.send_json({"type": "subscribe", "stream": "hashtag", "tag": "one-too-many"})

        frame = _recv_json(ws)
        assert frame["error"] == "Too many subscribed streams"


def test_resubscribing_an_existing_stream_at_the_cap_is_allowed(
    client: TestClient, db
) -> None:
    """The cap counts distinct streams, so a client sitting exactly at it can
    still re-send a subscribe it already holds without being errored at.
    """
    token = _make_access_token(db, "read write follow push")
    cap = streaming_module._MAX_STREAMS_PER_SUBSCRIBER
    with client.websocket_connect(f"/api/v1/streaming?access_token={token}") as ws:
        for i in range(cap):
            ws.send_json({"type": "subscribe", "stream": "hashtag", "tag": f"tag{i}"})
        ws.send_json({"type": "subscribe", "stream": "hashtag", "tag": "tag0"})
        # No error frame; prove the socket is still live by triggering one.
        ws.send_json({"type": "subscribe", "stream": "nonsense"})

        frame = _recv_json(ws)
        assert frame["error"] == "Unknown stream"


def test_reconnecting_after_the_last_disconnect_starts_a_working_pump(
    client: TestClient, db
) -> None:
    """A pump restart must not inherit the previous task's stop signal."""
    token = _make_access_token(db, "read write follow push")
    with client.websocket_connect(f"/api/v1/streaming?access_token={token}"):
        pass
    assert hub.subscriber_count() == 0

    with client.websocket_connect(
        f"/api/v1/streaming?stream=public&access_token={token}"
    ) as ws:
        _make_public_note("after-restart")

        frame = _recv_json(ws)
        assert frame["event"] == "update"
