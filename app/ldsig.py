import asyncio
import base64
import hashlib
import typing
from datetime import datetime

import pyld  # type: ignore
from Crypto.Hash import SHA256
from Crypto.Signature import PKCS1_v1_5
from loguru import logger
from pyld import jsonld  # type: ignore

from activitypub import activitypub as ap
from app.database import AsyncSession
from app.httpsig import _get_public_key
from app.utils.url import check_url

if typing.TYPE_CHECKING:
    from app.key import Key


# (connect, read), the requests idiom. pyld dereferences the payload's
# `@context` over the network, and for an inbox POST that URL is chosen by
# whoever sent the activity. requests_document_loader() forwards its kwargs
# straight to requests.get(), which without a `timeout` waits forever: a host
# that accepts the TCP connection and then says nothing parks the caller for
# good, with no way back short of a restart. pyld's resolved-context LRU
# (100 entries, per-process, cold after every restart) does not help, since a
# novel URL is a guaranteed miss.
_LOADER_TIMEOUT = (3.05, 10)

requests_loader = pyld.documentloader.requests.requests_document_loader(
    timeout=_LOADER_TIMEOUT
)


def _loader(url, options={}):
    # See https://github.com/digitalbazaar/pyld/issues/133
    options["headers"]["Accept"] = "application/ld+json"

    # XXX: temp fix/hack is it seems to be down for now
    if url == "https://w3id.org/identity/v1":
        url = (
            "https://raw.githubusercontent.com/web-payments/web-payments.org"
            "/master/contexts/identity-v1.jsonld"
        )

    # The same SSRF guard every other remote fetch in the app goes through.
    # This URL is attacker-supplied, so without it a crafted `@context` turns
    # signature verification into a probe of the host's private network.
    check_url(url)

    return requests_loader(url, options)


pyld.jsonld.set_document_loader(_loader)


def _options_hash(doc: ap.RawObject) -> str:
    doc = dict(doc["signature"])
    for k in ["type", "id", "signatureValue"]:
        if k in doc:
            del doc[k]
    doc["@context"] = "https://w3id.org/security/v1"
    normalized = jsonld.normalize(
        doc, {"algorithm": "URDNA2015", "format": "application/nquads"}
    )
    h = hashlib.new("sha256")
    h.update(normalized.encode("utf-8"))
    return h.hexdigest()


def _doc_hash(doc: ap.RawObject) -> str:
    doc = dict(doc)
    if "signature" in doc:
        del doc["signature"]
    normalized = jsonld.normalize(
        doc, {"algorithm": "URDNA2015", "format": "application/nquads"}
    )
    h = hashlib.new("sha256")
    h.update(normalized.encode("utf-8"))
    return h.hexdigest()


async def doc_hash_async(doc: ap.RawObject) -> str:
    """`_doc_hash` off the event loop.

    Normalization is pure CPU, but resolving `@context` reaches out over the
    network with `requests`, which is blocking. On an event loop that stalls
    every other request in the process (uvicorn runs a single one), so callers
    in async code must go through this.
    """
    return await asyncio.to_thread(_doc_hash, doc)


async def verify_signature(
    db_session: AsyncSession,
    doc: ap.RawObject,
) -> bool:
    if "signature" not in doc:
        logger.warning("The object does contain a signature")
        return False

    if "actor" not in doc:
        logger.warning("The object does not contain an actor")
        return False

    key_id = doc["signature"]["creator"]
    key = await _get_public_key(db_session, key_id)

    # Ensure the signing key actually belongs to the activity's actor, otherwise
    # anyone with a valid key could sign an object claiming to be from any actor
    # (actor impersonation / forgery of forwarded activities).
    actor_id = ap.get_id(doc["actor"])
    if key.owner != actor_id:
        logger.warning(
            f"LD sig key owner {key.owner!r} does not match actor {actor_id!r}"
        )
        return False

    # Both normalize + dereference `@context`, so keep them off the loop.
    options_hash = await asyncio.to_thread(_options_hash, doc)
    doc_hash = await asyncio.to_thread(_doc_hash, doc)

    to_be_signed = options_hash + doc_hash
    signature = doc["signature"]["signatureValue"]
    signer = PKCS1_v1_5.new(key.pubkey or key.privkey)  # type: ignore
    digest = SHA256.new()
    digest.update(to_be_signed.encode("utf-8"))
    return signer.verify(digest, base64.b64decode(signature))  # type: ignore


def generate_signature(doc: ap.RawObject, key: "Key") -> None:
    options = {
        "type": "RsaSignature2017",
        "creator": doc["actor"] + "#main-key",
        "created": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
    }
    doc["signature"] = options
    to_be_signed = _options_hash(doc) + _doc_hash(doc)
    if not key.privkey:
        raise ValueError(f"missing privkey on key {key!r}")

    signer = PKCS1_v1_5.new(key.privkey)
    digest = SHA256.new()
    digest.update(to_be_signed.encode("utf-8"))
    sig = base64.b64encode(signer.sign(digest))  # type: ignore
    options["signatureValue"] = sig.decode("utf-8")
