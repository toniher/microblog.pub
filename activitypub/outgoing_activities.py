import asyncio
import dataclasses
import traceback
from datetime import datetime
from datetime import timedelta
from typing import MutableMapping
from urllib.parse import urlparse

import httpx
from cachetools import TTLCache
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import joinedload

import activitypub.models
from activitypub import activitypub as ap
from activitypub.actor import LOCAL_ACTOR
from activitypub.actor import _actor_hash
from app import config
from app import http_client
from app import ldsig
from app.config import KEY_PATH
from app.database import AsyncSession
from app.key import Key
from app.utils.datetime import now
from app.utils.datetime import parse_retry_after
from app.utils.url import check_url_async
from app.utils.workers import Worker

_MAX_RETRIES = 16

_LD_SIG_CACHE: MutableMapping[str, ap.RawObject] = TTLCache(
    maxsize=config.OUTGOING_DELIVERY_BATCH_SIZE, ttl=60 * 5
)


k = Key(config.ID, f"{config.ID}#main-key")
k.load(KEY_PATH.read_text())


def _is_local_actor_updated() -> bool:
    """Returns True if the local actor was updated, i.e. updated via the config file"""
    actor_hash = _actor_hash(LOCAL_ACTOR)
    actor_hash_cache = config.ROOT_DIR / "data" / "local_actor_hash.dat"

    if not actor_hash_cache.exists():
        logger.info("Initializing local actor hash cache")
        actor_hash_cache.write_bytes(actor_hash)
        return False

    previous_actor_hash = actor_hash_cache.read_bytes()
    if previous_actor_hash == actor_hash:
        logger.info("Local actor hasn't been updated")
        return False

    actor_hash_cache.write_bytes(actor_hash)
    logger.info("Local actor has been updated")
    return True


async def _send_actor_update_if_needed(
    db_session: AsyncSession,
) -> None:
    """The process for sending an update for the local actor is done here as
    in production, we may have multiple uvicorn worker and this worker will
    always run in a single process."""
    if not _is_local_actor_updated():
        return

    logger.info("Will send an Update for the local actor")

    from activitypub.boxes import allocate_outbox_id
    from activitypub.boxes import compute_all_known_recipients
    from activitypub.boxes import outbox_object_id
    from activitypub.boxes import save_outbox_object

    update_activity_id = allocate_outbox_id()
    update_activity = {
        "@context": ap.AS_EXTENDED_CTX,
        "id": outbox_object_id(update_activity_id),
        "type": "Update",
        "to": [ap.AS_PUBLIC],
        "actor": config.ID,
        "object": ap.remove_context(LOCAL_ACTOR.ap_actor),
    }
    outbox_object = await save_outbox_object(
        db_session, update_activity_id, update_activity
    )

    # Send the update to the followers collection and all the actor we have ever
    # contacted
    recipients = await compute_all_known_recipients(db_session)
    for rcp in recipients:
        await new_outgoing_activity(
            db_session,
            recipient=rcp,
            outbox_object_id=outbox_object.id,
        )

    await db_session.commit()


async def new_outgoing_activity(
    db_session: AsyncSession,
    recipient: str,
    outbox_object_id: int | None = None,
    inbox_object_id: int | None = None,
    webmention_target: str | None = None,
) -> activitypub.models.OutgoingActivity:
    if outbox_object_id is None and inbox_object_id is None:
        raise ValueError("Must reference at least one inbox/outbox activity")
    if webmention_target and outbox_object_id is None:
        raise ValueError("Webmentions must reference an outbox activity")
    if outbox_object_id and inbox_object_id:
        raise ValueError("Cannot reference both inbox/outbox activities")

    outgoing_activity = activitypub.models.OutgoingActivity(
        recipient=recipient,
        outbox_object_id=outbox_object_id,
        inbox_object_id=inbox_object_id,
        webmention_target=webmention_target,
    )

    db_session.add(outgoing_activity)
    await db_session.flush()
    await db_session.refresh(outgoing_activity)
    return outgoing_activity


def _exp_backoff(tries: int) -> datetime:
    seconds = 2 * (2 ** (tries - 1))
    return now() + timedelta(seconds=seconds)


def _set_next_try(
    outgoing_activity: activitypub.models.OutgoingActivity,
    next_try: datetime | None = None,
) -> None:
    if not outgoing_activity.tries:
        raise ValueError("Should never happen")

    if outgoing_activity.tries >= _MAX_RETRIES:
        outgoing_activity.is_errored = True
        outgoing_activity.next_try = None
    else:
        outgoing_activity.next_try = next_try or _exp_backoff(outgoing_activity.tries)


@dataclasses.dataclass(frozen=True)
class _DeliveryRequest:
    activity_id: int
    recipient: str
    host: str
    webmention_payload: dict[str, str | None] | None
    ap_payload: ap.RawObject | None


@dataclasses.dataclass(frozen=True)
class _DeliveryOutcome:
    activity_id: int
    response: httpx.Response | None = None
    exception: Exception | None = None
    formatted_traceback: str | None = None
    retry_after: datetime | None = None
    skip_reason: str | None = None


async def fetch_next_outgoing_activities(
    db_session: AsyncSession,
    limit: int,
) -> list[activitypub.models.OutgoingActivity]:
    where = [
        activitypub.models.OutgoingActivity.next_try <= now(),
        activitypub.models.OutgoingActivity.is_errored.is_(False),
        activitypub.models.OutgoingActivity.is_sent.is_(False),
    ]
    return list(
        (
            await db_session.execute(
                select(activitypub.models.OutgoingActivity)
                .where(*where)
                .limit(limit)
                .options(
                    joinedload(activitypub.models.OutgoingActivity.inbox_object),
                    joinedload(activitypub.models.OutgoingActivity.outbox_object),
                )
                .order_by(
                    activitypub.models.OutgoingActivity.next_try,
                    activitypub.models.OutgoingActivity.id,
                )
            )
        )
        .scalars()
        .all()
    )


async def fetch_next_outgoing_activity(
    db_session: AsyncSession,
) -> activitypub.models.OutgoingActivity | None:
    activities = await fetch_next_outgoing_activities(db_session, 1)
    return activities[0] if activities else None


def _build_delivery_request(
    next_activity: activitypub.models.OutgoingActivity,
) -> _DeliveryRequest:
    if next_activity.id is None or next_activity.recipient is None:
        raise ValueError("Should never happen")

    activity_id: int = next_activity.id
    recipient: str = next_activity.recipient
    host: str = urlparse(recipient).netloc

    if next_activity.webmention_target and next_activity.outbox_object:
        webmention_payload = {
            "source": next_activity.outbox_object.url,
            "target": next_activity.webmention_target,
        }
        logger.info(f"{webmention_payload=}")
        return _DeliveryRequest(
            activity_id=activity_id,
            recipient=recipient,
            host=host,
            webmention_payload=webmention_payload,
            ap_payload=None,
        )

    payload = ap.wrap_object_if_needed(next_activity.anybox_object.ap_object)
    # `wrap_object_if_needed` returns the ORM's dict by identity for
    # Update/Delete, and the same OutboxObject may be shared across a whole
    # recipient fan-out via the session identity map — copy before mutating.
    payload = dict(payload)

    # Use LD sig if the activity may need to be forwarded by recipients
    if next_activity.anybox_object.is_from_outbox and payload["type"] in [
        "Create",
        "Update",
        "Delete",
    ]:
        # But only if the object is public (to help with deniability/privacy)
        if next_activity.outbox_object.visibility == ap.VisibilityEnum.PUBLIC:  # type: ignore  # noqa: E501
            if p := _LD_SIG_CACHE.get(payload["id"]):
                payload = p
            else:
                ldsig.generate_signature(payload, k)
                _LD_SIG_CACHE[payload["id"]] = payload

    logger.info(f"{payload=}")

    return _DeliveryRequest(
        activity_id=activity_id,
        recipient=recipient,
        host=host,
        webmention_payload=None,
        ap_payload=payload,
    )


async def _deliver(req: _DeliveryRequest) -> _DeliveryOutcome:
    try:
        await check_url_async(req.recipient)
        if req.webmention_payload is not None:
            resp = await http_client.get_client().post(
                req.recipient,
                data=req.webmention_payload,
                headers={
                    "User-Agent": config.USER_AGENT,
                },
            )
            resp.raise_for_status()
        else:
            if req.ap_payload is None:
                raise ValueError("Should never happen")
            resp = await ap.post(req.recipient, req.ap_payload)
    except httpx.HTTPStatusError as http_error:
        logger.exception("Failed")
        retry_after: datetime | None = None
        if http_error.response.status_code in (429, 503):
            if retry_after_value := http_error.response.headers.get("Retry-After"):
                retry_after = parse_retry_after(retry_after_value)
        return _DeliveryOutcome(
            activity_id=req.activity_id,
            exception=http_error,
            formatted_traceback=traceback.format_exc(),
            retry_after=retry_after,
        )
    except Exception as exc:
        logger.exception("Failed")
        return _DeliveryOutcome(
            activity_id=req.activity_id,
            exception=exc,
            formatted_traceback=traceback.format_exc(),
        )
    else:
        logger.info("Success")
        return _DeliveryOutcome(activity_id=req.activity_id, response=resp)


def _is_host_level_failure(exc: Exception) -> bool:
    """A failure that other requests to the same recipient would also hit.

    Short-circuiting the rest of a same-recipient group on these saves N-1
    pointless attempts against a throttled/unreachable host, without
    touching activity-specific permanent failures (4xx other than 429).
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 503)
    return isinstance(exc, httpx.TransportError)


async def _deliver_group(
    group: list[_DeliveryRequest],
    global_sem: asyncio.Semaphore,
    per_host_sems: dict[str, asyncio.Semaphore],
) -> list[_DeliveryOutcome]:
    """Deliver requests for a single recipient, strictly in order.

    Two activities queued for the same inbox must arrive in order (an
    `Undo(Follow)` racing ahead of `Follow`, or a `Delete` racing ahead of a
    `Create`, would leave the wrong state on the remote permanently) — so a
    group is never itself parallelized, only run concurrently with other
    groups.
    """
    outcomes: list[_DeliveryOutcome] = []
    skip_outcome: _DeliveryOutcome | None = None

    for req in group:
        if skip_outcome is not None:
            # Still consumes the attempt (tries was already incremented in
            # the prepare phase) so `_MAX_RETRIES` accounting isn't skewed.
            outcomes.append(
                dataclasses.replace(skip_outcome, activity_id=req.activity_id)
            )
            continue

        async with global_sem, per_host_sems[req.host]:
            outcome = await _deliver(req)
        outcomes.append(outcome)

        if outcome.exception is not None and _is_host_level_failure(outcome.exception):
            skip_outcome = _DeliveryOutcome(
                activity_id=req.activity_id,
                skip_reason=f"skipped: {req.host} is unavailable",
                retry_after=outcome.retry_after,
            )

    return outcomes


async def _deliver_batch(
    delivery_requests: list[_DeliveryRequest],
) -> list[_DeliveryOutcome]:
    if not delivery_requests:
        return []

    groups: dict[str, list[_DeliveryRequest]] = {}
    for req in delivery_requests:
        groups.setdefault(req.recipient, []).append(req)

    # Built fresh per batch: a module-level `asyncio.Semaphore` caches the
    # event loop it was created on, which breaks across pytest-asyncio's
    # per-test event loops.
    global_sem = asyncio.Semaphore(config.OUTGOING_DELIVERY_CONCURRENCY)
    per_host_sems: dict[str, asyncio.Semaphore] = {}
    for req in delivery_requests:
        if req.host not in per_host_sems:
            per_host_sems[req.host] = asyncio.Semaphore(
                config.OUTGOING_DELIVERY_PER_HOST_CONCURRENCY
            )

    grouped_outcomes = await asyncio.gather(
        *(_deliver_group(group, global_sem, per_host_sems) for group in groups.values())
    )
    return [
        outcome for group_outcomes in grouped_outcomes for outcome in group_outcomes
    ]


def _apply_delivery_outcome(
    activity: activitypub.models.OutgoingActivity,
    outcome: _DeliveryOutcome,
) -> None:
    if outcome.skip_reason is not None:
        activity.error = outcome.skip_reason
        _set_next_try(activity, outcome.retry_after)
    elif outcome.exception is not None:
        exc = outcome.exception
        if isinstance(exc, httpx.HTTPStatusError):
            activity.last_status_code = exc.response.status_code
            activity.last_response = exc.response.text
            activity.error = outcome.formatted_traceback

            if exc.response.status_code in [429, 503]:
                _set_next_try(activity, outcome.retry_after)
            elif exc.response.status_code == 401:
                _set_next_try(activity)
            elif 400 <= exc.response.status_code < 500:
                logger.info(f"status_code={exc.response.status_code} not retrying")
                activity.is_errored = True
                activity.next_try = None
            else:
                _set_next_try(activity)
        else:
            activity.error = outcome.formatted_traceback
            _set_next_try(activity)
    else:
        resp = outcome.response
        if resp is None:
            raise ValueError("Should never happen")
        activity.is_sent = True
        activity.last_status_code = resp.status_code
        activity.last_response = resp.text


async def process_outgoing_activities_batch(
    db_session: AsyncSession,
    activities: list[activitypub.models.OutgoingActivity],
) -> None:
    if not activities:
        return None

    activities_by_id = {activity.id: activity for activity in activities}
    delivery_requests: list[_DeliveryRequest] = []
    outcomes: list[_DeliveryOutcome] = []

    for next_activity in activities:
        next_activity.tries = next_activity.tries + 1  # type: ignore
        next_activity.last_try = now()

        logger.info(f"recipient={next_activity.recipient}")

        try:
            delivery_requests.append(_build_delivery_request(next_activity))
        except Exception as exc:
            logger.exception("Failed to prepare delivery")
            outcomes.append(
                _DeliveryOutcome(
                    activity_id=next_activity.id,  # type: ignore
                    exception=exc,
                    formatted_traceback=traceback.format_exc(),
                )
            )

    try:
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        raise

    outcomes.extend(await _deliver_batch(delivery_requests))

    for outcome in outcomes:
        _apply_delivery_outcome(activities_by_id[outcome.activity_id], outcome)

    # Defensive sweep: every row fetched into this batch must leave it either
    # sent, errored, or with an advanced `next_try` -- otherwise the next
    # poll would refetch it immediately and busy-loop.
    for activity in activities:
        if (
            not activity.is_sent
            and not activity.is_errored
            and (activity.next_try is None or activity.next_try <= now())
        ):
            logger.warning(f"Activity {activity.id} made no progress, forcing backoff")
            _set_next_try(activity)

    try:
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        raise

    return None


async def process_next_outgoing_activity(
    db_session: AsyncSession,
    next_activity: activitypub.models.OutgoingActivity,
) -> None:
    await process_outgoing_activities_batch(db_session, [next_activity])


class OutgoingActivityWorker(Worker[activitypub.models.OutgoingActivity]):
    batch_size = config.OUTGOING_DELIVERY_BATCH_SIZE

    async def get_next_messages(
        self,
        db_session: AsyncSession,
        limit: int,
    ) -> list[activitypub.models.OutgoingActivity]:
        return await fetch_next_outgoing_activities(db_session, limit)

    async def process_messages(
        self,
        db_session: AsyncSession,
        messages: list[activitypub.models.OutgoingActivity],
    ) -> None:
        await process_outgoing_activities_batch(db_session, messages)

    async def startup(self, db_session: AsyncSession) -> None:
        await _send_actor_update_if_needed(db_session)


async def loop() -> None:
    await OutgoingActivityWorker().run_forever()


if __name__ == "__main__":
    asyncio.run(loop())
