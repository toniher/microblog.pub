"""Web Push delivery worker.

Mirrors the shape of `activitypub/outgoing_activities.py` (copied, not
imported — that module runs `Key.load(KEY_PATH.read_text())` at import time,
which a background worker importing this module has no reason to pull in).

Trigger design: this worker scans *committed* `Notification` rows via a
watermark (`PushSubscription.last_notification_id`) rather than hooking any
of the 18 notification-insert call sites. Inbox-side inserts only
`db_session.add()`; the commit happens inside `save_to_inbox`
(`activitypub/boxes.py`), itself inside a `begin_nested()` savepoint
(`activitypub/incoming_activities.py`) that can roll back. A hook would push
notifications that never durably landed.
"""

import asyncio
import html
import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import bleach
import httpx
from loguru import logger
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import joinedload

import activitypub.models
from app import config
from app import http_client
from app import models
from app import webpush
from app.database import AsyncSession
from app.i18n import gettext_default
from app.mastodon import serializers
from app.utils.datetime import now
from app.utils.datetime import parse_retry_after
from app.utils.url import InvalidURLError
from app.utils.url import check_url_async
from app.utils.workers import Worker
from app.webpush import decode_client_key
from app.webpush import parse_auth_secret
from app.webpush import parse_p256dh

_MAX_RETRIES = 16

# A phone that was off for a week wants the last few notifications, not four
# hundred -- the push service would drop most on TTL anyway, and hundreds of
# POSTs at one endpoint is what gets a VAPID key rate-limited.
_MAX_BACKLOG = 20
_MAX_CATCH_UP = 5

# Comfortably above `_MAX_BACKLOG` so a single fetch always captures the
# *entire* remaining backlog for a subscription in one query -- otherwise
# the "exhausted every candidate" fast-forward below would skip rows it
# never saw.
_FETCH_LIMIT = 64

# Only these have a real Mastodon `alerts[*]` key. MOVE is in
# NOTIFICATION_TYPE_MAP but Mastodon defines no `alerts[move]`; excluding it
# here means the SQL scan itself never returns a MOVE row, so its cursor
# advancement is handled by the same fast-forward-to-newest_id path as any
# other SQL-filtered notification.
_PUSHABLE_TYPES = [
    t for t in serializers.NOTIFICATION_TYPE_MAP if t != models.NotificationType.MOVE
]

_ALERT_ATTR = {
    "mention": "alert_mention",
    "favourite": "alert_favourite",
    "reblog": "alert_reblog",
    "follow": "alert_follow",
    "follow_request": "alert_follow_request",
}


def _exp_backoff(tries: int) -> datetime:
    seconds = 2 * (2 ** (tries - 1))
    return now() + timedelta(seconds=seconds)


def _apply_backoff(
    sub: models.PushSubscription, retry_after: datetime | None = None
) -> bool:
    """Returns True if the subscription has exhausted its retries and must
    be deleted -- the only route there is a persistently dead endpoint."""
    if sub.tries >= _MAX_RETRIES:  # type: ignore
        return True
    sub.next_try = retry_after or _exp_backoff(sub.tries)  # type: ignore
    return False


def _push_title(mastodon_type: str, display_name: str) -> str:
    if mastodon_type == "mention":
        title = gettext_default("%(name)s mentioned you")
    elif mastodon_type == "favourite":
        title = gettext_default("%(name)s favourited your post")
    elif mastodon_type == "reblog":
        title = gettext_default("%(name)s boosted your post")
    elif mastodon_type == "follow":
        title = gettext_default("%(name)s started following you")
    elif mastodon_type == "follow_request":
        title = gettext_default("%(name)s has requested to follow you")
    else:
        raise ValueError(f"no push title template for {mastodon_type!r}")
    return title % {"name": display_name}


def _excerpt(raw_html: str | None, limit: int = 140) -> str:
    if not raw_html:
        return ""
    text = bleach.clean(raw_html, tags=[], strip=True)
    text = html.unescape(text)
    return " ".join(text.split())[:limit]


def _encode_payload(payload: dict) -> bytes:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    if len(raw) <= webpush.MAX_PLAINTEXT:
        return raw

    # The payload builder already targets well under the ceiling; this is
    # only a safety net against a pathologically long display name/body.
    raw = json.dumps({**payload, "body": ""}, separators=(",", ":")).encode()
    if len(raw) <= webpush.MAX_PLAINTEXT:
        return raw

    raw = json.dumps(
        {**payload, "body": "", "title": payload["title"][:64]},
        separators=(",", ":"),
    ).encode()
    return raw[: webpush.MAX_PLAINTEXT]


def _build_payload(
    sub: models.PushSubscription,
    notif: models.Notification,
    mastodon_type: str,
) -> dict:
    actor = notif.actor
    if actor is None or sub.access_token is None:
        raise ValueError("Should never happen")

    target = notif.outbox_object or notif.inbox_object
    return {
        # Safe despite looking like a leak: the payload is end-to-end
        # encrypted to this client's user agent, matching upstream
        # Mastodon's own push payload design.
        "access_token": sub.access_token.access_token,
        "preferred_locale": config.LANGUAGE_CODE,
        "notification_id": str(notif.id),
        "notification_type": mastodon_type,
        "icon": actor.resized_icon_url,
        "title": _push_title(mastodon_type, actor.display_name),
        "body": _excerpt(target.content if target is not None else None),
    }


async def _send_push(sub: models.PushSubscription, payload: dict) -> httpx.Response:
    endpoint: str = sub.endpoint  # type: ignore
    ua_public = parse_p256dh(decode_client_key(sub.p256dh))  # type: ignore
    auth_secret = parse_auth_secret(decode_client_key(sub.auth))  # type: ignore
    body = webpush.encrypt(
        _encode_payload(payload), ua_public=ua_public, auth_secret=auth_secret
    )
    headers = {
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": "172800",
        "Urgency": "normal",
        "User-Agent": config.USER_AGENT,
        "Authorization": webpush.vapid_authorization_header(endpoint),
    }
    return await http_client.get_client().post(
        endpoint, content=body, headers=headers, timeout=httpx.Timeout(10.0)
    )


async def _deliver_one(
    db_session: AsyncSession,
    sub: models.PushSubscription,
    notif: models.Notification,
    mastodon_type: str,
) -> bool:
    """Attempt delivery of one notification to one subscription.

    Returns True if the cursor should advance past `notif` (delivered, or
    permanently unrecoverable for this notification only); False if delivery
    failed and this subscription's processing should stop for this cycle.
    """
    sub.tries += 1  # type: ignore
    sub.last_try = now()

    try:
        await check_url_async(sub.endpoint)  # type: ignore
        response = await _send_push(sub, _build_payload(sub, notif, mastodon_type))
    except InvalidURLError:
        logger.warning(f"push_subscription={sub.id} endpoint no longer allowed")
        await db_session.delete(sub)
        return False
    except httpx.HTTPError as exc:
        logger.warning(f"push_subscription={sub.id} delivery failed: {exc}")
        sub.error = str(exc)
        if _apply_backoff(sub):
            await db_session.delete(sub)
        return False

    sub.last_status_code = response.status_code

    if 200 <= response.status_code < 300:
        sub.tries = 0
        sub.last_success_at = now()
        sub.error = None
        return True

    sub.error = response.text[:500]

    if response.status_code in (404, 410):
        # RFC 8030 §7.3: gone at the push service -- retrying is
        # guaranteed-futile third-party traffic.
        logger.info(f"push_subscription={sub.id} gone ({response.status_code})")
        await db_session.delete(sub)
        return False

    if response.status_code == 413:
        # Permanent for this notification only: the size clamp is wrong, not
        # the subscription. Advance past it rather than punishing the
        # subscriber for our bug.
        logger.warning(f"push_subscription={sub.id} payload rejected as too large")
        sub.tries = 0
        return True

    retry_after = None
    if response.status_code in (429, 503):
        if retry_after_value := response.headers.get("Retry-After"):
            retry_after = parse_retry_after(retry_after_value)

    # Other 4xx (401/403 is usually a transient VAPID blip) and 5xx: back
    # off, let `_MAX_RETRIES` decide rather than treating as immediate death.
    if _apply_backoff(sub, retry_after):
        await db_session.delete(sub)
    return False


def _alert_enabled(sub: models.PushSubscription, mastodon_type: str) -> bool:
    attr = _ALERT_ATTR.get(mastodon_type)
    return attr is not None and bool(getattr(sub, attr))


async def _policy_allows(
    db_session: AsyncSession, sub: models.PushSubscription, notif: models.Notification
) -> bool:
    if sub.policy == "all":
        return True
    if sub.policy == "none":
        return False
    if notif.actor_id is None:
        return False

    if sub.policy == "followed":
        return (
            await db_session.scalar(
                select(activitypub.models.Following.id)
                .where(activitypub.models.Following.actor_id == notif.actor_id)
                .limit(1)
            )
        ) is not None
    if sub.policy == "follower":
        return (
            await db_session.scalar(
                select(activitypub.models.Follower.id)
                .where(activitypub.models.Follower.actor_id == notif.actor_id)
                .limit(1)
            )
        ) is not None
    return True


def _token_expired(token: models.IndieAuthAccessToken) -> bool:
    return now() > token.created_at.replace(tzinfo=timezone.utc) + timedelta(
        seconds=token.expires_in
    )


async def _process_subscription(
    db_session: AsyncSession,
    sub: models.PushSubscription,
    newest_id: int,
) -> None:
    if sub.access_token is None or _token_expired(sub.access_token):
        logger.info(f"push_subscription={sub.id} token revoked/expired, deleting")
        await db_session.delete(sub)
        return

    backlog = newest_id - sub.last_notification_id  # type: ignore
    if backlog <= 0:
        return

    if backlog > _MAX_BACKLOG:
        logger.warning(
            f"push_subscription={sub.id} backlog={backlog}, catching up to the "
            f"last {_MAX_CATCH_UP}"
        )
        sub.last_notification_id = newest_id - _MAX_CATCH_UP

    candidates = (
        (
            await db_session.scalars(
                select(models.Notification)
                .where(
                    models.Notification.id > sub.last_notification_id,
                    models.Notification.id <= newest_id,
                    models.Notification.actor_id.is_not(None),
                    models.Notification.notification_type.in_(_PUSHABLE_TYPES),
                    models.notification_not_muted(),
                    models.notification_not_in_muted_conversation(),
                )
                .order_by(models.Notification.id)
                .limit(_FETCH_LIMIT)
                .options(*serializers.NOTIFICATION_OPTIONS)
            )
        )
        .unique()
        .all()
    )

    delivered = 0
    for notif in candidates:
        if delivered >= _MAX_CATCH_UP:
            break

        mastodon_type = serializers.NOTIFICATION_TYPE_MAP[notif.notification_type]
        if not _alert_enabled(sub, mastodon_type) or not await _policy_allows(
            db_session, sub, notif
        ):
            sub.last_notification_id = notif.id
            continue

        if await _deliver_one(db_session, sub, notif, mastodon_type):
            sub.last_notification_id = notif.id
            delivered += 1
        else:
            return
    else:
        # Exhausted every candidate without a failed delivery: any row the
        # SQL filters silently dropped (muted, non-pushable type,
        # actor-less) must still be stepped over, or the next cycle would
        # re-scan the same gap forever.
        sub.last_notification_id = newest_id


async def fetch_next_push_subscriptions(
    db_session: AsyncSession,
    limit: int,
) -> list[models.PushSubscription]:
    """The anti-busy-loop gate: a subscription with nothing new isn't
    returned, so `Worker._main_loop` takes its idle sleep instead of
    spinning."""
    pending_exists = (
        select(models.Notification.id)
        .where(
            models.Notification.id > models.PushSubscription.last_notification_id,
            models.Notification.actor_id.is_not(None),
            models.Notification.notification_type.in_(_PUSHABLE_TYPES),
            models.notification_not_muted(),
            models.notification_not_in_muted_conversation(),
        )
        .correlate(models.PushSubscription)
        .exists()
    )
    return list(
        (
            await db_session.scalars(
                select(models.PushSubscription)
                .join(
                    models.IndieAuthAccessToken,
                    models.PushSubscription.access_token_id
                    == models.IndieAuthAccessToken.id,
                )
                .where(
                    models.IndieAuthAccessToken.is_revoked.is_(False),
                    models.PushSubscription.next_try <= now(),
                    pending_exists,
                )
                .order_by(models.PushSubscription.next_try, models.PushSubscription.id)
                .limit(limit)
                .options(joinedload(models.PushSubscription.access_token))
            )
        )
        .unique()
        .all()
    )


async def process_push_subscriptions_batch(
    db_session: AsyncSession,
    subscriptions: list[models.PushSubscription],
) -> None:
    if not subscriptions:
        return

    newest_id = await db_session.scalar(select(func.max(models.Notification.id))) or 0

    for sub in subscriptions:
        await _process_subscription(db_session, sub, newest_id)

    await db_session.commit()


class PushNotificationWorker(Worker[models.PushSubscription]):
    batch_size = config.PUSH_DELIVERY_BATCH_SIZE

    async def get_next_messages(
        self, db_session: AsyncSession, limit: int
    ) -> list[models.PushSubscription]:
        return await fetch_next_push_subscriptions(db_session, limit)

    async def process_messages(
        self, db_session: AsyncSession, messages: list[models.PushSubscription]
    ) -> None:
        await process_push_subscriptions_batch(db_session, messages)


async def loop() -> None:
    await PushNotificationWorker().run_forever()


if __name__ == "__main__":
    asyncio.run(loop())
