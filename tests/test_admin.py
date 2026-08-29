from datetime import timedelta
from typing import Iterator
from uuid import uuid4

import respx
import starlette
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import activitypub.models
from activitypub import activitypub as ap
from activitypub.actor import LOCAL_ACTOR
from activitypub.ap_object import RemoteObject
from activitypub.tests import factories
from app import config
from app import models
from app.config import generate_csrf_token
from app.main import app
from app.utils.datetime import now
from tests.utils import generate_admin_session_cookies
from tests.utils import setup_outbox_note
from tests.utils import setup_remote_actor
from tests.utils import setup_remote_actor_as_follower


def _iter_endpoint_routes(
    routes: list, prefix: str = ""
) -> Iterator[tuple[str, set[str]]]:
    """Yield (full_path, methods) for every concrete endpoint route.

    FastAPI >=0.139 no longer flattens ``include_router`` calls into
    ``app.routes``; instead it inserts lazy ``_IncludedRouter`` wrappers that
    reference the original router and its prefix. Recurse through those so
    included (e.g. /admin) routes are still discoverable.
    """
    for route in routes:
        if isinstance(route, starlette.routing.Route):
            yield prefix + route.path, route.methods or set()
            continue

        include_context = getattr(route, "include_context", None)
        original_router = getattr(route, "original_router", None)
        if include_context is not None and original_router is not None:
            sub_prefix = prefix + getattr(include_context, "prefix", "")
            yield from _iter_endpoint_routes(original_router.routes, sub_prefix)


def test_admin_endpoints_are_authenticated(client: TestClient) -> None:
    routes_tested = []

    for path, methods in _iter_endpoint_routes(app.routes):
        if not path.startswith("/admin") or path == "/admin/login":
            continue

        for method in methods:
            resp = client.request(method, path, follow_redirects=False)

            # Admin routes should redirect to the login page
            assert resp.status_code == 302, f"{method} {path} is unauthenticated"
            assert resp.headers.get("Location", "").startswith(
                "http://testserver/admin/login"
            )
            routes_tested.append((method, path))

    assert len(routes_tested) > 0


def test_public_works_authenticated(client: TestClient) -> None:
    response = client.post(
        "/admin/actions/new",
        data={
            "content": "hello",
            "redirect_url": "http://testserver/",
            "visibility": ap.VisibilityEnum.PUBLIC.name,
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )
    assert response.status_code == 302
    resp = client.get("/", cookies=generate_admin_session_cookies())
    assert resp.status_code == 200


def test_admin_lookup_rejects_non_url_query(client: TestClient) -> None:
    """A bare word (not a URL, not an `@user@domain.tld` handle) is invalid
    input, not a lookup failure -- `check_url` rejects it before any network
    call is made, and this used to surface as an opaque "Internal Error"."""
    response = client.get(
        "/admin/lookup",
        params={"query": "micro"},
        cookies=generate_admin_session_cookies(),
    )
    assert response.status_code == 200
    assert "This is not a URL or a fediverse handle" in response.text


def test_admin_actions_new_rejects_oversized_upload(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr("app.uploads.config.MAX_IMAGE_UPLOAD_SIZE", 10)

    response = client.post(
        "/admin/actions/new",
        data={
            "content": "hello",
            "redirect_url": "http://testserver/",
            "visibility": ap.VisibilityEnum.PUBLIC.name,
            "csrf_token": generate_csrf_token(),
        },
        files={"files": ("photo.png", b"\x89PNG" + b"x" * 100, "image/png")},
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "too large" in response.text.lower()
    # The submitted content is preserved in the re-rendered form.
    assert "hello" in response.text


def test_admin_blocks_lists_blocked_actors(
    db: Session, client: TestClient, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    blocked = factories.ActorFactory.from_remote_actor(ra)
    blocked.is_blocked = True
    db.commit()

    other_ra = setup_remote_actor(respx_mock, base_url="https://example.org")
    not_blocked = factories.ActorFactory.from_remote_actor(other_ra)

    response = client.get("/admin/blocks", cookies=generate_admin_session_cookies())

    assert response.status_code == 200
    assert blocked.ap_id in response.text
    assert not_blocked.ap_id not in response.text
    # display_actor() offers the unblock action for every listed actor
    assert "/admin/actions/unblock" in response.text


def test_admin_blocks_with_no_blocked_actor(client: TestClient) -> None:
    response = client.get("/admin/blocks", cookies=generate_admin_session_cookies())

    assert response.status_code == 200
    assert "No blocked accounts." in response.text


def test_admin_mutes_lists_muted_actors(
    db: Session, client: TestClient, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    muted = factories.ActorFactory.from_remote_actor(ra)
    muted.is_muted = True
    db.commit()

    other_ra = setup_remote_actor(respx_mock, base_url="https://example.org")
    not_muted = factories.ActorFactory.from_remote_actor(other_ra)

    response = client.get("/admin/mutes", cookies=generate_admin_session_cookies())

    assert response.status_code == 200
    assert muted.ap_id in response.text
    assert not_muted.ap_id not in response.text
    # display_actor() offers the unmute action for every listed actor
    assert "/admin/actions/unmute" in response.text


def test_admin_mutes_skips_expired_mute(
    db: Session, client: TestClient, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)
    actor.is_muted = True
    actor.muted_until = now() - timedelta(seconds=1)
    db.commit()

    response = client.get("/admin/mutes", cookies=generate_admin_session_cookies())

    assert response.status_code == 200
    assert actor.ap_id not in response.text
    assert "No muted accounts." in response.text


def test_admin_mutes_with_no_muted_actor(client: TestClient) -> None:
    response = client.get("/admin/mutes", cookies=generate_admin_session_cookies())

    assert response.status_code == 200
    assert "No muted accounts." in response.text


def test_admin_mute_and_unmute_actions(
    db: Session, client: TestClient, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)
    db.commit()

    response = client.post(
        "/admin/actions/mute",
        data={
            "ap_actor_id": actor.ap_id,
            "redirect_url": "http://testserver/admin/mutes",
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )
    assert response.status_code == 302
    db.refresh(actor)
    assert actor.is_muted is True
    # The admin button matches a Mastodon client's defaults.
    assert actor.are_notifications_muted is True
    assert actor.muted_until is None

    response = client.post(
        "/admin/actions/unmute",
        data={
            "ap_actor_id": actor.ap_id,
            "redirect_url": "http://testserver/admin/mutes",
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )
    assert response.status_code == 302
    db.refresh(actor)
    assert actor.is_muted is False


def test_admin_revoke_quote_action(
    db: Session, client: TestClient, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    actor = factories.ActorFactory.from_remote_actor(ra)
    quoted_object = setup_outbox_note()

    stamp = factories.OutboxObjectFactory.from_remote_object(
        uuid4().hex,
        RemoteObject(
            factories.build_quote_authorization(
                from_remote_actor=LOCAL_ACTOR,
                quoting_object_ap_id=ra.ap_id + "/note/quoting",
                quoted_object_ap_id=quoted_object.ap_id,
            ),
            LOCAL_ACTOR,
        ),
    )

    quoting_note = factories.InboxObjectFactory.from_remote_object(
        RemoteObject(
            factories.build_note_object(
                from_remote_actor=ra,
                outbox_public_id="quoting",
                content="RE: ...",
                quote=quoted_object.ap_id,
                quote_authorization=stamp.ap_id,
            ),
            ra,
        ),
        actor,
    )
    quoting_note.quote_ap_id = quoted_object.ap_id
    quoting_note.quote_authorization_ap_id = stamp.ap_id
    quoting_note.quote_is_verified = True
    db.commit()

    response = client.post(
        "/admin/actions/revoke_quote",
        data={
            "ap_object_id": quoting_note.ap_id,
            "redirect_url": "http://testserver/admin/notifications",
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )
    assert response.status_code == 302

    db.refresh(quoting_note)
    assert quoting_note.quote_is_verified is False

    db.refresh(stamp)
    assert stamp.is_deleted is True


def test_admin_stream_hides_muted_actor(
    db: Session, client: TestClient, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://example.com")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    factories.InboxObjectFactory.from_remote_object(
        RemoteObject(
            factories.build_note_object(from_remote_actor=ra, content="Noisy note"),
            ra,
        ),
        follower.actor,
    )

    response = client.get("/admin/stream", cookies=generate_admin_session_cookies())
    assert "Noisy note" in response.text

    follower.actor.is_muted = True
    db.commit()

    response = client.get("/admin/stream", cookies=generate_admin_session_cookies())
    assert response.status_code == 200
    assert "Noisy note" not in response.text


def test_admin_pin_enforces_max_pinned_limit(db: Session, client: TestClient) -> None:
    for i in range(activitypub.models.MAX_PINNED_OBJECTS + 1):
        response = client.post(
            "/admin/actions/new",
            data={
                "content": f"note {i}",
                "redirect_url": "http://testserver/",
                "visibility": ap.VisibilityEnum.PUBLIC.name,
                "csrf_token": generate_csrf_token(),
            },
            cookies=generate_admin_session_cookies(),
            follow_redirects=False,
        )
        assert response.status_code == 302

    outbox_objects = (
        db.query(activitypub.models.OutboxObject)
        .order_by(activitypub.models.OutboxObject.id)
        .all()
    )
    assert len(outbox_objects) == activitypub.models.MAX_PINNED_OBJECTS + 1

    for outbox_object in outbox_objects[: activitypub.models.MAX_PINNED_OBJECTS]:
        response = client.post(
            "/admin/actions/pin",
            data={
                "ap_object_id": outbox_object.ap_id,
                "redirect_url": "http://testserver/",
                "csrf_token": generate_csrf_token(),
            },
            cookies=generate_admin_session_cookies(),
            follow_redirects=False,
        )
        assert response.status_code == 302

    one_too_many = outbox_objects[activitypub.models.MAX_PINNED_OBJECTS]
    response = client.post(
        "/admin/actions/pin",
        data={
            "ap_object_id": one_too_many.ap_id,
            "redirect_url": "http://testserver/",
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_admin_edit_history(db: Session, client: TestClient) -> None:
    response = client.post(
        "/admin/actions/new",
        data={
            "content": "hello world",
            "redirect_url": "http://testserver/",
            "visibility": ap.VisibilityEnum.PUBLIC.name,
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )
    assert response.status_code == 302

    outbox_object = db.query(activitypub.models.OutboxObject).one()

    response = client.post(
        f"/admin/actions/edit_text/{outbox_object.public_id}",
        data={
            "content": "hello world, edited",
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )
    assert response.status_code == 302

    response = client.get(
        f"/admin/edit_history/{outbox_object.public_id}",
        cookies=generate_admin_session_cookies(),
    )
    assert response.status_code == 200
    assert "hello world" in response.text
    assert "hello world, edited" in response.text


def _create_note_via_admin(
    client: TestClient, content: str = "hello world", alias: str | None = None
) -> str:
    data = {
        "content": content,
        "redirect_url": "http://testserver/",
        "visibility": ap.VisibilityEnum.PUBLIC.name,
        "csrf_token": generate_csrf_token(),
    }
    if alias is not None:
        data["alias"] = alias
    response = client.post(
        "/admin/actions/new",
        data=data,
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )
    assert response.status_code == 302
    location = response.headers["location"]
    return location.rsplit("/", 1)[-1]


def test_admin_edit_text__alias_only_change_is_local_only(
    db: Session, client: TestClient
) -> None:
    public_id = _create_note_via_admin(client, content="hello world")
    outbox_object = db.query(activitypub.models.OutboxObject).one()
    assert outbox_object.public_id == public_id
    assert outbox_object.revisions is None

    response = client.post(
        f"/admin/actions/edit_text/{public_id}",
        data={
            "content": "hello world",
            "alias": "hello-world",
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == (
        f"http://localhost:8000/{config.ALIAS_URL_PREFIX}/hello-world"
    )

    db.refresh(outbox_object)
    assert outbox_object.alias == "hello-world"
    assert outbox_object.ap_object["url"] == (
        f"http://localhost:8000/{config.ALIAS_URL_PREFIX}/hello-world"
    )
    # Local-only: no Update federated, no revision recorded, nothing queued.
    assert outbox_object.revisions is None
    assert db.query(activitypub.models.OutgoingActivity).count() == 0


def test_admin_edit_text__content_and_alias_change_federates_update(
    db: Session, client: TestClient
) -> None:
    public_id = _create_note_via_admin(client, content="hello world")
    outbox_object = db.query(activitypub.models.OutboxObject).one()
    old_url = outbox_object.url

    response = client.post(
        f"/admin/actions/edit_text/{public_id}",
        data={
            "content": "hello world, edited",
            "alias": "hello-world",
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )

    assert response.status_code == 302

    db.refresh(outbox_object)
    assert outbox_object.alias == "hello-world"
    assert outbox_object.ap_object["url"] == (
        f"http://localhost:8000/{config.ALIAS_URL_PREFIX}/hello-world"
    )
    # An Update was federated: a revision was recorded, and it snapshots the
    # *old* URL (before the alias took effect).
    assert outbox_object.revisions and len(outbox_object.revisions) == 1
    assert outbox_object.revisions[0]["ap_object"]["url"] == old_url


def test_admin_edit_text__clearing_alias_restores_permalink(
    db: Session, client: TestClient
) -> None:
    public_id = _create_note_via_admin(
        client, content="hello world", alias="hello-world"
    )
    outbox_object = db.query(activitypub.models.OutboxObject).one()
    assert outbox_object.alias == "hello-world"

    response = client.post(
        f"/admin/actions/edit_text/{public_id}",
        data={
            "content": "hello world",
            "alias": "",
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == f"http://localhost:8000/o/{public_id}"

    db.refresh(outbox_object)
    assert outbox_object.alias is None
    assert outbox_object.ap_object["url"] == f"http://localhost:8000/o/{public_id}"


def test_admin_edit_text__rejects_duplicate_alias_but_allows_resubmitting_own(
    db: Session, client: TestClient
) -> None:
    _create_note_via_admin(client, content="first", alias="one")
    second_public_id = _create_note_via_admin(client, content="second", alias="two")

    # Trying to steal another post's alias is rejected.
    response = client.post(
        f"/admin/actions/edit_text/{second_public_id}",
        data={
            "content": "second",
            "alias": "one",
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )
    assert response.status_code == 422

    # Re-submitting the post's own unchanged alias is not a conflict.
    response = client.post(
        f"/admin/actions/edit_text/{second_public_id}",
        data={
            "content": "second",
            "alias": "two",
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )
    assert response.status_code == 302


def test_admin_edit_text__alias_is_slugified(db: Session, client: TestClient) -> None:
    public_id = _create_note_via_admin(client, content="hello world")

    response = client.post(
        f"/admin/actions/edit_text/{public_id}",
        data={
            "content": "hello world",
            "alias": "Hello World!",
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )

    assert response.status_code == 302
    outbox_object = db.query(activitypub.models.OutboxObject).one()
    assert outbox_object.alias == "hello-world"


def test_admin_edit_text__alias_only_change_preserves_question_poll_answers(
    db: Session, client: TestClient
) -> None:
    response = client.post(
        "/admin/actions/new",
        data={
            "redirect_url": "http://testserver/",
            "content": "what do you think",
            "visibility": ap.VisibilityEnum.PUBLIC.name,
            "csrf_token": generate_csrf_token(),
            "poll_type": "oneOf",
            "poll_duration": "5",
            "poll_answer_1": "A",
            "poll_answer_2": "B",
        },
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )
    assert response.status_code == 302

    outbox_object = db.query(activitypub.models.OutboxObject).one()
    assert outbox_object.ap_type == "Question"
    assert len(outbox_object.poll_items) == 2

    response = client.post(
        f"/admin/actions/edit_text/{outbox_object.public_id}",
        data={
            "content": "what do you think",
            "alias": "poll-1",
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )
    assert response.status_code == 302

    db.refresh(outbox_object)
    assert outbox_object.alias == "poll-1"
    # Alias-only edits skip `send_update`, which would otherwise rebuild the
    # note from scratch and silently drop the poll answers.
    assert len(outbox_object.poll_items) == 2
    assert {pi["name"] for pi in outbox_object.poll_items} == {"A", "B"}


def test_admin_edit_text__updates_content_warning_and_sensitive(
    db: Session, client: TestClient
) -> None:
    public_id = _create_note_via_admin(client, content="hello world")
    outbox_object = db.query(activitypub.models.OutboxObject).one()
    assert outbox_object.summary is None
    assert outbox_object.sensitive is False

    response = client.post(
        f"/admin/actions/edit_text/{public_id}",
        data={
            "content": "hello world",
            "content_warning": "spoilers",
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )
    assert response.status_code == 302

    db.refresh(outbox_object)
    assert outbox_object.summary == "spoilers"
    # A content warning implies sensitive, same rule as the compose form.
    assert outbox_object.sensitive is True
    assert outbox_object.revisions and len(outbox_object.revisions) == 1


def test_admin_edit_text__clearing_content_warning_federates_update(
    db: Session, client: TestClient
) -> None:
    public_id = _create_note_via_admin(client, content="hello world")
    outbox_object = db.query(activitypub.models.OutboxObject).one()

    response = client.post(
        f"/admin/actions/edit_text/{public_id}",
        data={
            "content": "hello world",
            "content_warning": "spoilers",
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )
    assert response.status_code == 302
    db.refresh(outbox_object)
    assert outbox_object.summary == "spoilers"

    response = client.post(
        f"/admin/actions/edit_text/{public_id}",
        data={
            "content": "hello world",
            "content_warning": "",
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )
    assert response.status_code == 302
    db.refresh(outbox_object)
    assert outbox_object.summary is None
    assert outbox_object.sensitive is False
    assert outbox_object.revisions and len(outbox_object.revisions) == 2


def test_admin_actions_new__invalid_language_rerenders_form_with_content(
    client: TestClient,
) -> None:
    response = client.post(
        "/admin/actions/new",
        data={
            "content": "a post I don't want to lose",
            "visibility": ap.VisibilityEnum.PUBLIC.name,
            "language": "not a code",
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "a post I don&#39;t want to lose" in response.text


def test_admin_actions_new__duplicate_alias_rerenders_form_with_content(
    client: TestClient,
) -> None:
    _create_note_via_admin(client, content="first", alias="taken")

    response = client.post(
        "/admin/actions/new",
        data={
            "content": "a second post",
            "visibility": ap.VisibilityEnum.PUBLIC.name,
            "alias": "taken",
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "a second post" in response.text


def test_admin_actions_new__article_without_title_rerenders_form(
    client: TestClient,
) -> None:
    response = client.post(
        "/admin/actions/new",
        data={
            "content": "body text",
            "visibility": ap.VisibilityEnum.PUBLIC.name,
            "type": "Article",
            "csrf_token": generate_csrf_token(),
        },
        cookies=generate_admin_session_cookies(),
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "body text" in response.text


def test_admin_actions_new__alias_sets_ap_url_from_the_start(
    db: Session, client: TestClient
) -> None:
    _create_note_via_admin(client, content="hello world", alias="hello-world")

    outbox_object = db.query(activitypub.models.OutboxObject).one()
    assert outbox_object.alias == "hello-world"
    assert outbox_object.ap_object["url"] == (
        f"http://localhost:8000/{config.ALIAS_URL_PREFIX}/hello-world"
    )


def test_admin_notifications__renders_an_inbound_report(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a report about a local post
    ra = setup_remote_actor(respx_mock)
    remote_actor = factories.ActorFactory.from_remote_actor(ra)
    outbox_object = setup_outbox_note()
    flag = RemoteObject(
        {
            **factories.build_flag_activity(
                from_remote_actor=ra,
                reported_ap_ids=[LOCAL_ACTOR.ap_id, outbox_object.ap_id],
                content="this is spam",
            ),
            "id": ra.ap_id + "#flag/1",
        },
        ra,
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(flag, remote_actor)
    db.add(
        models.Notification(
            notification_type=models.NotificationType.REPORTED,
            actor_id=remote_actor.id,
            inbox_object_id=inbox_object.id,
            outbox_object_id=outbox_object.id,
        )
    )
    db.commit()

    response = client.get(
        "/admin/notifications", cookies=generate_admin_session_cookies()
    )

    assert response.status_code == 200
    assert "sent a report" in response.text
    assert "this is spam" in response.text
