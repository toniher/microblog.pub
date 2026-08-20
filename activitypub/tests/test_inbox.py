from unittest import mock
from uuid import uuid4

import httpx
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

import activitypub.models
from activitypub import activitypub as ap
from activitypub.actor import LOCAL_ACTOR
from activitypub.ap_object import RemoteObject
from activitypub.tests import factories
from app import models
from tests.utils import mock_httpsig_checker
from tests.utils import run_process_next_incoming_activity
from tests.utils import setup_inbox_delete
from tests.utils import setup_outbox_note
from tests.utils import setup_remote_actor
from tests.utils import setup_remote_actor_as_follower
from tests.utils import setup_remote_actor_as_following


def test_inbox_requires_httpsig(
    client: TestClient,
):
    response = client.post(
        "/inbox",
        headers={"Content-Type": ap.AS_CTX},
        json={},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid HTTP sig"


def test_inbox_incoming_follow_request(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor
    ra = factories.RemoteActorFactory(
        base_url="https://example.com",
        username="toto",
        public_key="pk",
    )
    respx_mock.get(ra.ap_id).mock(return_value=httpx.Response(200, json=ra.ap_actor))

    # When receiving a Follow activity
    follow_activity = RemoteObject(
        factories.build_follow_activity(
            from_remote_actor=ra,
            for_remote_actor=LOCAL_ACTOR,
        ),
        ra,
    )
    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=follow_activity.ap_object,
        )

    # Then the server returns a 202
    assert response.status_code == 202

    run_process_next_incoming_activity()

    # And the actor was saved in DB
    saved_actor = db.execute(select(activitypub.models.Actor)).scalar_one()
    assert saved_actor.ap_id == ra.ap_id

    # And the Follow activity was saved in the inbox
    inbox_object = db.execute(select(activitypub.models.InboxObject)).scalar_one()
    assert inbox_object.ap_object == follow_activity.ap_object

    # And a follower was internally created
    follower = db.execute(select(activitypub.models.Follower)).scalar_one()
    assert follower.ap_actor_id == ra.ap_id
    assert follower.actor_id == saved_actor.id
    assert follower.inbox_object_id == inbox_object.id

    # And an Accept activity was created in the outbox
    outbox_object = db.execute(select(activitypub.models.OutboxObject)).scalar_one()
    assert outbox_object.ap_type == "Accept"
    assert outbox_object.activity_object_ap_id == follow_activity.ap_id

    # And an outgoing activity was created to track the Accept activity delivery
    outgoing_activity = db.execute(
        select(activitypub.models.OutgoingActivity)
    ).scalar_one()
    assert outgoing_activity.outbox_object_id == outbox_object.id


def test_inbox_incoming_follow_request__manually_approves_followers(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor
    ra = factories.RemoteActorFactory(
        base_url="https://example.com",
        username="toto",
        public_key="pk",
    )
    respx_mock.get(ra.ap_id).mock(return_value=httpx.Response(200, json=ra.ap_actor))

    # When receiving a Follow activity
    follow_activity = RemoteObject(
        factories.build_follow_activity(
            from_remote_actor=ra,
            for_remote_actor=LOCAL_ACTOR,
        ),
        ra,
    )
    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=follow_activity.ap_object,
        )

    # Then the server returns a 202
    assert response.status_code == 202

    with mock.patch("activitypub.boxes.MANUALLY_APPROVES_FOLLOWERS", True):
        run_process_next_incoming_activity()

    # And the actor was saved in DB
    saved_actor = db.execute(select(activitypub.models.Actor)).scalar_one()
    assert saved_actor.ap_id == ra.ap_id

    # And the Follow activity was saved in the inbox
    inbox_object = db.execute(select(activitypub.models.InboxObject)).scalar_one()
    assert inbox_object.ap_object == follow_activity.ap_object

    # And no follower was internally created
    assert db.scalar(select(func.count(activitypub.models.Follower.id))) == 0


def test_inbox_accept_follow_request(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor
    ra = setup_remote_actor(respx_mock)
    actor_in_db = factories.ActorFactory.from_remote_actor(ra)

    # And a Follow activity in the outbox
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

    # When receiving a Accept activity
    accept_activity = RemoteObject(
        factories.build_accept_activity(
            from_remote_actor=ra,
            for_remote_object=follow_from_outbox,
        ),
        ra,
    )
    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=accept_activity.ap_object,
        )

    # Then the server returns a 202
    assert response.status_code == 202

    run_process_next_incoming_activity()

    # And the Accept activity was saved in the inbox
    inbox_activity = db.execute(select(activitypub.models.InboxObject)).scalar_one()
    assert inbox_activity.ap_type == "Accept"
    assert inbox_activity.relates_to_outbox_object_id == outbox_object.id
    assert inbox_activity.actor_id == actor_in_db.id

    # And a following entry was created internally
    following = db.execute(select(activitypub.models.Following)).scalar_one()
    assert following.ap_actor_id == actor_in_db.ap_id


def test_inbox__create_from_follower(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor
    ra = setup_remote_actor(respx_mock)

    # Who is also a follower
    setup_remote_actor_as_follower(ra)

    create_activity = factories.build_create_activity(
        factories.build_note_object(
            from_remote_actor=ra,
            outbox_public_id=str(uuid4()),
            content="Hello",
            to=[LOCAL_ACTOR.ap_id],
        )
    )

    # When receiving a Create activity
    ro = RemoteObject(create_activity, ra)

    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=ro.ap_object,
        )

    # Then the server returns a 202
    assert response.status_code == 202

    # And when processing the incoming activity
    run_process_next_incoming_activity()

    # Then the Create activity was saved
    create_activity_from_inbox: activitypub.models.InboxObject | None = db.execute(
        select(activitypub.models.InboxObject).where(
            activitypub.models.InboxObject.ap_type == "Create"
        )
    ).scalar_one_or_none()
    assert create_activity_from_inbox
    assert create_activity_from_inbox.ap_id == ro.ap_id

    # And the Note object was created
    note_activity_from_inbox: activitypub.models.InboxObject | None = db.execute(
        select(activitypub.models.InboxObject).where(
            activitypub.models.InboxObject.ap_type == "Note"
        )
    ).scalar_one_or_none()
    assert note_activity_from_inbox
    assert note_activity_from_inbox.ap_id == ro.activity_object_ap_id


def test_inbox__announce_of_unknown_object_sets_conversation(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor we follow
    ra = setup_remote_actor(respx_mock)
    setup_remote_actor_as_following(ra)

    # And a Note from a different, unknown remote actor. The note-path mock
    # must be registered before the actor's own (path-less) mock: respx
    # matches routes in registration order, and a path-less URL matches any
    # path under that host, so a broader route registered first would shadow
    # this one.
    other_ra = factories.RemoteActorFactory(
        base_url="https://note-author.example", username="noteauthor", public_key="pk9"
    )
    note_object = factories.build_note_object(from_remote_actor=other_ra)
    respx_mock.get(note_object["id"]).mock(
        return_value=httpx.Response(200, json=note_object)
    )
    respx_mock.get(other_ra.ap_id).mock(
        return_value=httpx.Response(200, json=other_ra.ap_actor)
    )

    # When receiving an Announce of that Note
    announce_activity = factories.build_announce_activity(
        from_remote_actor=ra,
        announced_object_ap_id=note_object["id"],
    )
    ro = RemoteObject(announce_activity, ra)

    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=ro.ap_object,
        )
    assert response.status_code == 202

    # And when processing the incoming activity
    run_process_next_incoming_activity()

    # Then the announced Note was saved to the inbox with its conversation set
    note_from_inbox: activitypub.models.InboxObject | None = db.execute(
        select(activitypub.models.InboxObject).where(
            activitypub.models.InboxObject.ap_type == "Note"
        )
    ).scalar_one_or_none()
    assert note_from_inbox
    assert note_from_inbox.ap_id == note_object["id"]
    assert note_from_inbox.conversation == note_object["context"]


def test_inbox__create_already_deleted_object(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor
    ra = setup_remote_actor(respx_mock)

    # Who is also a follower
    follower = setup_remote_actor_as_follower(ra)

    # And a Create activity for a Note object
    create_activity = factories.build_create_activity(
        factories.build_note_object(
            from_remote_actor=ra,
            outbox_public_id=str(uuid4()),
            content="Hello",
            to=[LOCAL_ACTOR.ap_id],
        )
    )
    ro = RemoteObject(create_activity, ra)

    # And a Delete activity received for the create object
    setup_inbox_delete(follower.actor, ro.activity_object_ap_id)  # type: ignore

    # When receiving a Create activity
    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=ro.ap_object,
        )

    # Then the server returns a 202
    assert response.status_code == 202

    # And when processing the incoming activity
    run_process_next_incoming_activity()

    # Then the Create activity was saved
    create_activity_from_inbox: activitypub.models.InboxObject | None = db.execute(
        select(activitypub.models.InboxObject).where(
            activitypub.models.InboxObject.ap_type == "Create"
        )
    ).scalar_one_or_none()
    assert create_activity_from_inbox
    assert create_activity_from_inbox.ap_id == ro.ap_id
    # But it has the deleted flag
    assert create_activity_from_inbox.is_deleted is True

    # And the Note wasn't created
    assert (
        db.execute(
            select(activitypub.models.InboxObject).where(
                activitypub.models.InboxObject.ap_type == "Note"
            )
        ).scalar_one_or_none()
        is None
    )


def test_inbox__actor_is_blocked(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor
    ra = setup_remote_actor(respx_mock)

    # Who is also a follower
    follower = setup_remote_actor_as_follower(ra)
    follower.actor.is_blocked = True
    db.commit()

    create_activity = factories.build_create_activity(
        factories.build_note_object(
            from_remote_actor=ra,
            outbox_public_id=str(uuid4()),
            content="Hello",
            to=[LOCAL_ACTOR.ap_id],
        )
    )

    # When receiving a Create activity
    ro = RemoteObject(create_activity, ra)

    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=ro.ap_object,
        )

    # Then the server returns a 202
    assert response.status_code == 202

    # And when processing the incoming activity from a blocked actor
    run_process_next_incoming_activity()

    # Then the Create activity was discarded
    assert (
        db.scalar(
            select(func.count(activitypub.models.InboxObject.id)).where(
                activitypub.models.InboxObject.ap_type != "Follow"
            )
        )
        == 0
    )


def test_inbox__move_activity(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor
    ra = setup_remote_actor(respx_mock)

    # Which is followed by the local actor
    following = setup_remote_actor_as_following(ra)
    old_actor = following.actor
    assert old_actor
    assert following.outbox_object
    follow_id = following.outbox_object.ap_id

    # When receiving a Move activity
    new_ra = setup_remote_actor(
        respx_mock,
        base_url="https://new-account.com",
        also_known_as=[ra.ap_id],
    )
    move_activity = RemoteObject(
        factories.build_move_activity(ra, new_ra),
        ra,
    )

    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=move_activity.ap_object,
        )

    # Then the server returns a 202
    assert response.status_code == 202

    run_process_next_incoming_activity()

    # And the Move activity was saved in the inbox
    inbox_activity = db.execute(select(activitypub.models.InboxObject)).scalar_one()
    assert inbox_activity.ap_type == "Move"
    assert inbox_activity.actor_id == old_actor.id

    # And the following actor was deleted
    assert db.scalar(select(func.count(activitypub.models.Following.id))) == 0

    # And the follow was undone
    assert (
        db.scalar(
            select(func.count(activitypub.models.OutboxObject.id)).where(
                activitypub.models.OutboxObject.ap_type == "Undo",
                activitypub.models.OutboxObject.activity_object_ap_id == follow_id,
            )
        )
        == 1
    )

    # And the new account was followed
    assert (
        db.scalar(
            select(func.count(activitypub.models.OutboxObject.id)).where(
                activitypub.models.OutboxObject.ap_type == "Follow",
                activitypub.models.OutboxObject.activity_object_ap_id == new_ra.ap_id,
            )
        )
        == 1
    )

    # And a notification was created
    notif = db.execute(
        select(models.Notification).where(
            models.Notification.notification_type == models.NotificationType.MOVE
        )
    ).scalar_one()
    assert notif.actor.ap_id == new_ra.ap_id
    assert notif.inbox_object_id == inbox_activity.id


def test_inbox__block_activity(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor
    ra = setup_remote_actor(respx_mock)

    # Which is followed by the local actor
    setup_remote_actor_as_following(ra)

    # When receiving a Block activity
    follow_activity = RemoteObject(
        factories.build_block_activity(
            from_remote_actor=ra,
            for_remote_actor=LOCAL_ACTOR,
        ),
        ra,
    )
    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=follow_activity.ap_object,
        )

    # Then the server returns a 202
    assert response.status_code == 202

    run_process_next_incoming_activity()

    # And the actor was saved in DB
    saved_actor = db.execute(select(activitypub.models.Actor)).scalar_one()
    assert saved_actor.ap_id == ra.ap_id

    # And the Block activity was saved in the inbox
    inbox_activity = db.execute(
        select(activitypub.models.InboxObject).where(
            activitypub.models.InboxObject.ap_type == "Block"
        )
    ).scalar_one()

    # And a notification was created
    notif = db.execute(
        select(models.Notification).where(
            models.Notification.notification_type == models.NotificationType.BLOCKED
        )
    ).scalar_one()
    assert notif.actor.ap_id == ra.ap_id
    assert notif.inbox_object_id == inbox_activity.id


def _mock_transient_activity_id():
    """An activity without an `id` -- a Mastodon report -- is queued under a
    JSON-LD hash of the payload, which makes pyld dereference its `@context`
    over the network. The hash is not what these tests are about, so stub it out
    and keep them offline.
    """
    return mock.patch(
        "app.ldsig.doc_hash_async",
        mock.AsyncMock(return_value="flag-doc-hash"),
    )


def test_inbox__flag_activity(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor
    ra = setup_remote_actor(respx_mock)

    # And a local post
    outbox_object = setup_outbox_note()

    # When receiving a report about the local actor and that post
    flag_activity = factories.build_flag_activity(
        from_remote_actor=ra,
        reported_ap_ids=[LOCAL_ACTOR.ap_id, outbox_object.ap_id],
        content="this is spam",
    )
    with mock_httpsig_checker(ra), _mock_transient_activity_id():
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=flag_activity,
        )

    # Then the server returns a 202
    assert response.status_code == 202

    run_process_next_incoming_activity()

    # And the Flag activity was saved in the inbox, with a synthetic ID as
    # Mastodon sends reports without one
    inbox_activity = db.execute(
        select(activitypub.models.InboxObject).where(
            activitypub.models.InboxObject.ap_type == "Flag"
        )
    ).scalar_one()
    assert inbox_activity.ap_id.startswith(ra.ap_id + "#flag/")
    assert inbox_activity.content == "this is spam"

    # And a notification was created, pointing at the reported post
    notif = db.execute(
        select(models.Notification).where(
            models.Notification.notification_type == models.NotificationType.REPORTED
        )
    ).scalar_one()
    assert notif.actor.ap_id == ra.ap_id
    assert notif.inbox_object_id == inbox_activity.id
    assert notif.outbox_object_id == outbox_object.id


def test_inbox__flag_activity_about_foreign_objects_is_dropped(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor
    ra = setup_remote_actor(respx_mock)

    # When receiving a report that is not about anything local
    flag_activity = factories.build_flag_activity(
        from_remote_actor=ra,
        reported_ap_ids=["https://other.example/users/someone"],
    )
    with mock_httpsig_checker(ra), _mock_transient_activity_id():
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=flag_activity,
        )

    assert response.status_code == 202

    run_process_next_incoming_activity()

    # Then it was dropped, and no notification was created
    assert (
        db.scalar(
            select(func.count(activitypub.models.InboxObject.id)).where(
                activitypub.models.InboxObject.ap_type == "Flag"
            )
        )
        == 0
    )
    assert db.scalar(select(func.count(models.Notification.id))) == 0


def test_inbox_quote_request_auto_accepted_for_public_post(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor and one of our public posts
    ra = setup_remote_actor(respx_mock)
    outbox_object = setup_outbox_note()

    quoting_ap_id = ra.ap_id + "/note/quoting"

    # When receiving a QuoteRequest for it (default `quote_policy`: "public")
    quote_request_activity = factories.build_quote_request_activity(
        from_remote_actor=ra,
        quoted_ap_id=outbox_object.ap_id,
        instrument_ap_id=quoting_ap_id,
    )
    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=quote_request_activity,
        )
    assert response.status_code == 202

    run_process_next_incoming_activity()

    # Then the QuoteRequest was saved
    inbox_activity = db.execute(
        select(activitypub.models.InboxObject).where(
            activitypub.models.InboxObject.ap_type == "QuoteRequest"
        )
    ).scalar_one()
    assert inbox_activity.actor.ap_id == ra.ap_id

    # And a stamp (QuoteAuthorization) was minted, referencing the request
    stamp = db.execute(
        select(activitypub.models.OutboxObject).where(
            activitypub.models.OutboxObject.ap_type == "QuoteAuthorization"
        )
    ).scalar_one()
    assert stamp.ap_object["interactingObject"] == quoting_ap_id
    assert stamp.ap_object["interactionTarget"] == outbox_object.ap_id
    assert stamp.relates_to_inbox_object_id == inbox_activity.id
    assert stamp.is_hidden_from_homepage is True

    # And an Accept carrying the stamp as `result` was sent back to the requester
    accept = db.execute(
        select(activitypub.models.OutboxObject).where(
            activitypub.models.OutboxObject.ap_type == "Accept"
        )
    ).scalar_one()
    assert accept.ap_object["object"] == inbox_activity.ap_id
    assert accept.ap_object["result"] == stamp.ap_id

    outgoing = db.execute(select(activitypub.models.OutgoingActivity)).scalar_one()
    assert outgoing.outbox_object_id == accept.id
    assert outgoing.recipient == ra.inbox_url


def test_inbox_quote_request_for_unknown_object_is_dropped(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor
    ra = setup_remote_actor(respx_mock)

    # When receiving a QuoteRequest about an object that isn't ours
    quote_request_activity = factories.build_quote_request_activity(
        from_remote_actor=ra,
        quoted_ap_id="https://other.example/users/someone/notes/1",
        instrument_ap_id=ra.ap_id + "/note/quoting",
    )
    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=quote_request_activity,
        )
    assert response.status_code == 202

    run_process_next_incoming_activity()

    # Then it was dropped, and nothing was sent back
    assert (
        db.scalar(
            select(func.count(activitypub.models.InboxObject.id)).where(
                activitypub.models.InboxObject.ap_type == "QuoteRequest"
            )
        )
        == 0
    )
    assert db.scalar(select(func.count(activitypub.models.OutboxObject.id))) == 0


def _setup_pending_quote(
    ra,
) -> tuple[
    activitypub.models.OutboxObject,
    activitypub.models.OutboxObject,
    RemoteObject,
]:
    """A quote post of one of `ra`'s notes, with its `QuoteRequest` already
    sent and still pending -- the state `_handle_quote_request_accept_or_reject`
    expects to find when the Accept/Reject comes back.

    The quoted note is saved to the inbox (as it would be from the
    fetch-then-reload done when the quote was first composed): the
    accept/reject handler resolves the quoted post's actor from it, to check
    that whoever sent the Accept/Reject is actually that actor.
    """
    actor_in_db = factories.ActorFactory.from_remote_actor(ra)
    quoted_note_from_inbox = RemoteObject(
        factories.build_note_object(
            from_remote_actor=ra,
            outbox_public_id="quoted",
            content="Original post",
        ),
        ra,
    )
    factories.InboxObjectFactory.from_remote_object(quoted_note_from_inbox, actor_in_db)
    quoted_post_ap_id = quoted_note_from_inbox.ap_id

    note_id = uuid4().hex
    note_from_outbox = RemoteObject(
        factories.build_note_object(
            from_remote_actor=LOCAL_ACTOR,
            outbox_public_id=note_id,
            content="RE: ...",
            quote=quoted_post_ap_id,
        ),
        LOCAL_ACTOR,
    )
    quote_outbox_object = factories.OutboxObjectFactory.from_remote_object(
        note_id, note_from_outbox
    )
    quote_outbox_object.quote_ap_id = quoted_post_ap_id
    quote_outbox_object.quote_state = "pending"
    # As `send_create` would leave it: the Accept path re-renders the note from
    # its source to publish the authorization, and refuses to run without one.
    quote_outbox_object.source = "RE: ..."

    quote_request_id = uuid4().hex
    quote_request_from_outbox = RemoteObject(
        factories.build_quote_request_activity(
            from_remote_actor=LOCAL_ACTOR,
            quoted_ap_id=quoted_post_ap_id,
            instrument_ap_id=quote_outbox_object.ap_id,
            outbox_public_id=quote_request_id,
        ),
        LOCAL_ACTOR,
    )
    quote_request_outbox_object = factories.OutboxObjectFactory.from_remote_object(
        quote_request_id, quote_request_from_outbox
    )
    quote_request_outbox_object.relates_to_outbox_object_id = quote_outbox_object.id

    return quote_outbox_object, quote_request_outbox_object, quote_request_from_outbox


def test_inbox_accept_of_our_quote_request_stores_authorization(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor and our pending quote of one of their posts. The
    # stamp mock is registered *before* the actor's own path-less mock below
    # (see the note in test_inbox__announce_of_unknown_object_sets_conversation):
    # respx matches routes in registration order, and a path-less URL for
    # `ra.ap_id` matches any path under that host, including the stamp's.
    ra = factories.RemoteActorFactory(
        base_url="https://example.com", username="toto", public_key="pk"
    )
    quote_outbox_object, _, quote_request_from_outbox = _setup_pending_quote(ra)
    db.commit()
    assert quote_outbox_object.quote_ap_id

    # And the stamp they'll present, fetchable from their instance
    stamp_ap_id = ra.ap_id + "/quote_auth/" + uuid4().hex
    stamp = factories.build_quote_authorization(
        from_remote_actor=ra,
        quoting_object_ap_id=quote_outbox_object.ap_id,
        quoted_object_ap_id=quote_outbox_object.quote_ap_id,
    )
    stamp["id"] = stamp_ap_id
    respx_mock.get(stamp_ap_id).mock(return_value=httpx.Response(200, json=stamp))
    respx_mock.get(ra.ap_id).mock(return_value=httpx.Response(200, json=ra.ap_actor))

    # When receiving an Accept for our QuoteRequest, carrying the stamp
    accept_activity = RemoteObject(
        factories.build_accept_activity(
            from_remote_actor=ra,
            for_remote_object=quote_request_from_outbox,
        ),
        ra,
    )
    accept_activity.ap_object["result"] = stamp_ap_id

    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=accept_activity.ap_object,
        )
    assert response.status_code == 202

    run_process_next_incoming_activity()

    # Then the quote post is now authorized
    db.refresh(quote_outbox_object)
    assert quote_outbox_object.quote_state == "accepted"
    assert quote_outbox_object.quote_authorization_ap_id == stamp_ap_id
    assert quote_outbox_object.ap_object["quoteAuthorization"] == stamp_ap_id


def test_inbox_reject_of_our_quote_request_marks_it_rejected(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor and our pending quote of one of their posts
    ra = setup_remote_actor(respx_mock)
    quote_outbox_object, _, quote_request_from_outbox = _setup_pending_quote(ra)
    db.commit()

    reject_activity = {
        "@context": ap.AS_CTX,
        "type": "Reject",
        "id": ra.ap_id + "/reject/" + uuid4().hex,
        "actor": ra.ap_id,
        "object": quote_request_from_outbox.ap_id,
    }

    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=reject_activity,
        )
    assert response.status_code == 202

    run_process_next_incoming_activity()

    db.refresh(quote_outbox_object)
    assert quote_outbox_object.quote_state == "rejected"
    assert quote_outbox_object.quote_authorization_ap_id is None


def test_inbox_accept_of_our_quote_request_with_invalid_stamp_is_rejected(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor and our pending quote of one of their posts
    ra = setup_remote_actor(respx_mock)
    quote_outbox_object, _, quote_request_from_outbox = _setup_pending_quote(ra)
    db.commit()
    assert quote_outbox_object.quote_ap_id

    # And a stamp attributed to a different actor entirely
    impostor = factories.RemoteActorFactory(
        base_url="https://impostor.example", username="impostor", public_key="pk2"
    )
    stamp_ap_id = impostor.ap_id + "/quote_auth/" + uuid4().hex
    stamp = factories.build_quote_authorization(
        from_remote_actor=impostor,
        quoting_object_ap_id=quote_outbox_object.ap_id,
        quoted_object_ap_id=quote_outbox_object.quote_ap_id,
    )
    stamp["id"] = stamp_ap_id
    respx_mock.get(stamp_ap_id).mock(return_value=httpx.Response(200, json=stamp))

    accept_activity = RemoteObject(
        factories.build_accept_activity(
            from_remote_actor=ra,
            for_remote_object=quote_request_from_outbox,
        ),
        ra,
    )
    accept_activity.ap_object["result"] = stamp_ap_id

    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=accept_activity.ap_object,
        )
    assert response.status_code == 202

    run_process_next_incoming_activity()

    db.refresh(quote_outbox_object)
    assert quote_outbox_object.quote_state == "rejected"
    assert quote_outbox_object.quote_authorization_ap_id is None


def test_inbox_accept_of_our_quote_request_from_spoofed_actor_is_ignored(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor and our pending quote of one of their posts
    ra = setup_remote_actor(respx_mock)
    quote_outbox_object, _, quote_request_from_outbox = _setup_pending_quote(ra)
    db.commit()
    assert quote_outbox_object.quote_ap_id

    # And a third party who is *not* the quoted post's author, presenting a
    # stamp they minted for themselves -- which would satisfy every check in
    # `_verify_quote_authorization` on its own, since that only checks the
    # stamp against whichever actor it's told is "the quoted actor".
    impostor = setup_remote_actor(respx_mock, base_url="https://impostor.example")
    stamp_ap_id = impostor.ap_id + "/quote_auth/" + uuid4().hex
    stamp = factories.build_quote_authorization(
        from_remote_actor=impostor,
        quoting_object_ap_id=quote_outbox_object.ap_id,
        quoted_object_ap_id=quote_outbox_object.quote_ap_id,
    )
    stamp["id"] = stamp_ap_id
    respx_mock.get(stamp_ap_id).mock(return_value=httpx.Response(200, json=stamp))

    # When that third party sends an Accept for our QuoteRequest
    accept_activity = RemoteObject(
        factories.build_accept_activity(
            from_remote_actor=impostor,
            for_remote_object=quote_request_from_outbox,
        ),
        impostor,
    )
    accept_activity.ap_object["result"] = stamp_ap_id

    with mock_httpsig_checker(impostor):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=accept_activity.ap_object,
        )
    assert response.status_code == 202

    run_process_next_incoming_activity()

    # Then it's ignored outright: not accepted, and not even flipped to
    # rejected -- the real quoted actor's own Accept/Reject can still land.
    db.refresh(quote_outbox_object)
    assert quote_outbox_object.quote_state == "pending"
    assert quote_outbox_object.quote_authorization_ap_id is None


def test_inbox_create_with_verified_quote_bumps_quotes_count(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor and one of our public posts
    ra = setup_remote_actor(respx_mock)
    quoted_object = setup_outbox_note()

    quoting_ap_id = ra.ap_id + "/note/quoting"

    # And a stamp we minted (attributedTo us, the quoted post's author)
    stamp = factories.OutboxObjectFactory.from_remote_object(
        uuid4().hex,
        RemoteObject(
            factories.build_quote_authorization(
                from_remote_actor=LOCAL_ACTOR,
                quoting_object_ap_id=quoting_ap_id,
                quoted_object_ap_id=quoted_object.ap_id,
            ),
            LOCAL_ACTOR,
        ),
    )
    db.commit()

    # When receiving the quote Create, presenting that stamp
    create_activity = factories.build_create_activity(
        factories.build_note_object(
            from_remote_actor=ra,
            outbox_public_id=quoting_ap_id.rsplit("/", 1)[-1],
            content="RE: ...",
            quote=quoted_object.ap_id,
            quote_authorization=stamp.ap_id,
        )
    )
    ro = RemoteObject(create_activity, ra)
    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=ro.ap_object,
        )
    assert response.status_code == 202

    run_process_next_incoming_activity()

    # Then the quote is stored as verified
    note_from_inbox = db.execute(
        select(activitypub.models.InboxObject).where(
            activitypub.models.InboxObject.ap_type == "Note"
        )
    ).scalar_one()
    assert note_from_inbox.quote_ap_id == quoted_object.ap_id
    assert note_from_inbox.quote_is_verified is True

    # And the quoted post's counter and notifications reflect it
    db.refresh(quoted_object)
    assert quoted_object.quotes_count == 1

    notif = db.execute(
        select(models.Notification).where(
            models.Notification.notification_type == models.NotificationType.QUOTE
        )
    ).scalar_one()
    assert notif.inbox_object_id == note_from_inbox.id
    assert notif.outbox_object_id == quoted_object.id


def test_inbox_create_with_legacy_alias_quote_is_unverified(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor and one of our public posts
    ra = setup_remote_actor(respx_mock)
    quoted_object = setup_outbox_note()

    # When receiving a Create carrying only the legacy `quoteUrl` alias
    create_activity = factories.build_create_activity(
        factories.build_note_object(
            from_remote_actor=ra,
            content="RE: ...",
            legacy_quote_alias=quoted_object.ap_id,
        )
    )
    ro = RemoteObject(create_activity, ra)
    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=ro.ap_object,
        )
    assert response.status_code == 202

    run_process_next_incoming_activity()

    # Then it's stored, but unverified -- and doesn't bump the counter
    note_from_inbox = db.execute(
        select(activitypub.models.InboxObject).where(
            activitypub.models.InboxObject.ap_type == "Note"
        )
    ).scalar_one()
    assert note_from_inbox.quote_ap_id == quoted_object.ap_id
    assert note_from_inbox.quote_is_verified is False

    db.refresh(quoted_object)
    assert quoted_object.quotes_count == 0


def test_inbox_create_with_legacy_alias_quote_does_not_fetch_the_target(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a remote actor quoting, via the legacy alias only, a remote post
    # we have never seen
    ra = setup_remote_actor(respx_mock)
    quoted_ap_id = "https://quoted.example/users/bob/note/1"

    create_activity = factories.build_create_activity(
        factories.build_note_object(
            from_remote_actor=ra,
            content="RE: ...",
            legacy_quote_alias=quoted_ap_id,
        )
    )
    ro = RemoteObject(create_activity, ra)
    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=ro.ap_object,
        )
    assert response.status_code == 202

    with mock.patch("activitypub.boxes.ap.fetch", side_effect=ap.fetch) as mocked_fetch:
        run_process_next_incoming_activity()

    # Then the quote is stored, unverified
    note_from_inbox = db.execute(
        select(activitypub.models.InboxObject).where(
            activitypub.models.InboxObject.ap_type == "Note"
        )
    ).scalar_one()
    assert note_from_inbox.quote_ap_id == quoted_ap_id
    assert note_from_inbox.quote_is_verified is False

    # And the target was never dereferenced: with no stamp to verify against
    # it, an unverified quote is never rendered, so the fetch (which happens
    # with the inbox write transaction open) would buy nothing
    assert quoted_ap_id not in {
        call.args[0] for call in mocked_fetch.call_args_list if call.args
    }
    assert (
        db.execute(
            select(func.count(activitypub.models.InboxObject.id)).where(
                activitypub.models.InboxObject.ap_id == quoted_ap_id
            )
        ).scalar_one()
        == 0
    )


def test_inbox_delete_of_a_verified_quote_updates_quotes_count(
    db: Session,
    client: TestClient,
    respx_mock: respx.MockRouter,
) -> None:
    # Given a verified remote quote of one of our posts
    ra = setup_remote_actor(respx_mock)
    quoted_object = setup_outbox_note()
    quoting_ap_id = ra.ap_id + "/note/quoting"

    factories.OutboxObjectFactory.from_remote_object(
        uuid4().hex,
        RemoteObject(
            factories.build_quote_authorization(
                from_remote_actor=LOCAL_ACTOR,
                quoting_object_ap_id=quoting_ap_id,
                quoted_object_ap_id=quoted_object.ap_id,
            ),
            LOCAL_ACTOR,
        ),
    )
    db.commit()

    stamp_ap_id = db.execute(
        select(activitypub.models.OutboxObject.ap_id).where(
            activitypub.models.OutboxObject.ap_type == "QuoteAuthorization"
        )
    ).scalar_one()

    create_activity = factories.build_create_activity(
        factories.build_note_object(
            from_remote_actor=ra,
            outbox_public_id="quoting",
            content="RE: ...",
            quote=quoted_object.ap_id,
            quote_authorization=stamp_ap_id,
        )
    )
    with mock_httpsig_checker(ra):
        client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=RemoteObject(create_activity, ra).ap_object,
        )
    run_process_next_incoming_activity()

    db.refresh(quoted_object)
    assert quoted_object.quotes_count == 1

    # When the quoting post is deleted
    delete_activity = factories.build_delete_activity(
        from_remote_actor=ra,
        deleted_object_ap_id=quoting_ap_id,
    )
    with mock_httpsig_checker(ra):
        response = client.post(
            "/inbox",
            headers={"Content-Type": ap.AS_CTX},
            json=RemoteObject(delete_activity, ra).ap_object,
        )
    assert response.status_code == 202

    run_process_next_incoming_activity()

    # Then the counter is recomputed rather than left drifting
    db.refresh(quoted_object)
    assert quoted_object.quotes_count == 0
