import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Iterator
from unittest import mock
from uuid import uuid4

import fastapi
import httpx
import respx
from sqlalchemy import event

import activitypub.models
from activitypub import activitypub as ap
from activitypub import actor
from activitypub.actor import LOCAL_ACTOR
from activitypub.ap_object import RemoteObject
from activitypub.incoming_activities import fetch_next_incoming_activity
from activitypub.incoming_activities import process_next_incoming_activity
from activitypub.tests import factories
from app import httpsig
from app import models
from app.config import session_serializer
from app.database import AsyncSession
from app.database import async_engine
from app.database import async_session
from app.main import app


@contextmanager
def mock_httpsig_checker(
    ra: actor.RemoteActor,
    has_valid_signature: bool = True,
    is_ap_actor_gone: bool = False,
):
    async def httpsig_checker(
        request: fastapi.Request,
    ) -> httpsig.HTTPSigInfo:
        return httpsig.HTTPSigInfo(
            has_valid_signature=has_valid_signature,
            signed_by_ap_actor_id=ra.ap_id,
            is_ap_actor_gone=is_ap_actor_gone,
        )

    app.dependency_overrides[httpsig.httpsig_checker] = httpsig_checker
    try:
        yield
    finally:
        del app.dependency_overrides[httpsig.httpsig_checker]


def generate_admin_session_cookies() -> dict[str, Any]:
    return {"session": session_serializer.dumps({"is_logged_in": True})}


def setup_remote_actor(
    respx_mock: respx.MockRouter,
    base_url="https://example.com",
    also_known_as=None,
) -> actor.RemoteActor:
    ra = factories.RemoteActorFactory(
        base_url=base_url,
        username="toto",
        public_key="pk",
        also_known_as=also_known_as if also_known_as else [],
    )
    respx_mock.get(ra.ap_id + "/outbox").mock(
        return_value=httpx.Response(
            200,
            json={
                "@context": ap.AS_EXTENDED_CTX,
                "id": f"{ra.ap_id}/outbox",
                "type": "OrderedCollection",
                "totalItems": 0,
                "orderedItems": [],
            },
        )
    )
    respx_mock.get(ra.ap_id).mock(return_value=httpx.Response(200, json=ra.ap_actor))
    return ra


def setup_remote_actor_as_follower(
    ra: actor.RemoteActor,
) -> activitypub.models.Follower:
    actor = factories.ActorFactory.from_remote_actor(ra)

    follow_id = uuid4().hex
    follow_from_inbox = RemoteObject(
        factories.build_follow_activity(
            from_remote_actor=ra,
            for_remote_actor=LOCAL_ACTOR,
            outbox_public_id=follow_id,
        ),
        ra,
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        follow_from_inbox, actor
    )

    follower = factories.FollowerFactory(
        inbox_object_id=inbox_object.id,
        actor_id=actor.id,
        ap_actor_id=actor.ap_id,
    )
    return follower


def setup_remote_actor_as_following(
    ra: actor.RemoteActor,
) -> activitypub.models.Following:
    actor = factories.ActorFactory.from_remote_actor(ra)

    follow_id = uuid4().hex
    follow_from_outbox = RemoteObject(
        factories.build_follow_activity(
            from_remote_actor=LOCAL_ACTOR,
            for_remote_actor=ra,
            outbox_public_id=follow_id,
        ),
        LOCAL_ACTOR,
    )
    outbox_object = factories.OutboxObjectFactory.from_remote_object(
        follow_id, follow_from_outbox
    )

    following = factories.FollowingFactory(
        outbox_object_id=outbox_object.id,
        actor_id=actor.id,
        ap_actor_id=actor.ap_id,
    )
    return following


def setup_remote_actor_as_following_and_follower(
    ra: actor.RemoteActor,
) -> tuple[activitypub.models.Following, activitypub.models.Follower]:
    actor = factories.ActorFactory.from_remote_actor(ra)

    follow_id = uuid4().hex
    follow_from_outbox = RemoteObject(
        factories.build_follow_activity(
            from_remote_actor=LOCAL_ACTOR,
            for_remote_actor=ra,
            outbox_public_id=follow_id,
        ),
        LOCAL_ACTOR,
    )
    outbox_object = factories.OutboxObjectFactory.from_remote_object(
        follow_id, follow_from_outbox
    )

    following = factories.FollowingFactory(
        outbox_object_id=outbox_object.id,
        actor_id=actor.id,
        ap_actor_id=actor.ap_id,
    )

    follow_id = uuid4().hex
    follow_from_inbox = RemoteObject(
        factories.build_follow_activity(
            from_remote_actor=ra,
            for_remote_actor=LOCAL_ACTOR,
            outbox_public_id=follow_id,
        ),
        ra,
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        follow_from_inbox, actor
    )

    follower = factories.FollowerFactory(
        inbox_object_id=inbox_object.id,
        actor_id=actor.id,
        ap_actor_id=actor.ap_id,
    )

    return following, follower


def setup_outbox_note(
    content: str = "Hello",
    to: list[str] | None = None,
    cc: list[str] | None = None,
    tags: list[ap.RawObject] | None = None,
    in_reply_to: str | None = None,
) -> activitypub.models.OutboxObject:
    note_id = uuid4().hex
    note_from_outbox = RemoteObject(
        factories.build_note_object(
            from_remote_actor=LOCAL_ACTOR,
            outbox_public_id=note_id,
            content=content,
            to=to,
            cc=cc,
            tags=tags,
            in_reply_to=in_reply_to,
        ),
        LOCAL_ACTOR,
    )
    return factories.OutboxObjectFactory.from_remote_object(note_id, note_from_outbox)


def setup_inbox_note(
    actor: activitypub.models.Actor,
    content: str = "Hello",
    to: list[str] | None = None,
    cc: list[str] | None = None,
    tags: list[ap.RawObject] | None = None,
    in_reply_to: str | None = None,
) -> activitypub.models.InboxObject:
    note_id = uuid4().hex
    note_from_outbox = RemoteObject(
        factories.build_note_object(
            from_remote_actor=actor,
            outbox_public_id=note_id,
            content=content,
            to=to,
            cc=cc,
            tags=tags,
            in_reply_to=in_reply_to,
        ),
        actor,
    )
    return factories.InboxObjectFactory.from_remote_object(note_from_outbox, actor)


def setup_inbox_delete(
    actor: activitypub.models.Actor, deleted_object_ap_id: str
) -> activitypub.models.InboxObject:
    follow_from_inbox = RemoteObject(
        factories.build_delete_activity(
            from_remote_actor=actor,
            deleted_object_ap_id=deleted_object_ap_id,
        ),
        actor,
    )
    inbox_object = factories.InboxObjectFactory.from_remote_object(
        follow_from_inbox, actor
    )
    return inbox_object


def run_async(func, *args, **kwargs):
    async def _func():
        async with async_session() as db:
            return await func(db, *args, **kwargs)

    asyncio.run(_func())


async def _process_next_incoming_activity(db_session: AsyncSession) -> None:
    next_activity = await fetch_next_incoming_activity(db_session)
    assert next_activity
    await process_next_incoming_activity(db_session, next_activity)


def run_process_next_incoming_activity() -> None:
    run_async(_process_next_incoming_activity)


async def setup_auth_application_client(db: AsyncSession):
    # adds auth app client to db
    client = models.OAuthClient(
        client_name="testclient",
        redirect_uris=["testuri"],
        client_id="testclientid",
        client_secret="testclientsecret",
    )

    db.add(client)
    await db.commit()


async def setup_auth_auth_token(db: AsyncSession):
    # adds authorized access token to DB
    await setup_auth_application_client(db)
    auth_request = models.IndieAuthAuthorizationRequest(
        code="accesscode",
        scope="create",
        redirect_uri="testuri",
        client_id="testclientid",
        code_challenge="",
        code_challenge_method="",
    )

    db.add(auth_request)
    await db.commit()


async def setup_auth_access_token(db: AsyncSession):
    # adds authorized access token to DB
    await setup_auth_auth_token(db)
    access_token = models.IndieAuthAccessToken(
        indieauth_authorization_request_id=1,
        access_token="accesstoken",
        refresh_token="refreshtoken",
        expires_in=3600,
        scope="create update",
    )
    db.add(access_token)
    await db.commit()


@contextmanager
def no_network_while_writing() -> Iterator[list[str]]:
    """Fail if an HTTP request starts while the async engine's connection has
    uncommitted writes.

    In production that means SQLite's writer lock is held across the
    network, blocking every other process's writes for as long as the remote
    server takes (and failing them after the 15s busy timeout). Autouse in
    both conftests, so the whole suite guards this.
    """
    writing: list[str] = []
    violations: list[str] = []
    sync_engine = async_engine.sync_engine

    def _on_execute(conn, cursor, statement, *args) -> None:
        verb = statement.lstrip().split(None, 1)[0].upper()
        if not writing and verb in ("INSERT", "UPDATE", "DELETE"):
            writing.append(" ".join(statement.split()[:3]))

    def _on_end(conn) -> None:
        writing.clear()

    original_send = httpx.AsyncClient.send

    async def _send(self, request, *args, **kwargs):
        if writing:
            violations.append(f"{request.method} {request.url} after {writing[0]}")
        return await original_send(self, request, *args, **kwargs)

    listeners: list[tuple[str, Callable[..., Any]]] = [
        ("before_cursor_execute", _on_execute),
        ("commit", _on_end),
        ("rollback", _on_end),
    ]
    for name, fn in listeners:
        event.listen(sync_engine, name, fn)
    try:
        with mock.patch.object(httpx.AsyncClient, "send", _send):
            yield violations
    finally:
        for name, fn in listeners:
            event.remove(sync_engine, name, fn)
    # Collected, not raised from `_send`: code under test often swallows
    # exceptions from network calls.
    assert not violations, "network while holding the SQLite writer lock: " + (
        "; ".join(violations)
    )


_JSONLD_CONTEXTS = {
    "https://www.w3.org/ns/activitystreams": "activitystreams.jsonld",
    "https://w3id.org/security/v1": "security-v1.jsonld",
}


@contextmanager
def offline_jsonld_contexts() -> Iterator[None]:
    """Serve the JSON-LD contexts LD signatures need from vendored copies
    (`tests/jsonld_contexts/`) instead of fetching them, so the suite doesn't
    depend on w3.org / w3id.org. Autouse in both conftests.

    Any other context URL fails loudly rather than going to the network.
    """
    import pyld  # type: ignore

    from app import ldsig

    contexts_dir = Path(__file__).parent / "jsonld_contexts"

    def _offline_loader(url: str, options: dict | None = None) -> dict:
        if (filename := _JSONLD_CONTEXTS.get(url)) is None:
            raise ValueError(f"test tried to fetch JSON-LD context {url}")
        return {
            "contextUrl": None,
            "documentUrl": url,
            "document": json.loads((contexts_dir / filename).read_text()),
        }

    pyld.jsonld.set_document_loader(_offline_loader)
    try:
        yield
    finally:
        pyld.jsonld.set_document_loader(ldsig._loader)
