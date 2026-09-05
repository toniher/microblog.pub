"""Mastodon Streaming API over WebSocket (`/api/v1/streaming`).

Cross-process delivery is a single in-process poll task, not a broker: uvicorn
runs as one process/one event loop (`numprocs=1`, no `--workers`), so the only
requirement is something inside the web process that learns about rows the
other three processes (incoming/outgoing/push workers) committed. SQLite is
WAL, so a poll never blocks a writer. `delete`/`status.update` additionally
need a bounded tracked-row re-poll rather than a plain `updated_at` watch
(neither `InboxObject` nor `OutboxObject` auto-maintains `updated_at` on
every write).

This module owns the whole feature: the event bus (`Hub`/`Subscriber`), the
poll loop (`EventPump`), and the WebSocket/health routes. It imports
`app.mastodon.timelines` (not `app.mastodon.router`, to avoid a circular
import — `router.py` imports this module to mount the routes).
"""

import asyncio
import contextlib
import json
from collections import OrderedDict
from dataclasses import dataclass

from fastapi import APIRouter
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from loguru import logger
from sqlalchemy import func
from sqlalchemy import select
from starlette.responses import PlainTextResponse

import activitypub.models
from activitypub import activitypub as ap
from activitypub.boxes import AnyboxObject
from app import config
from app import models
from app.database import AsyncSession
from app.database import async_session
from app.indieauth import AccessTokenInfo
from app.indieauth import _check_access_token
from app.indieauth import _to_token_info
from app.mastodon import ids
from app.mastodon import serializers
from app.mastodon import timelines
from app.mastodon.scopes import has_scope

router = APIRouter()

# A stream subscription: ("hashtag", "cats") or ("user", None).
StreamKey = tuple[str, str | None]

_VALID_STREAM_KINDS = {
    "user",
    "user:notification",
    "public",
    "public:local",
    "public:remote",
    "hashtag",
    "direct",
    "list",
}

# Backpressure: drop the oldest frame rather than close the socket. A client
# stalled for a moment should miss a post, not lose the connection — its next
# REST poll heals the gap, and reconnecting costs it every subscription with
# no replay anyway.
_QUEUE_MAXSIZE = 256

# Hard cap on how many streams one socket may subscribe to. `hashtag` keys
# carry a client-supplied tag, so without this a single connection could grow
# `Subscriber.streams` without bound — costing memory and, worse, making
# `Hub.active_hashtags()` (rebuilt on every batch of new statuses) arbitrarily
# large. Mastodon clients subscribe to a handful of streams; 64 is generous.
_MAX_STREAMS_PER_SUBSCRIBER = 64

# How many of the newest rows per table are watched for delete/edit. Bounded
# on purpose: a full-table is_deleted scan every tick is not worth it for
# statuses far outside what any live client is currently looking at.
_SEED_TRACK = 200
_TRACK_LIMIT = 500

# New-row scan and notification-scan page size per tick.
_FETCH_LIMIT = 100

# How often an idle writer re-validates its token against revocation. Access
# tokens here are effectively non-expiring, so revocation is the only event
# worth polling for, and a few minutes of grace on a revoked token is no
# worse than a cached REST client already gets.
_TOKEN_RECHECK_INTERVAL = 300.0


def streaming_base_url() -> str | None:
    """The `wss://`/`ws://` origin advertised as `urls.streaming_api` (v1) and
    `configuration.urls.streaming` (v2, the one Mastodon 4.x clients read).
    No path — clients append `/api/v1/streaming` themselves. `None` when
    streaming is disabled, so the instance advertisement omits the key
    entirely rather than pointing at an endpoint that will refuse to connect.
    """
    if not config.STREAMING_ENABLED:
        return None
    scheme = "wss" if config.CONFIG.https else "ws"
    return f"{scheme}://{config.DOMAIN}"


@dataclass(frozen=True)
class Event:
    event: str  # update | status.update | delete | notification | conversation
    payload: str  # already a JSON string (or a bare status id for `delete`)
    streams: frozenset


class Subscriber:
    def __init__(self, token: str) -> None:
        self.token = token
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self.streams: set[StreamKey] = set()
        self.dropped = 0

    def offer(self, event: Event) -> None:
        try:
            self.queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        with contextlib.suppress(asyncio.QueueEmpty):
            self.queue.get_nowait()
        self.dropped += 1
        if self.dropped == 1:
            logger.warning("streaming: subscriber queue full, dropping oldest frame")
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(event)


@dataclass
class _Tracked:
    status_id: str
    is_deleted: bool
    # inbox: `updated_at`; outbox: `len(revisions)`. Both are exact edit
    # signals already in the schema (see activitypub/boxes.py:2037 and
    # send_update's revisions append) — no onupdate=/migration needed.
    signal: object


def _classify_streams(obj: AnyboxObject, active_hashtags: set[str]) -> frozenset:
    """Which streams `obj` belongs to, mirroring the REST timeline filters
    exactly: `public` is precisely `user ∩ {visibility == PUBLIC}`, the same
    relationship `timelines_public`'s non-local branch has to `timelines_home`.

    Deliberately excludes `list`/`exclusive`: both need a DB round-trip
    (list membership, the exclusive-member set) that a pure sync classifier
    can't make, so `_apply_list_and_exclusive` widens the result afterwards.
    """
    keys: set[StreamKey] = {("user", None)}
    if obj.visibility == ap.VisibilityEnum.PUBLIC:
        keys.add(("public", None))
        if isinstance(obj, activitypub.models.OutboxObject):
            keys.add(("public:local", None))
        else:
            keys.add(("public:remote", None))
        # Intersect rather than scanning `active_hashtags` and calling
        # `has_tag` once per subscribed tag: this is O(len(obj.tags)) no
        # matter how many hashtag streams are subscribed, so no client can
        # make an arriving status expensive for the pump.
        for tag in timelines.tag_names(obj) & active_hashtags:
            keys.add(("hashtag", tag))
    return frozenset(keys)


def _apply_list_and_exclusive(
    obj: AnyboxObject,
    keys: frozenset,
    list_hits: dict[int, set[str]],
    exclusive_actor_ids: set[int],
) -> frozenset | None:
    """Union in `("list", …)` keys for the lists `obj` belongs to (from
    `list_hits`, keyed by `InboxObject.id`), and drop `("user", None)` when
    `obj`'s author is a member of an `exclusive` list — `exclusive` has to
    reach the `user` stream too, or it drifts from REST home, since
    `_classify_streams`'s own contract is that `user` mirrors the home
    timeline. `public*`/`hashtag` keys are unaffected, matching Mastodon.

    Both belong here rather than in `_classify_streams` because a list
    timeline (unlike `public`) must carry followers-only posts too, and
    because both need `list_hits`/`exclusive_actor_ids`, which the pure sync
    classifier has no DB access to compute. Returns `None` when nothing is
    left to publish (a followers-only post from an exclusive member has no
    `public*` key to fall back to).
    """
    if not isinstance(obj, activitypub.models.InboxObject):
        return keys
    assert obj.id is not None
    widened = set(keys) | {("list", lid) for lid in list_hits.get(obj.id, ())}
    if obj.actor_id in exclusive_actor_ids:
        widened.discard(("user", None))
    return frozenset(widened) if widened else None


class EventPump:
    """Polls committed rows and turns them into `Event`s.

    `tick()` is the testable primitive (mirrors how tests/test_push_worker.py
    drives the push worker's primitives instead of its loop) — it takes a
    short-lived session, does one round of work, and returns. `run()` is the
    idle-sleep loop around it, copied from `Worker._main_loop`'s shape
    (`app/utils/workers.py`) WITHOUT subclassing `Worker`, which installs
    process-level signal handlers via `run_forever()` — catastrophic here,
    since this task lives inside the uvicorn process.
    """

    def __init__(self, hub: "Hub") -> None:
        self._hub = hub
        self._inbox_cursor = 0
        self._outbox_cursor = 0
        self._notification_cursor = 0
        self._tracked_inbox: "OrderedDict[int, _Tracked]" = OrderedDict()
        self._tracked_outbox: "OrderedDict[int, _Tracked]" = OrderedDict()

    async def _sleep(self, stop_event: asyncio.Event, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=seconds)

    async def seed(self, db_session: AsyncSession) -> None:
        """Cursors start at the current MAX(id): a connecting client gets
        only live events, never a backlog. The tracked maps are seeded with
        the newest rows per table, since those are exactly what a client that
        just loaded its REST timeline is looking at — and thus what the owner
        is about to delete or edit.
        """
        self._inbox_cursor = await db_session.scalar(
            select(func.coalesce(func.max(activitypub.models.InboxObject.id), 0))
        )
        self._outbox_cursor = await db_session.scalar(
            select(func.coalesce(func.max(activitypub.models.OutboxObject.id), 0))
        )
        self._notification_cursor = await db_session.scalar(
            select(func.coalesce(func.max(models.Notification.id), 0))
        )

        self._tracked_inbox.clear()
        self._tracked_outbox.clear()

        inbox_rows = (
            await db_session.execute(
                select(
                    activitypub.models.InboxObject.id,
                    activitypub.models.InboxObject.is_deleted,
                    activitypub.models.InboxObject.updated_at,
                    activitypub.models.InboxObject.ap_published_at,
                )
                .order_by(activitypub.models.InboxObject.id.desc())
                .limit(_SEED_TRACK)
            )
        ).all()
        for row_id, is_deleted, updated_at, published_at in reversed(inbox_rows):
            status_id = ids.encode_object_id(
                row_id, ids.ObjectSource.INBOX, published_at
            )
            self._tracked_inbox[row_id] = _Tracked(
                status_id, bool(is_deleted), updated_at
            )

        outbox_rows = (
            await db_session.execute(
                select(
                    activitypub.models.OutboxObject.id,
                    activitypub.models.OutboxObject.is_deleted,
                    func.coalesce(
                        func.json_array_length(
                            activitypub.models.OutboxObject.revisions
                        ),
                        0,
                    ),
                    activitypub.models.OutboxObject.ap_published_at,
                )
                .order_by(activitypub.models.OutboxObject.id.desc())
                .limit(_SEED_TRACK)
            )
        ).all()
        for row_id, is_deleted, revcount, published_at in reversed(outbox_rows):
            status_id = ids.encode_object_id(
                row_id, ids.ObjectSource.OUTBOX, published_at
            )
            self._tracked_outbox[row_id] = _Tracked(
                status_id, bool(is_deleted), revcount
            )

    def _track(
        self, tracked: "OrderedDict[int, _Tracked]", row_id: int, entry: _Tracked
    ) -> None:
        tracked[row_id] = entry
        while len(tracked) > _TRACK_LIMIT:
            tracked.popitem(last=False)

    async def _new_statuses(
        self, db_session: AsyncSession
    ) -> tuple[list[int], list[int], bool]:
        behind = False

        new_inbox_ids = list(
            (
                await db_session.scalars(
                    select(activitypub.models.InboxObject.id)
                    .where(activitypub.models.InboxObject.id > self._inbox_cursor)
                    .order_by(activitypub.models.InboxObject.id.asc())
                    .limit(_FETCH_LIMIT)
                )
            ).all()
        )
        if new_inbox_ids:
            self._inbox_cursor = new_inbox_ids[-1]
            if len(new_inbox_ids) >= _FETCH_LIMIT:
                behind = True

        new_outbox_ids = list(
            (
                await db_session.scalars(
                    select(activitypub.models.OutboxObject.id)
                    .where(activitypub.models.OutboxObject.id > self._outbox_cursor)
                    .order_by(activitypub.models.OutboxObject.id.asc())
                    .limit(_FETCH_LIMIT)
                )
            ).all()
        )
        if new_outbox_ids:
            self._outbox_cursor = new_outbox_ids[-1]
            if len(new_outbox_ids) >= _FETCH_LIMIT:
                behind = True

        return new_inbox_ids, new_outbox_ids, behind

    async def _active_list_policies(self, db_session: AsyncSession) -> dict[int, str]:
        """`{list_id: replies_policy}` for every list currently subscribed to
        by at least one socket. Resolved fresh each call (never cached)
        since a `PUT` can change `replies_policy` between ticks.
        """
        active = self._hub.active_lists()
        if not active:
            return {}
        list_ids = {
            internal_id
            for lid in active
            if (internal_id := ids.decode_list_id(lid)) is not None
        }
        if not list_ids:
            return {}
        rows = (
            await db_session.execute(
                select(
                    models.MastodonList.id, models.MastodonList.replies_policy
                ).where(models.MastodonList.id.in_(list_ids))
            )
        ).all()
        return {row[0]: row[1] for row in rows}

    async def _list_hits(
        self,
        db_session: AsyncSession,
        active_lists: dict[int, str],
        inbox_ids: list[int],
    ) -> dict[int, set[str]]:
        """Which of `active_lists` each of `inbox_ids` belongs to, one query
        per subscribed list reusing `models.list_timeline_where` verbatim —
        the REST timeline and the stream can't drift apart this way, exactly
        why `_direct_events` re-runs `dm_threads()` instead of reimplementing
        its filter.
        """
        hits: dict[int, set[str]] = {}
        if not active_lists or not inbox_ids:
            return hits
        for list_id, replies_policy in active_lists.items():
            matched_ids = await db_session.scalars(
                select(activitypub.models.InboxObject.id).where(
                    activitypub.models.InboxObject.id.in_(inbox_ids),
                    *models.list_timeline_where(list_id, replies_policy),
                )
            )
            for inbox_id in matched_ids:
                hits.setdefault(inbox_id, set()).add(str(list_id))
        return hits

    async def _status_events(
        self,
        db_session: AsyncSession,
        new_inbox_ids: list[int],
        new_outbox_ids: list[int],
    ) -> list[Event]:
        if not new_inbox_ids and not new_outbox_ids:
            return []

        active_hashtags = self._hub.active_hashtags()
        active_lists = await self._active_list_policies(db_session)
        list_hits = await self._list_hits(db_session, active_lists, new_inbox_ids)
        exclusive_actor_ids = set(
            await db_session.scalars(models.exclusive_list_member_actor_ids())
        )
        events: list[Event] = []

        inbox_rows: list[AnyboxObject] = []
        if new_inbox_ids:
            inbox_rows = list(
                await timelines.fetch_inbox_timeline_page(
                    db_session,
                    before=None,
                    after=None,
                    limit=len(new_inbox_ids),
                    extra_where=(activitypub.models.InboxObject.id.in_(new_inbox_ids),),
                )
            )
        outbox_rows: list[AnyboxObject] = []
        if new_outbox_ids:
            outbox_rows = list(
                await timelines.fetch_outbox_timeline_page(
                    db_session,
                    before=None,
                    after=None,
                    limit=len(new_outbox_ids),
                    extra_where=(
                        activitypub.models.OutboxObject.id.in_(new_outbox_ids),
                    ),
                )
            )

        rows = sorted([*inbox_rows, *outbox_rows], key=timelines.status_id_int)
        if rows:
            await serializers.prefetch_status_relations(db_session, rows)

        for obj in rows:
            streams = _apply_list_and_exclusive(
                obj,
                _classify_streams(obj, active_hashtags),
                list_hits,
                exclusive_actor_ids,
            )

            status_id = (
                ids.encode_outbox_id(obj)
                if isinstance(obj, activitypub.models.OutboxObject)
                else ids.encode_inbox_id(obj)
            )
            # A row just fetched from the DB always has an id; encode_*_id
            # above already raises otherwise.
            assert obj.id is not None
            if isinstance(obj, activitypub.models.OutboxObject):
                self._track(
                    self._tracked_outbox,
                    obj.id,
                    _Tracked(status_id, False, len(obj.revisions or [])),
                )
            else:
                self._track(
                    self._tracked_inbox,
                    obj.id,
                    _Tracked(status_id, False, obj.updated_at),
                )

            if streams is None:
                # A followers-only post from an exclusive-list member: no
                # `public*` key to fall back to, so there is nothing to
                # publish, but the row above is still tracked for a later
                # edit/delete.
                continue
            payload = json.dumps(await serializers.serialize_status(db_session, obj))
            events.append(Event("update", payload, streams))

        return events

    async def _notification_events(self, db_session: AsyncSession) -> list[Event]:
        new_ids = list(
            (
                await db_session.scalars(
                    select(models.Notification.id)
                    .where(models.Notification.id > self._notification_cursor)
                    .order_by(models.Notification.id.asc())
                    .limit(_FETCH_LIMIT)
                )
            ).all()
        )
        if not new_ids:
            return []
        self._notification_cursor = new_ids[-1]

        notifications = (
            (
                await db_session.execute(
                    select(models.Notification)
                    .where(
                        models.Notification.id.in_(new_ids),
                        models.notification_not_muted(),
                        models.notification_not_in_muted_conversation(),
                    )
                    .options(*serializers.NOTIFICATION_OPTIONS)
                    .order_by(models.Notification.id.asc())
                )
            )
            .unique()
            .scalars()
            .all()
        )

        events = []
        streams = frozenset({("user:notification", None), ("user", None)})
        for notification in notifications:
            entity = await serializers.serialize_notification(db_session, notification)
            if entity is None:
                continue
            events.append(Event("notification", json.dumps(entity), streams))
        return events

    async def _direct_events(
        self,
        db_session: AsyncSession,
        new_inbox_ids: list[int],
        new_outbox_ids: list[int],
    ) -> list[Event]:
        if "direct" not in self._hub.active_stream_kinds():
            return []
        if not new_inbox_ids and not new_outbox_ids:
            return []

        contexts: set[str] = set()
        if new_inbox_ids:
            contexts.update(
                (
                    await db_session.scalars(
                        select(activitypub.models.InboxObject.ap_context).where(
                            activitypub.models.InboxObject.id.in_(new_inbox_ids),
                            activitypub.models.InboxObject.visibility
                            == ap.VisibilityEnum.DIRECT,
                            activitypub.models.InboxObject.ap_context.is_not(None),
                            activitypub.models.InboxObject.is_transient.is_(False),
                            activitypub.models.InboxObject.is_deleted.is_(False),
                        )
                    )
                ).all()
            )
        if new_outbox_ids:
            contexts.update(
                (
                    await db_session.scalars(
                        select(activitypub.models.OutboxObject.ap_context).where(
                            activitypub.models.OutboxObject.id.in_(new_outbox_ids),
                            activitypub.models.OutboxObject.visibility
                            == ap.VisibilityEnum.DIRECT,
                            activitypub.models.OutboxObject.ap_context.is_not(None),
                            activitypub.models.OutboxObject.is_transient.is_(False),
                            activitypub.models.OutboxObject.is_deleted.is_(False),
                        )
                    )
                ).all()
            )
        if not contexts:
            return []

        threads = await timelines.dm_threads(db_session)
        streams = frozenset({("direct", None)})
        events = []
        for last, actor_ids, unread in threads:
            if last.ap_context in contexts:
                entity = await serializers.serialize_conversation(
                    db_session, last, actor_ids, unread
                )
                events.append(Event("conversation", json.dumps(entity), streams))
        return events

    async def _edit_and_delete_events(self, db_session: AsyncSession) -> list[Event]:
        # Two `IN (<=500 ids)` re-polls per tick, forever, is the pump's whole
        # idle cost. Nobody subscribed to anything means every event produced
        # here would be dropped by `Hub.publish` anyway, so skip the queries
        # outright — a socket that connects without `?stream=` and never sends
        # a subscribe frame then costs nothing. Tracked baselines keep being
        # maintained by `_status_events`, so a delete that happens during the
        # gap still fires on the first tick after a subscribe.
        subscribed = self._hub.all_subscribed_stream_keys()
        if not subscribed:
            return []

        active_lists = await self._active_list_policies(db_session)
        exclusive_actor_ids = set(
            await db_session.scalars(models.exclusive_list_member_actor_ids())
        )

        events: list[Event] = []

        if self._tracked_inbox:
            tracked_ids = list(self._tracked_inbox.keys())
            rows = (
                await db_session.execute(
                    select(
                        activitypub.models.InboxObject.id,
                        activitypub.models.InboxObject.is_deleted,
                        activitypub.models.InboxObject.updated_at,
                    ).where(activitypub.models.InboxObject.id.in_(tracked_ids))
                )
            ).all()
            seen = set()
            updated: list[tuple[int, AnyboxObject, dict]] = []
            for row_id, is_deleted, updated_at in rows:
                seen.add(row_id)
                tracked = self._tracked_inbox[row_id]
                if is_deleted and not tracked.is_deleted:
                    events.append(
                        Event(
                            "delete",
                            tracked.status_id,
                            subscribed,
                        )
                    )
                    del self._tracked_inbox[row_id]
                    continue
                if updated_at != tracked.signal:
                    inbox_obj_rows = await timelines.fetch_inbox_timeline_page(
                        db_session,
                        before=None,
                        after=None,
                        limit=1,
                        extra_where=(activitypub.models.InboxObject.id == row_id,),
                    )
                    if inbox_obj_rows:
                        obj = inbox_obj_rows[0]
                        await serializers.prefetch_status_relations(
                            db_session, inbox_obj_rows
                        )
                        entity = await serializers.serialize_status(db_session, obj)
                        updated.append((row_id, obj, entity))
                    tracked.signal = updated_at

            # One `_list_hits` call for the whole batch of edited rows, not
            # one per row: a burst of edits (e.g. a worker rewriting many
            # `updated_at`s in one pass) would otherwise multiply list
            # queries by row count instead of by subscribed-list count.
            list_hits = await self._list_hits(
                db_session, active_lists, [row_id for row_id, _, _ in updated]
            )
            active_hashtags = self._hub.active_hashtags()
            for row_id, updated_obj, entity in updated:
                streams = _apply_list_and_exclusive(
                    updated_obj,
                    _classify_streams(updated_obj, active_hashtags),
                    list_hits,
                    exclusive_actor_ids,
                )
                if streams is not None:
                    events.append(Event("status.update", json.dumps(entity), streams))
            # Rows the WHERE clause never returned (hard-pruned by
            # app/prune.py) are silently dropped: they're never rows a live
            # client still holds, so a `delete` frame would be wrong.
            for row_id in list(self._tracked_inbox.keys()):
                if row_id not in seen:
                    del self._tracked_inbox[row_id]

        if self._tracked_outbox:
            tracked_ids = list(self._tracked_outbox.keys())
            rows = (
                await db_session.execute(
                    select(
                        activitypub.models.OutboxObject.id,
                        activitypub.models.OutboxObject.is_deleted,
                        func.coalesce(
                            func.json_array_length(
                                activitypub.models.OutboxObject.revisions
                            ),
                            0,
                        ),
                    ).where(activitypub.models.OutboxObject.id.in_(tracked_ids))
                )
            ).all()
            seen = set()
            for row_id, is_deleted, revcount in rows:
                seen.add(row_id)
                tracked = self._tracked_outbox[row_id]
                if is_deleted and not tracked.is_deleted:
                    events.append(
                        Event(
                            "delete",
                            tracked.status_id,
                            subscribed,
                        )
                    )
                    del self._tracked_outbox[row_id]
                    continue
                if revcount != tracked.signal:
                    outbox_obj_rows = await timelines.fetch_outbox_timeline_page(
                        db_session,
                        before=None,
                        after=None,
                        limit=1,
                        extra_where=(activitypub.models.OutboxObject.id == row_id,),
                    )
                    if outbox_obj_rows:
                        await serializers.prefetch_status_relations(
                            db_session, outbox_obj_rows
                        )
                        entity = await serializers.serialize_status(
                            db_session, outbox_obj_rows[0]
                        )
                        streams = _classify_streams(
                            outbox_obj_rows[0], self._hub.active_hashtags()
                        )
                        events.append(
                            Event("status.update", json.dumps(entity), streams)
                        )
                    tracked.signal = revcount
            for row_id in list(self._tracked_outbox.keys()):
                if row_id not in seen:
                    del self._tracked_outbox[row_id]

        return events

    async def tick(self, db_session: AsyncSession) -> tuple[list[Event], bool]:
        """One round of work: new statuses/notifications/conversations, plus
        a bounded re-poll of tracked rows for deletes/edits. Returns the
        events to publish and whether the caller is behind (a full batch was
        seen, so `run()` should skip its idle sleep).
        """
        new_inbox_ids, new_outbox_ids, behind = await self._new_statuses(db_session)

        events: list[Event] = []
        events += await self._status_events(db_session, new_inbox_ids, new_outbox_ids)
        events += await self._notification_events(db_session)
        events += await self._direct_events(db_session, new_inbox_ids, new_outbox_ids)
        events += await self._edit_and_delete_events(db_session)

        return events, behind

    async def run(self, stop_event: asyncio.Event) -> None:
        """`stop_event` is owned by the task, not by the pump.

        A pump instance outlives the tasks that drive it (the last client
        disconnecting stops the task; the next one starts a new one over the
        same cursors). Reading the event off `self` instead would let a
        restart that races a shutdown swap the object out from under the
        outgoing task, which would then never observe its own stop and would
        poll alongside its replacement until cancelled.
        """
        while not stop_event.is_set():
            behind = False
            try:
                async with async_session() as db_session:
                    events, behind = await self.tick(db_session)
                for event in events:
                    self._hub.publish(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                # No supervisord behind this task: a poison row must never
                # crash the pump (or the process it lives in).
                logger.exception("streaming event pump tick failed")

            if not behind:
                await self._sleep(stop_event, config.STREAMING_POLL_INTERVAL)


class Hub:
    """Module-level singleton: the connection set, the refcounted pump
    lifecycle, and fan-out. `publish()` is sync (no `await`), so a slow
    subscriber's queue can never stall the pump.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._subscribers: set[Subscriber] = set()
        self._pump = EventPump(self)
        self._task: "asyncio.Task[None] | None" = None
        self._stop_event: asyncio.Event | None = None

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def register(self, sub: Subscriber) -> None:
        async with self._lock:
            self._subscribers.add(sub)
            if self._task is None:
                async with async_session() as db_session:
                    await self._pump.seed(db_session)
                self._stop_event = asyncio.Event()
                self._task = asyncio.create_task(
                    self._pump.run(self._stop_event),
                    name="mastodon-streaming-pump",
                )

    async def unregister(self, sub: Subscriber) -> None:
        task_to_stop = None
        async with self._lock:
            self._subscribers.discard(sub)
            if not self._subscribers and self._task is not None:
                task_to_stop, self._task = self._task, None
                # Signal inside the lock, against the event this very task was
                # started with: a `register()` that races the wait below then
                # builds a fresh event for its own task and cannot be stopped
                # by this shutdown.
                if self._stop_event is not None:
                    self._stop_event.set()
                self._stop_event = None
        if task_to_stop is not None:
            # wait_for cancels the task if it overruns, so a tick that hangs
            # can delay teardown but can never leave a second pump polling.
            with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(task_to_stop, timeout=5)

    def publish(self, event: Event) -> None:
        for sub in list(self._subscribers):
            if event.streams & sub.streams:
                sub.offer(event)

    def active_stream_kinds(self) -> set[str]:
        return {key[0] for sub in self._subscribers for key in sub.streams}

    def active_hashtags(self) -> set[str]:
        return {
            key[1]
            for sub in self._subscribers
            for key in sub.streams
            if key[0] == "hashtag" and key[1]
        }

    def active_lists(self) -> set[str]:
        return {
            key[1]
            for sub in self._subscribers
            for key in sub.streams
            if key[0] == "list" and key[1]
        }

    def all_subscribed_stream_keys(self) -> frozenset:
        keys: set[StreamKey] = set()
        for sub in self._subscribers:
            keys.update(sub.streams)
        return frozenset(keys)

    async def aclose(self) -> None:
        task_to_stop = None
        async with self._lock:
            self._subscribers.clear()
            if self._task is not None:
                task_to_stop, self._task = self._task, None
                if self._stop_event is not None:
                    self._stop_event.set()
                self._stop_event = None
        if task_to_stop is not None:
            task_to_stop.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task_to_stop


hub = Hub()


def _parse_stream_key(
    stream: object, tag: object, list_id: object = None
) -> StreamKey | None:
    if not isinstance(stream, str) or stream not in _VALID_STREAM_KINDS:
        return None
    if stream == "hashtag":
        if not isinstance(tag, str) or not tag:
            return None
        return ("hashtag", tag.lstrip("#").lower())
    if stream == "list":
        if not isinstance(list_id, str) or not list_id:
            return None
        return ("list", list_id)
    return (stream, None)


async def _resolve_ws_token(
    websocket: WebSocket, db_session: AsyncSession
) -> tuple[AccessTokenInfo | None, str | None]:
    """Resolve the bearer token from a WebSocket handshake, in Mastodon's
    order: query param (browsers can't set headers on a WS), then the
    Sec-WebSocket-Protocol header, then Authorization. Returns the offered
    subprotocol too, since it must be echoed back on accept() if used.
    """
    token = websocket.query_params.get("access_token")
    subprotocol = None
    if not token:
        proto_header = websocket.headers.get("sec-websocket-protocol")
        if proto_header:
            offered = [p.strip() for p in proto_header.split(",") if p.strip()]
            if offered:
                token = offered[0]
                subprotocol = offered[0]
    if not token:
        auth = websocket.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            token = auth.removeprefix("Bearer ")
    if not token:
        return None, None

    is_valid, access_token = await _check_access_token(db_session, token)
    if not is_valid or access_token is None:
        return None, subprotocol

    return _to_token_info(access_token), subprotocol


async def _token_still_valid(token: str) -> bool:
    async with async_session() as db_session:
        is_valid, _ = await _check_access_token(db_session, token)
        return is_valid


async def _send_error(websocket: WebSocket, message: str) -> None:
    with contextlib.suppress(Exception):
        await websocket.send_text(json.dumps({"error": message}))


def _frame(event: Event, key: StreamKey) -> dict:
    kind, tag = key
    stream = [kind, tag] if tag is not None else [kind]
    return {"stream": stream, "event": event.event, "payload": event.payload}


async def _reader_loop(
    websocket: WebSocket, sub: Subscriber, token_info: AccessTokenInfo
) -> None:
    while True:
        try:
            data = await websocket.receive_text()
        except WebSocketDisconnect:
            return

        try:
            message = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            await _send_error(websocket, "Malformed JSON")
            continue
        if not isinstance(message, dict):
            await _send_error(websocket, "Malformed JSON")
            continue

        msg_type = message.get("type")
        if msg_type not in ("subscribe", "unsubscribe"):
            await _send_error(websocket, "Unknown message type")
            continue

        stream = message.get("stream")

        key = _parse_stream_key(stream, message.get("tag"), message.get("list"))
        if key is None:
            await _send_error(websocket, "Unknown stream")
            continue
        if key[0] == "user:notification" and not has_scope(
            token_info, "read:notifications"
        ):
            await _send_error(
                websocket, "This stream requires the read:notifications scope"
            )
            continue
        if key[0] == "list" and not has_scope(token_info, "read:lists"):
            await _send_error(websocket, "This stream requires the read:lists scope")
            continue

        if msg_type == "subscribe":
            if key[0] == "list":
                # Bounds the pump's per-tick list work by the number of lists
                # that actually exist rather than by client-supplied ids —
                # strictly better than the hashtag case
                # `_MAX_STREAMS_PER_SUBSCRIBER` exists to contain.
                internal_id = ids.decode_list_id(key[1] or "")
                exists = False
                if internal_id is not None:
                    async with async_session() as db_session:
                        exists = (
                            await db_session.get(models.MastodonList, internal_id)
                            is not None
                        )
                if not exists:
                    await _send_error(websocket, "Unknown list")
                    continue
            if (
                key not in sub.streams
                and len(sub.streams) >= _MAX_STREAMS_PER_SUBSCRIBER
            ):
                await _send_error(websocket, "Too many subscribed streams")
                continue
            sub.streams.add(key)
        else:
            sub.streams.discard(key)


async def _writer_loop(websocket: WebSocket, sub: Subscriber) -> None:
    while True:
        try:
            event = await asyncio.wait_for(
                sub.queue.get(), timeout=_TOKEN_RECHECK_INTERVAL
            )
        except asyncio.TimeoutError:
            if not await _token_still_valid(sub.token):
                await websocket.close(code=1008, reason="Access token revoked")
                return
            continue

        for key in sorted(k for k in event.streams if k in sub.streams):
            await websocket.send_text(json.dumps(_frame(event, key)))


def _initial_stream_key(
    websocket: WebSocket, token_info: AccessTokenInfo
) -> StreamKey | None:
    stream = websocket.query_params.get("stream")
    if not stream:
        return None
    key = _parse_stream_key(
        stream,
        websocket.query_params.get("tag"),
        websocket.query_params.get("list"),
    )
    if key is None:
        return None
    if key[0] == "user:notification" and not has_scope(
        token_info, "read:notifications"
    ):
        return None
    if key[0] == "list" and not has_scope(token_info, "read:lists"):
        return None
    return key


@router.websocket("/api/v1/streaming")
async def streaming_endpoint(websocket: WebSocket) -> None:
    if not config.STREAMING_ENABLED:
        await websocket.close(code=1008, reason="Streaming is disabled")
        return

    async with async_session() as db_session:
        token_info, subprotocol = await _resolve_ws_token(websocket, db_session)

    if token_info is None or not has_scope(token_info, "read:statuses"):
        await websocket.close(code=1008, reason="Invalid access token")
        return

    if hub.subscriber_count() >= config.STREAMING_MAX_CONNECTIONS:
        await websocket.close(code=1013, reason="Too many connections")
        return

    sub = Subscriber(token=token_info.access_token)
    initial_key = _initial_stream_key(websocket, token_info)
    if initial_key is not None:
        sub.streams.add(initial_key)

    # Register (which seeds the pump's cursors) BEFORE accept(): otherwise a
    # status posted in the gap between accept() and register() would already
    # be reflected in MAX(id) by the time seed() runs, and get silently
    # absorbed into the baseline instead of delivered as a live event.
    await hub.register(sub)
    if subprotocol:
        await websocket.accept(subprotocol=subprotocol)
    else:
        await websocket.accept()

    try:
        reader = asyncio.create_task(_reader_loop(websocket, sub, token_info))
        writer = asyncio.create_task(_writer_loop(websocket, sub))
        _, pending = await asyncio.wait(
            {reader, writer}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        await hub.unregister(sub)
        with contextlib.suppress(Exception):
            await websocket.close()


@router.get("/api/v1/streaming/health", response_model=None)
async def streaming_health() -> PlainTextResponse:
    return PlainTextResponse("OK")
