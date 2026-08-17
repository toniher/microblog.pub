import base64
import json
import time

import pytest
from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.DH import key_agreement
from Crypto.PublicKey import ECC
from Crypto.Signature import DSS

from app import webpush


def _b64url_decode(s: str) -> bytes:
    s = s.replace(" ", "").replace("\n", "")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# --- RFC 8291 Appendix A ----------------------------------------------------
#
# https://www.rfc-editor.org/rfc/rfc8291.html#appendix-A


_RFC_PLAINTEXT = _b64url_decode(
    "V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24"
)
_RFC_AS_PUBLIC_RAW = _b64url_decode(
    "BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8w"
    "EqKK6PBru3jl7A8"
)
_RFC_AS_PRIVATE_RAW = _b64url_decode("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw")
_RFC_UA_PUBLIC_RAW = _b64url_decode(
    "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZ"
    "GH6SRpkNtoIAiw4"
)
_RFC_SALT = _b64url_decode("DGv6ra1nlYgDCS1FRnbzlw")
_RFC_AUTH_SECRET = _b64url_decode("BTBZMqHH6r4Tts7J_aSIgg")

_RFC_ECDH_SECRET = _b64url_decode("kyrL1jIIOHEzg3sM2ZWRHDRB62YACZhhSlknJ672kSs")
_RFC_KEY_INFO = _b64url_decode(
    "V2ViUHVzaDogaW5mbwAEJXGyvs3942BVGq8e0PTNNmwRzr5VX4m8t7GGpTM5FzFo7OLr4BhZ"
    "e9MEebhuPI-OztV3ylkYfpJGmQ22ggCLDgT-M_SrDepxkU21WCP3O1SUj0EwbZIHMtu5pZpT"
    "KGSCIA5Zent7wmC6HCJ5mFgJkuk5cwAvMBKiiujwa7t45ewP"
)
_RFC_IKM = _b64url_decode("S4lYMb_L0FxCeq0WhDx813KgSYqU26kOyzWUdsXYyrg")
_RFC_CEK = _b64url_decode("oIhVW04MRdy2XN9CiKLxTg")
_RFC_NONCE = _b64url_decode("4h_95klXJ5E_qnoN")
_RFC_HEADER = _b64url_decode(
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIg"
    "Dll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8"
)
_RFC_BODY = _b64url_decode(
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml"
    "mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT"
    "pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
)


def _rfc_as_key() -> ECC.EccKey:
    return ECC.construct(curve="P-256", d=int.from_bytes(_RFC_AS_PRIVATE_RAW, "big"))


def test_rfc8291_ecdh_secret():
    as_key = _rfc_as_key()
    ua_public = webpush.parse_p256dh(_RFC_UA_PUBLIC_RAW)
    secret = key_agreement(static_priv=as_key, static_pub=ua_public, kdf=lambda x: x)
    assert secret == _RFC_ECDH_SECRET


def test_rfc8291_key_info():
    key_info = b"WebPush: info\x00" + _RFC_UA_PUBLIC_RAW + _RFC_AS_PUBLIC_RAW
    assert key_info == _RFC_KEY_INFO


def test_rfc8291_ikm():
    ikm = webpush._hkdf(_RFC_ECDH_SECRET, 32, _RFC_AUTH_SECRET, context=_RFC_KEY_INFO)
    assert ikm == _RFC_IKM


def test_rfc8291_cek_and_nonce():
    cek = webpush._hkdf(
        _RFC_IKM, 16, _RFC_SALT, context=b"Content-Encoding: aes128gcm\x00"
    )
    nonce = webpush._hkdf(
        _RFC_IKM, 12, _RFC_SALT, context=b"Content-Encoding: nonce\x00"
    )
    assert cek == _RFC_CEK
    assert nonce == _RFC_NONCE


def test_rfc8291_header():
    header = (
        _RFC_SALT
        + (4096).to_bytes(4, "big")
        + bytes([len(_RFC_AS_PUBLIC_RAW)])
        + _RFC_AS_PUBLIC_RAW
    )
    assert header == _RFC_HEADER


def test_rfc8291_full_vector_byte_identical():
    ua_public = webpush.parse_p256dh(_RFC_UA_PUBLIC_RAW)
    body = webpush.encrypt(
        _RFC_PLAINTEXT,
        ua_public=ua_public,
        auth_secret=_RFC_AUTH_SECRET,
        salt=_RFC_SALT,
        as_key=_rfc_as_key(),
    )
    assert body == _RFC_BODY


# --- Independent round-trip --------------------------------------------------


def _decrypt(body: bytes, *, ua_private: ECC.EccKey, auth_secret: bytes) -> bytes:
    """A from-scratch aes128gcm receiver, deliberately not sharing code with
    `app.webpush.encrypt` — this catches a self-consistent-but-wrong pair
    that a shared-code round-trip would miss."""
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


def test_round_trip():
    ua_key = ECC.generate(curve="P-256")
    auth_secret = b"\x11" * 16
    plaintext = b'{"hello": "world"}'

    body = webpush.encrypt(
        plaintext, ua_public=ua_key.public_key(), auth_secret=auth_secret
    )
    decrypted = _decrypt(body, ua_private=ua_key, auth_secret=auth_secret)
    assert decrypted == plaintext


def test_round_trip_max_plaintext():
    ua_key = ECC.generate(curve="P-256")
    auth_secret = b"\x22" * 16
    plaintext = b"x" * webpush.MAX_PLAINTEXT

    body = webpush.encrypt(
        plaintext, ua_public=ua_key.public_key(), auth_secret=auth_secret
    )
    assert len(body) <= 4096
    decrypted = _decrypt(body, ua_private=ua_key, auth_secret=auth_secret)
    assert decrypted == plaintext


def test_encrypt_rejects_oversized_plaintext():
    ua_key = ECC.generate(curve="P-256")
    with pytest.raises(webpush.WebPushError):
        webpush.encrypt(
            b"x" * (webpush.MAX_PLAINTEXT + 1),
            ua_public=ua_key.public_key(),
            auth_secret=b"\x00" * 16,
        )


# --- Rejections --------------------------------------------------------------


def test_parse_p256dh_rejects_wrong_length():
    with pytest.raises(ValueError):
        webpush.parse_p256dh(b"\x04" + b"\x00" * 63)  # 64 bytes total


def test_parse_p256dh_rejects_wrong_prefix():
    ua_key = ECC.generate(curve="P-256")
    raw = webpush._raw_public_point(ua_key)
    bad = b"\x02" + raw[1:]
    with pytest.raises(ValueError):
        webpush.parse_p256dh(bad)


def test_parse_p256dh_rejects_off_curve_point():
    # The on-curve check is load-bearing: accepting an off-curve point is an
    # invalid-curve attack that can recover our ephemeral private key.
    bad = b"\x04" + b"\x01" * 64
    with pytest.raises(ValueError):
        webpush.parse_p256dh(bad)


def test_parse_auth_secret_rejects_wrong_length():
    with pytest.raises(ValueError):
        webpush.parse_auth_secret(b"\x00" * 15)


def test_parse_auth_secret_accepts_16_bytes():
    secret = b"\x00" * 16
    assert webpush.parse_auth_secret(secret) == secret


# --- VAPID JWT ----------------------------------------------------------------


def test_vapid_aud_strips_path_and_query():
    assert (
        webpush.vapid_aud("https://push.example.net/sub/abc?x=1")
        == "https://push.example.net"
    )


def test_vapid_authorization_header_shape():
    header = webpush.vapid_authorization_header("https://push.example.net/sub/abc")
    assert header.startswith("vapid t=")
    assert ", k=" in header
    jwt_part = header.split("t=", 1)[1].split(",", 1)[0]
    assert len(jwt_part.split(".")) == 3


def test_vapid_jwt_header_and_payload():
    header_value = webpush.vapid_authorization_header(
        "https://push.example.net/sub/abc?x=1"
    )
    jwt = header_value.split("t=", 1)[1].split(",", 1)[0]
    header_b64, payload_b64, sig_b64 = jwt.split(".")

    jwt_header = json.loads(_b64url_decode(header_b64))
    assert jwt_header == {"typ": "JWT", "alg": "ES256"}

    payload = json.loads(_b64url_decode(payload_b64))
    assert payload["aud"] == "https://push.example.net"
    assert 0 < payload["exp"] - int(time.time()) <= 60 * 60 * 24

    signature = _b64url_decode(sig_b64)
    assert len(signature) == 64

    verifier = DSS.new(webpush.get_vapid_key().public_key(), "fips-186-3")
    verifier.verify(SHA256.new(f"{header_b64}.{payload_b64}".encode()), signature)


def test_vapid_authorization_header_carries_public_key():
    header_value = webpush.vapid_authorization_header("https://push.example.net/sub")
    k = header_value.split("k=", 1)[1]
    assert k == webpush.vapid_public_key_b64()


# --- VAPID key file management -------------------------------------------


def test_create_vapid_key_is_idempotent(tmp_path):
    key_path = tmp_path / "vapid_key.pem"
    webpush._create_vapid_key(key_path)
    first = key_path.read_text()
    webpush._create_vapid_key(key_path)
    second = key_path.read_text()
    assert first == second
    ECC.import_key(first)  # doesn't raise


def test_read_vapid_key_raises_on_empty_file(tmp_path):
    key_path = tmp_path / "vapid_key.pem"
    key_path.write_text("")
    with pytest.raises(webpush.WebPushError):
        webpush._read_vapid_key(key_path)


def test_vapid_public_key_b64_length():
    # 65-byte uncompressed point, base64url without padding.
    assert len(webpush.vapid_public_key_b64()) == 87
