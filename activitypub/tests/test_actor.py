import httpx
import pytest
import respx
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

import activitypub.models
from activitypub.actor import fetch_actor
from activitypub.actor import refresh_actor_counts
from activitypub.actor import save_actor
from activitypub.tests import factories


@pytest.mark.asyncio
async def test_fetch_actor(async_db_session: AsyncSession, respx_mock) -> None:
    # Given a remote actor
    ra = factories.RemoteActorFactory(
        base_url="https://example.com",
        username="toto",
        public_key="pk",
    )
    respx_mock.get(ra.ap_id).mock(return_value=httpx.Response(200, json=ra.ap_actor))
    respx_mock.get(
        "https://example.com/.well-known/webfinger",
        params={"resource": "acct%3Atoto%40example.com"},
    ).mock(return_value=httpx.Response(200, json={"subject": "acct:toto@example.com"}))

    # When fetching this actor for the first time
    saved_actor = await fetch_actor(async_db_session, ra.ap_id)

    # Then it has been fetched and saved in DB
    assert respx.calls.call_count == 2
    assert (
        await async_db_session.execute(select(activitypub.models.Actor))
    ).scalar_one().ap_id == saved_actor.ap_id

    # When fetching it a second time
    actor_from_db = await fetch_actor(async_db_session, ra.ap_id)

    # Then it's read from the DB
    assert actor_from_db.ap_id == ra.ap_id
    assert (
        await async_db_session.execute(select(func.count(activitypub.models.Actor.id)))
    ).scalar_one() == 1
    assert respx.calls.call_count == 2
    await async_db_session.close()


@pytest.mark.asyncio
async def test_save_actor_recovers_from_concurrent_insert_race(
    async_db_session: AsyncSession,
) -> None:
    # Regression test: two concurrent requests can both see "actor not in DB
    # yet" for the same not-yet-cached actor (e.g. two clients backfilling
    # the same first-time-seen account's outbox at once), so the loser's
    # INSERT hits actor.ap_id's unique constraint. save_actor should recover
    # by returning the winner's row instead of surfacing the IntegrityError.
    ra = factories.RemoteActorFactory(
        base_url="https://example.com",
        username="toto",
        public_key="pk",
    )

    winner = await save_actor(async_db_session, ra.ap_actor)

    loser = await save_actor(async_db_session, ra.ap_actor)

    assert loser.id == winner.id
    assert (
        await async_db_session.execute(select(func.count(activitypub.models.Actor.id)))
    ).scalar_one() == 1


@pytest.mark.asyncio
async def test_refresh_actor_counts(respx_mock) -> None:
    ra = factories.RemoteActorFactory(
        base_url="https://example.com",
        username="toto",
        public_key="pk",
    )
    actor_in_db = activitypub.models.Actor(
        ap_id=ra.ap_id,
        ap_actor=ra.ap_actor,
        ap_type=ra.ap_type,
        handle="@toto@example.com",
    )

    respx_mock.get(f"{ra.ap_id}/followers").mock(
        return_value=httpx.Response(
            200, json={"type": "OrderedCollection", "totalItems": 12}
        )
    )
    respx_mock.get(f"{ra.ap_id}/following").mock(
        return_value=httpx.Response(
            200, json={"type": "OrderedCollection", "totalItems": 34}
        )
    )
    respx_mock.get(f"{ra.ap_id}/outbox").mock(
        return_value=httpx.Response(
            200, json={"type": "OrderedCollection", "totalItems": 56}
        )
    )

    await refresh_actor_counts(actor_in_db)

    assert actor_in_db.followers_count == 12
    assert actor_in_db.following_count == 34
    assert actor_in_db.statuses_count == 56
    assert actor_in_db.counts_refreshed_at is not None


@pytest.mark.asyncio
async def test_refresh_actor_counts_tolerates_fetch_failure(respx_mock) -> None:
    # Regression: some remote actors 404/403 their followers/following
    # collections, or omit totalItems entirely. A failure on one collection
    # must not prevent the others from being cached, nor crash the caller.
    ra = factories.RemoteActorFactory(
        base_url="https://example.com",
        username="toto",
        public_key="pk",
    )
    actor_in_db = activitypub.models.Actor(
        ap_id=ra.ap_id,
        ap_actor=ra.ap_actor,
        ap_type=ra.ap_type,
        handle="@toto@example.com",
    )

    respx_mock.get(f"{ra.ap_id}/followers").mock(return_value=httpx.Response(404))
    respx_mock.get(f"{ra.ap_id}/following").mock(
        return_value=httpx.Response(200, json={"type": "OrderedCollection"})
    )
    respx_mock.get(f"{ra.ap_id}/outbox").mock(
        return_value=httpx.Response(
            200, json={"type": "OrderedCollection", "totalItems": 56}
        )
    )

    await refresh_actor_counts(actor_in_db)

    assert actor_in_db.followers_count is None
    assert actor_in_db.following_count is None
    assert actor_in_db.statuses_count == 56
    assert actor_in_db.counts_refreshed_at is not None


def test_sqlalchemy_factory(db: Session) -> None:
    ra = factories.RemoteActorFactory(
        base_url="https://example.com",
        username="toto",
        public_key="pk",
    )
    actor_in_db = factories.ActorFactory(
        ap_type=ra.ap_type,
        ap_actor=ra.ap_actor,
        ap_id=ra.ap_id,
    )
    assert (
        actor_in_db.id == db.execute(select(activitypub.models.Actor)).scalar_one().id
    )
    db.close()
