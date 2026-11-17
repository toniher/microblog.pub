import base64
import time

from app.config import BASE_URL
from app.config import hmac_sha256

SUPPORTED_RESIZE = [50, 740]
# Proxy links are signed with an expiry counted in days (EXPIRY_PERIOD).
# The expiry is bucketed by EXPIRY_LENGTH days so a link stays byte-identical
# for a whole bucket: browsers (1-year Cache-Control), the nginx proxy cache
# and the resize cache are all keyed on the URL, and a URL that changed daily
# defeated all three. A link minted in a bucket stays valid until the end of
# the *next* bucket: between EXPIRY_LENGTH and 2 * EXPIRY_LENGTH days.
EXPIRY_PERIOD = 86400
EXPIRY_LENGTH = 7


class InvalidProxySignatureError(Exception):
    pass


def proxied_media_sig(expires: int, url: str) -> str:
    hm = hmac_sha256()
    hm.update(f"{expires}".encode())
    hm.update(b"|")
    hm.update(url.encode())
    return base64.urlsafe_b64encode(hm.digest()).decode()


def verify_proxied_media_sig(expires: int, url: str, sig: str) -> None:
    now = int(time.time() / EXPIRY_PERIOD)
    expected = proxied_media_sig(expires, url)
    if now > expires or sig != expected:
        raise InvalidProxySignatureError("invalid or expired media")


def current_expiry() -> int:
    """The `expires` value (in EXPIRY_PERIOD units) for links minted now;
    constant across each EXPIRY_LENGTH-day bucket."""
    day = int(time.time() / EXPIRY_PERIOD)
    return (day // EXPIRY_LENGTH + 2) * EXPIRY_LENGTH


def proxied_media_url(url: str) -> str:
    if url.startswith(BASE_URL):
        return url
    expires = current_expiry()
    sig = proxied_media_sig(expires, url)

    return (
        BASE_URL
        + f"/proxy/media/{expires}/{sig}/"
        + base64.urlsafe_b64encode(url.encode()).decode()
    )


def resized_media_url(url: str, size: int) -> str:
    if size not in SUPPORTED_RESIZE:
        raise ValueError(f"Unsupported resize {size}")
    if url.startswith(BASE_URL):
        return url
    return proxied_media_url(url) + f"/{size}"
