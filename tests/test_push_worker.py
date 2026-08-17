import base64
import json
import secrets
from datetime import timedelta

import httpx
import pytest
import respx
from Crypto.Cipher import AES
from Crypto.Protocol.DH import key_agreement
from Crypto.PublicKey import ECC
from Crypto.Random import get_random_bytes
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import activitypub.models
from app import models
from app import push_notifications
from app import webpush
from app.utils.datetime import as_utc
from app.utils.datetime import now
from tests.utils import setup_remote_actor
from tests.utils import setup_remote_actor_as_follower


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _decrypt(body: bytes, *, ua_private: ECC.EccKey, auth_secret: bytes) -> bytes:
    """A from-scratch aes128gcm receiver, independent of `app.webpush.encrypt` —
    see tests/test_webpush.py for why this matters."""
    salt = body[0:16]
    idlen = body[20]
    as_public_raw = body[21 : 21 + idlen]
    ciphertext_and_tag = body[21 + idlen :]
    ciphertext, tag = ciphertext_and_tag[:-16], ciphertext_and_tag[-16:]

    as_public = ECC.import_key(as_public_raw, curve_name="P-256")
    ua_public_raw = webpush._raw_public_point(ua_private.public_key())

    ecdh_secret = key_agreement(
        static_priv=ua_private, static_pub=as_public, kdf=lambda x: x
    )
    key_info = b"WebPush: info\x00" + ua_public_raw + as_public_raw
    ikm = webpush._hkdf(ecdh_secret, 32, auth_secret, context=key_info)
    cek = webpush._hkdf(ikm, 16, salt, context=b"Content-Encoding: aes128gcm\x00")
    nonce = webpush._hkdf(ikm, 12, salt, context=b"Content-Encoding: nonce\x00")

    padded = AES.new(cek, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ciphertext, tag)
    assert padded[-1:] == b"\x02"
    return padded[:-1]


async def _make_access_token_row(
    db_session: AsyncSession, scope: str
) -> models.IndieAuthAccessToken:
    token = models.IndieAuthAccessToken(
        access_token=secrets.token_urlsafe(16),
        refresh_token=None,
        expires_in=3600,
        scope=scope,
    )
    db_session.add(token)
    await db_session.commit()
    return token


async def _make_subscription(
    db_session: AsyncSession,
    access_token_id: int | None,
    ua_key: ECC.EccKey,
    auth_secret: bytes,
    *,
    endpoint: str = "https://push.example.net/sub/abc",
    **overrides,
) -> models.PushSubscription:
    sub = models.PushSubscription(
        access_token_id=access_token_id,
        endpoint=endpoint,
        p256dh=_b64url(webpush._raw_public_point(ua_key)),
        auth=_b64url(auth_secret),
        alert_mention=True,
        alert_status=True,
        alert_reblog=True,
        alert_follow=True,
        alert_follow_request=True,
        alert_favourite=True,
        alert_poll=True,
        alert_update=True,
        policy="all",
        last_notification_id=0,
        tries=0,
        next_try=now(),
    )
    for key, value in overrides.items():
        setattr(sub, key, value)
    db_session.add(sub)
    await db_session.commit()
    return sub


async def _make_notification(
    db_session: AsyncSession,
    actor_id: int | None,
    notification_type: models.NotificationType = models.NotificationType.NEW_FOLLOWER,
) -> models.Notification:
    notif = models.Notification(notification_type=notification_type, actor_id=actor_id)
    db_session.add(notif)
    await db_session.commit()
    return notif


@pytest.mark.asyncio
async def test_worker_encrypts_and_delivers_a_real_payload(
    async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    """Centerpiece test: builds a receiver keypair, runs one batch against a
    mocked 201, then decrypts the captured body. Covers VAPID + aes128gcm +
    payload shape + HTTP headers + the 4096-byte ceiling in one pass."""
    ra = setup_remote_actor(respx_mock, base_url="https://follower.example")
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None

    token_row = await _make_access_token_row(async_db_session, "push")
    ua_key = ECC.generate(curve="P-256")
    auth_secret = get_random_bytes(16)
    await _make_subscription(
        async_db_session,
        token_row.id,
        ua_key,
        auth_secret,
        endpoint="https://push.example.net/sub/centerpiece",
    )
    notif = await _make_notification(async_db_session, follower.actor.id)

    captured: dict = {}

    def _capture(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        captured["content"] = request.content
        return httpx.Response(201)

    respx_mock.post("https://push.example.net/sub/centerpiece").mock(
        side_effect=_capture
    )

    subs = await push_notifications.fetch_next_push_subscriptions(async_db_session, 10)
    assert len(subs) == 1
    await push_notifications.process_push_subscriptions_batch(async_db_session, subs)

    headers = captured["headers"]
    assert headers["content-encoding"] == "aes128gcm"
    assert headers["content-type"] == "application/octet-stream"
    assert headers["ttl"] == "172800"
    assert headers["urgency"] == "normal"

    auth_header = headers["authorization"]
    assert auth_header.startswith("vapid t=")
    jwt = auth_header.split("t=", 1)[1].split(",", 1)[0]
    claims = json.loads(_b64url_decode(jwt.split(".")[1]))
    assert claims["aud"] == "https://push.example.net"

    body = captured["content"]
    assert len(body) <= 4096

    plaintext = _decrypt(body, ua_private=ua_key, auth_secret=auth_secret)
    payload = json.loads(plaintext)
    assert payload["notification_id"] == str(notif.id)
    assert payload["notification_type"] == "follow"
    assert payload["access_token"] == token_row.access_token
    assert payload["title"]

    sub = (await async_db_session.scalars(select(models.PushSubscription))).one()
    assert sub.last_notification_id == notif.id
    assert sub.tries == 0
    assert sub.last_success_at is not None


@pytest.mark.asyncio
async def test_worker_deletes_subscription_on_410(
    async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock)
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    token_row = await _make_access_token_row(async_db_session, "push")
    ua_key = ECC.generate(curve="P-256")
    await _make_subscription(
        async_db_session,
        token_row.id,
        ua_key,
        get_random_bytes(16),
        endpoint="https://push.example.net/sub/gone",
    )
    await _make_notification(async_db_session, follower.actor.id)

    respx_mock.post("https://push.example.net/sub/gone").mock(
        return_value=httpx.Response(410)
    )

    subs = await push_notifications.fetch_next_push_subscriptions(async_db_session, 10)
    await push_notifications.process_push_subscriptions_batch(async_db_session, subs)

    remaining = await async_db_session.scalar(
        select(func.count(models.PushSubscription.id))
    )
    assert remaining == 0


@pytest.mark.asyncio
async def test_worker_backs_off_on_500_without_advancing_cursor(
    async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock)
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    token_row = await _make_access_token_row(async_db_session, "push")
    ua_key = ECC.generate(curve="P-256")
    sub = await _make_subscription(
        async_db_session,
        token_row.id,
        ua_key,
        get_random_bytes(16),
        endpoint="https://push.example.net/sub/flaky",
    )
    await _make_notification(async_db_session, follower.actor.id)

    respx_mock.post("https://push.example.net/sub/flaky").mock(
        return_value=httpx.Response(500)
    )

    subs = await push_notifications.fetch_next_push_subscriptions(async_db_session, 10)
    await push_notifications.process_push_subscriptions_batch(async_db_session, subs)

    await async_db_session.refresh(sub)
    assert sub.tries == 1
    assert sub.last_notification_id == 0
    assert sub.next_try is not None
    assert as_utc(sub.next_try) > now()


@pytest.mark.asyncio
async def test_worker_honours_retry_after_on_429(
    async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock)
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    token_row = await _make_access_token_row(async_db_session, "push")
    ua_key = ECC.generate(curve="P-256")
    sub = await _make_subscription(
        async_db_session,
        token_row.id,
        ua_key,
        get_random_bytes(16),
        endpoint="https://push.example.net/sub/throttled",
    )
    await _make_notification(async_db_session, follower.actor.id)

    respx_mock.post("https://push.example.net/sub/throttled").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "120"})
    )

    subs = await push_notifications.fetch_next_push_subscriptions(async_db_session, 10)
    await push_notifications.process_push_subscriptions_batch(async_db_session, subs)

    await async_db_session.refresh(sub)
    assert sub.next_try is not None
    delta = (as_utc(sub.next_try) - now()).total_seconds()
    assert 100 < delta < 140


@pytest.mark.asyncio
async def test_worker_advances_cursor_on_413_without_deleting(
    async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock)
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    token_row = await _make_access_token_row(async_db_session, "push")
    ua_key = ECC.generate(curve="P-256")
    sub = await _make_subscription(
        async_db_session,
        token_row.id,
        ua_key,
        get_random_bytes(16),
        endpoint="https://push.example.net/sub/toobig",
    )
    notif = await _make_notification(async_db_session, follower.actor.id)

    respx_mock.post("https://push.example.net/sub/toobig").mock(
        return_value=httpx.Response(413)
    )

    subs = await push_notifications.fetch_next_push_subscriptions(async_db_session, 10)
    await push_notifications.process_push_subscriptions_batch(async_db_session, subs)

    await async_db_session.refresh(sub)
    assert sub.last_notification_id == notif.id
    assert sub.tries == 0
    remaining = await async_db_session.scalar(
        select(func.count(models.PushSubscription.id))
    )
    assert remaining == 1


@pytest.mark.asyncio
async def test_worker_deletes_subscription_after_exhausting_retries(
    async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock)
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    token_row = await _make_access_token_row(async_db_session, "push")
    ua_key = ECC.generate(curve="P-256")
    await _make_subscription(
        async_db_session,
        token_row.id,
        ua_key,
        get_random_bytes(16),
        endpoint="https://push.example.net/sub/dying",
        tries=push_notifications._MAX_RETRIES - 1,
    )
    await _make_notification(async_db_session, follower.actor.id)

    respx_mock.post("https://push.example.net/sub/dying").mock(
        return_value=httpx.Response(500)
    )

    subs = await push_notifications.fetch_next_push_subscriptions(async_db_session, 10)
    await push_notifications.process_push_subscriptions_batch(async_db_session, subs)

    remaining = await async_db_session.scalar(
        select(func.count(models.PushSubscription.id))
    )
    assert remaining == 0


@pytest.mark.asyncio
async def test_worker_skips_muted_actor_without_delivering(
    db, async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    # A muted actor's notification is dropped by the SQL scan's own
    # `notification_not_muted()` filter, including the anti-busy-loop EXISTS
    # gate -- so with *only* a muted notification pending, the subscription
    # is never even returned to advance its cursor (correct: no wasted
    # work). Pair it with a second, unmuted notification so there is
    # something to actually advance the cursor over, and assert the muted
    # one drew no HTTP call while the unmuted one did.
    muted_ra = setup_remote_actor(respx_mock, base_url="https://muted.example")
    muted_follower = setup_remote_actor_as_follower(muted_ra)
    assert muted_follower.actor is not None
    # `follower.actor` was created through the sync `_Session` the
    # factories use (see activitypub/tests/conftest.py); mutate and commit
    # through the matching sync `db` fixture, not `async_db_session`.
    muted_follower.actor.is_muted = True
    muted_follower.actor.are_notifications_muted = True
    db.commit()

    unmuted_ra = setup_remote_actor(respx_mock, base_url="https://unmuted.example")
    unmuted_follower = setup_remote_actor_as_follower(unmuted_ra)
    assert unmuted_follower.actor is not None

    token_row = await _make_access_token_row(async_db_session, "push")
    ua_key = ECC.generate(curve="P-256")
    sub = await _make_subscription(
        async_db_session,
        token_row.id,
        ua_key,
        get_random_bytes(16),
        endpoint="https://push.example.net/sub/mute-mix",
    )
    await _make_notification(async_db_session, muted_follower.actor.id)
    unmuted_notif = await _make_notification(
        async_db_session, unmuted_follower.actor.id
    )

    calls = {"count": 0}

    def _accept(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(201)

    respx_mock.post("https://push.example.net/sub/mute-mix").mock(side_effect=_accept)

    subs = await push_notifications.fetch_next_push_subscriptions(async_db_session, 10)
    await push_notifications.process_push_subscriptions_batch(async_db_session, subs)

    assert calls["count"] == 1
    await async_db_session.refresh(sub)
    assert sub.last_notification_id == unmuted_notif.id


@pytest.mark.asyncio
async def test_worker_skips_disabled_alert_without_delivering(
    async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock)
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    token_row = await _make_access_token_row(async_db_session, "push")
    ua_key = ECC.generate(curve="P-256")
    sub = await _make_subscription(
        async_db_session,
        token_row.id,
        ua_key,
        get_random_bytes(16),
        endpoint="https://push.example.net/sub/no-follow-alert",
        alert_follow=False,
    )
    notif = await _make_notification(async_db_session, follower.actor.id)

    subs = await push_notifications.fetch_next_push_subscriptions(async_db_session, 10)
    await push_notifications.process_push_subscriptions_batch(async_db_session, subs)

    await async_db_session.refresh(sub)
    assert sub.last_notification_id == notif.id


@pytest.mark.asyncio
async def test_worker_policy_follower_skips_non_follower(
    async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock, base_url="https://stranger.example")
    stranger = activitypub.models.Actor(
        ap_type="Person",
        ap_actor=ra.ap_actor,
        ap_id=ra.ap_id,
    )
    async_db_session.add(stranger)
    await async_db_session.commit()

    token_row = await _make_access_token_row(async_db_session, "push")
    ua_key = ECC.generate(curve="P-256")
    sub = await _make_subscription(
        async_db_session,
        token_row.id,
        ua_key,
        get_random_bytes(16),
        endpoint="https://push.example.net/sub/follower-only",
        policy="follower",
    )
    notif = await _make_notification(
        async_db_session,
        stranger.id,
        notification_type=models.NotificationType.LIKE,
    )

    subs = await push_notifications.fetch_next_push_subscriptions(async_db_session, 10)
    await push_notifications.process_push_subscriptions_batch(async_db_session, subs)

    await async_db_session.refresh(sub)
    assert sub.last_notification_id == notif.id


@pytest.mark.asyncio
async def test_worker_scan_excludes_revoked_token_subscriptions(
    async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock)
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    token_row = await _make_access_token_row(async_db_session, "push")
    ua_key = ECC.generate(curve="P-256")
    await _make_subscription(
        async_db_session,
        token_row.id,
        ua_key,
        get_random_bytes(16),
        endpoint="https://push.example.net/sub/revoked",
    )
    await _make_notification(async_db_session, follower.actor.id)

    token_row.is_revoked = True
    async_db_session.add(token_row)
    await async_db_session.commit()

    # The revoked-token filter lives in the SQL scan itself, so the
    # subscription is never even returned as a candidate. (Actual deletion
    # on revoke happens eagerly in the oauth revoke endpoints, tested in
    # tests/mastodon/test_oauth.py.)
    subs = await push_notifications.fetch_next_push_subscriptions(async_db_session, 10)
    assert subs == []


@pytest.mark.asyncio
async def test_worker_deletes_subscription_for_expired_token(
    async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock)
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    token_row = await _make_access_token_row(async_db_session, "push")
    token_row.expires_in = 1
    token_row.created_at = now() - timedelta(days=1)
    async_db_session.add(token_row)
    await async_db_session.commit()

    ua_key = ECC.generate(curve="P-256")
    await _make_subscription(
        async_db_session,
        token_row.id,
        ua_key,
        get_random_bytes(16),
        endpoint="https://push.example.net/sub/expired",
        # `next_try`/backlog gates live on the subscription row; force a
        # pending notification so the EXISTS scan still surfaces it despite
        # the token itself being long expired.
    )
    await _make_notification(async_db_session, follower.actor.id)

    subs = await push_notifications.fetch_next_push_subscriptions(async_db_session, 10)
    await push_notifications.process_push_subscriptions_batch(async_db_session, subs)

    remaining = await async_db_session.scalar(
        select(func.count(models.PushSubscription.id))
    )
    assert remaining == 0


@pytest.mark.asyncio
async def test_worker_caps_delivery_and_catches_up_on_flood(
    async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock)
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    token_row = await _make_access_token_row(async_db_session, "push")
    ua_key = ECC.generate(curve="P-256")
    await _make_subscription(
        async_db_session,
        token_row.id,
        ua_key,
        get_random_bytes(16),
        endpoint="https://push.example.net/sub/flood",
    )

    notifs = [
        await _make_notification(async_db_session, follower.actor.id) for _ in range(30)
    ]

    delivered = {"count": 0}

    def _count_and_accept(request: httpx.Request) -> httpx.Response:
        delivered["count"] += 1
        return httpx.Response(201)

    respx_mock.post("https://push.example.net/sub/flood").mock(
        side_effect=_count_and_accept
    )

    subs = await push_notifications.fetch_next_push_subscriptions(async_db_session, 10)
    await push_notifications.process_push_subscriptions_batch(async_db_session, subs)

    # 30 pending exceeds `_MAX_BACKLOG`, so the cursor jumps to
    # `newest - _MAX_CATCH_UP` before delivering at most `_MAX_CATCH_UP`.
    assert delivered["count"] == push_notifications._MAX_CATCH_UP
    sub = (await async_db_session.scalars(select(models.PushSubscription))).one()
    assert sub.last_notification_id == notifs[-1].id


@pytest.mark.asyncio
async def test_fetch_next_push_subscriptions_empty_when_nothing_pending(
    async_db_session: AsyncSession, respx_mock: respx.MockRouter
) -> None:
    ra = setup_remote_actor(respx_mock)
    follower = setup_remote_actor_as_follower(ra)
    assert follower.actor is not None
    token_row = await _make_access_token_row(async_db_session, "push")
    ua_key = ECC.generate(curve="P-256")
    notif = await _make_notification(async_db_session, follower.actor.id)
    # Already caught up: nothing new since `last_notification_id`.
    await _make_subscription(
        async_db_session,
        token_row.id,
        ua_key,
        get_random_bytes(16),
        endpoint="https://push.example.net/sub/caught-up",
        last_notification_id=notif.id,
    )

    subs = await push_notifications.fetch_next_push_subscriptions(async_db_session, 10)
    assert subs == []
