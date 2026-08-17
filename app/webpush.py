"""Web Push protocol primitives: VAPID (RFC 8292) request authorization and
`aes128gcm` (RFC 8291 / RFC 8188) payload encryption.

Pure protocol module: no DB, no FastAPI, no `app.models` imports. This keeps
it unit-testable on its own and importable from both the web process and the
push delivery worker (`app/push_notifications.py`) without dragging in the
whole app.

Everything is built on `pycryptodome`, already a hard dependency — no new
package is added for VAPID or `aes128gcm`.
"""

import base64
import json
import os
import time
from pathlib import Path
from typing import MutableMapping
from urllib.parse import urlparse

from cachetools import TTLCache
from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.DH import key_agreement
from Crypto.Protocol.KDF import HKDF
from Crypto.PublicKey import ECC
from Crypto.Random import get_random_bytes
from Crypto.Signature import DSS

from app import config

# RFC 8030 §7.2 only obliges a push service to accept a 4096-byte body.
# 86-byte header + 1-byte padding delimiter + 16-byte GCM tag leaves this
# much room for plaintext.
_RECORD_SIZE = 4096
MAX_PLAINTEXT = _RECORD_SIZE - 86 - 1 - 16

# Strictly shorter than the JWT's own `exp`, so a cached JWT is never served
# past the point a fresh one would already exist.
_VAPID_JWT_EXP_SECONDS = 60 * 60 * 12
_VAPID_JWT_CACHE_TTL = 60 * 60 * 11
_VAPID_JWT_CACHE: MutableMapping[str, str] = TTLCache(
    maxsize=64, ttl=_VAPID_JWT_CACHE_TTL
)

_JWT_HEADER_B64 = None  # set on first use, see _jwt_header_b64()


class WebPushError(Exception):
    pass


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _hkdf(master: bytes, key_len: int, salt: bytes, context: bytes) -> bytes:
    # `num_keys` defaults to 1, but the stub still types the return as
    # `bytes | tuple[bytes, ...]` — narrow it back for mypy.
    derived = HKDF(master, key_len, salt, SHA256, context=context)
    assert isinstance(derived, bytes)
    return derived


def _raw_public_point(key: ECC.EccKey) -> bytes:
    pub = key.public_key() if key.has_private() else key
    x = int(pub.pointQ.x).to_bytes(32, "big")
    y = int(pub.pointQ.y).to_bytes(32, "big")
    return b"\x04" + x + y


def decode_client_key(value: str) -> bytes:
    """Decode a client-supplied `p256dh`/`auth` key.

    Tolerates both base64 and base64url, padded or not — real Push API
    subscriptions vary in the wild. Shared by the subscribe endpoint (which
    validates what a client sends) and the delivery worker (which must
    decode it back the same way), so the two never drift.
    """
    normalized = value.strip().replace("-", "+").replace("_", "/")
    padded = normalized + "=" * (-len(normalized) % 4)
    return base64.b64decode(padded)


def parse_p256dh(raw: bytes) -> ECC.EccKey:
    """Parse a client's `p256dh` key: a 65-byte uncompressed P-256 point.

    Raises `ValueError` both on malformed input and on a point that isn't on
    the curve. The on-curve check (performed by `ECC.import_key` itself) is
    load-bearing: accepting an attacker-supplied off-curve point is an
    invalid-curve attack that can recover our ephemeral private key. Never
    remove it.
    """
    if len(raw) != 65 or raw[0] != 0x04:
        raise ValueError(
            "p256dh must be a 65-byte uncompressed EC point (0x04 || X || Y)"
        )
    return ECC.import_key(raw, curve_name="P-256")


def parse_auth_secret(raw: bytes) -> bytes:
    if len(raw) != 16:
        raise ValueError("auth secret must be exactly 16 bytes")
    return raw


# --- VAPID key management -------------------------------------------------
#
# Mirrors app/key.py's shape. The keypair lives at `data/vapid_key.pem`,
# generated lazily on first use so existing installs never need to re-run
# the config wizard (which also calls `_create_vapid_key` directly, so a
# fresh install skips the lazy path).


def _create_vapid_key(key_path: Path) -> None:
    """Idempotently ensure a VAPID (P-256) keypair PEM exists at `key_path`.

    Uses `os.open(..., O_CREAT | O_EXCL)` rather than tempfile + `os.replace`:
    `os.replace` is atomic but last-writer-wins, so two concurrent creators
    (the web process and the worker, on first boot) could each end up
    serving a different key — one process advertising key A while the other
    signs with key B. With `O_EXCL`, exactly one process wins the create;
    the loser simply reads the winner's file afterwards.
    """
    try:
        fd = os.open(str(key_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return

    try:
        key = ECC.generate(curve="P-256")
        os.write(fd, key.export_key(format="PEM").encode("ascii"))
    finally:
        os.close(fd)


def _read_vapid_key(key_path: Path) -> ECC.EccKey:
    _create_vapid_key(key_path)
    pem = key_path.read_text()
    if not pem.strip():
        raise WebPushError(f"VAPID key at {key_path} is empty or corrupt")
    return ECC.import_key(pem)


_vapid_key: ECC.EccKey | None = None


def get_vapid_key() -> ECC.EccKey:
    global _vapid_key
    if _vapid_key is None:
        _vapid_key = _read_vapid_key(config.VAPID_KEY_PATH)
    return _vapid_key


def vapid_public_key_b64() -> str:
    """The single source for `server_key` / `vapid_key` /
    `configuration.vapid.public_key` — an 87-char base64url string."""
    return _b64url_encode(_raw_public_point(get_vapid_key()))


# --- VAPID JWT (RFC 8292) --------------------------------------------------


def _jwt_header_b64() -> str:
    global _JWT_HEADER_B64
    if _JWT_HEADER_B64 is None:
        _JWT_HEADER_B64 = _b64url_encode(
            json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode()
        )
    return _JWT_HEADER_B64


def vapid_aud(endpoint: str) -> str:
    """The VAPID `aud` claim: the push endpoint's origin, with no path and no
    query. This is the single most common VAPID bug — both Mozilla autopush
    and FCM 401 a JWT whose `aud` carries a path."""
    parsed = urlparse(endpoint)
    return f"{parsed.scheme}://{parsed.netloc}"


def _sign_vapid_jwt(aud: str) -> str:
    sub = f"mailto:{config.CONTACT_EMAIL}" if config.CONTACT_EMAIL else config.ID
    payload = json.dumps(
        {"aud": aud, "exp": int(time.time()) + _VAPID_JWT_EXP_SECONDS, "sub": sub},
        separators=(",", ":"),
    ).encode()
    signing_input = f"{_jwt_header_b64()}.{_b64url_encode(payload)}"
    signer = DSS.new(get_vapid_key(), "fips-186-3")
    # Raw 64-byte r||s, not DER (71 bytes) — exactly what an ES256 JWT needs.
    signature = signer.sign(SHA256.new(signing_input.encode()))
    return f"{signing_input}.{_b64url_encode(signature)}"


def vapid_authorization_header(endpoint: str) -> str:
    """The full `Authorization` header value for a push request to `endpoint`."""
    aud = vapid_aud(endpoint)
    jwt = _VAPID_JWT_CACHE.get(aud)
    if jwt is None:
        jwt = _sign_vapid_jwt(aud)
        _VAPID_JWT_CACHE[aud] = jwt
    return f"vapid t={jwt}, k={vapid_public_key_b64()}"


# --- aes128gcm payload encryption (RFC 8291 / RFC 8188) -------------------


def encrypt(
    plaintext: bytes,
    *,
    ua_public: ECC.EccKey,
    auth_secret: bytes,
    salt: bytes | None = None,
    as_key: ECC.EccKey | None = None,
) -> bytes:
    """Encrypt `plaintext` for delivery to a Web Push endpoint.

    `salt` and `as_key` are keyword-only seams so tests can pin them and
    assert against the RFC 8291 Appendix A vector; production callers must
    leave both `None` — a fresh salt and ephemeral keypair per message.
    """
    if len(plaintext) > MAX_PLAINTEXT:
        raise WebPushError(f"plaintext exceeds MAX_PLAINTEXT ({MAX_PLAINTEXT} bytes)")

    salt = salt if salt is not None else get_random_bytes(16)
    as_key = as_key if as_key is not None else ECC.generate(curve="P-256")

    ua_public_raw = _raw_public_point(ua_public)
    as_public_raw = _raw_public_point(as_key)

    ecdh_secret = key_agreement(
        static_priv=as_key, static_pub=ua_public, kdf=lambda x: x
    )

    key_info = b"WebPush: info\x00" + ua_public_raw + as_public_raw
    ikm = _hkdf(ecdh_secret, 32, auth_secret, context=key_info)
    cek = _hkdf(ikm, 16, salt, context=b"Content-Encoding: aes128gcm\x00")
    nonce = _hkdf(ikm, 12, salt, context=b"Content-Encoding: nonce\x00")

    cipher = AES.new(cek, AES.MODE_GCM, nonce=nonce)
    # RFC 8188 §2: a single record's padding delimiter is always 0x02.
    ciphertext, tag = cipher.encrypt_and_digest(plaintext + b"\x02")

    header = (
        salt
        + _RECORD_SIZE.to_bytes(4, "big")
        + bytes([len(as_public_raw)])
        + as_public_raw
    )
    return header + ciphertext + tag
