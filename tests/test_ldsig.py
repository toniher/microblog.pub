import base64
from copy import deepcopy

import httpx
import pytest
from Crypto.Hash import SHA256
from Crypto.Signature import PKCS1_v1_5
from respx import MockRouter

from activitypub import activitypub as ap
from activitypub.tests import factories
from app import ldsig
from app.database import AsyncSession
from app.key import Key

_SAMPLE_CREATE = {
    "type": "Create",
    "actor": "https://microblog.pub",
    "object": {
        "type": "Note",
        "sensitive": False,
        "cc": ["https://microblog.pub/followers"],
        "to": ["https://www.w3.org/ns/activitystreams#Public"],
        "content": "<p>Hello world!</p>",
        "tag": [],
        "attributedTo": "https://microblog.pub",
        "published": "2018-05-21T15:51:59Z",
        "id": "https://microblog.pub/outbox/988179f13c78b3a7/activity",
        "url": "https://microblog.pub/note/988179f13c78b3a7",
    },
    "@context": ap.AS_EXTENDED_CTX,
    "published": "2018-05-21T15:51:59Z",
    "to": ["https://www.w3.org/ns/activitystreams#Public"],
    "cc": ["https://microblog.pub/followers"],
    "id": "https://microblog.pub/outbox/988179f13c78b3a7",
}


@pytest.mark.asyncio
async def test_linked_data_sig(
    async_db_session: AsyncSession,
    respx_mock: MockRouter,
) -> None:
    privkey, pubkey = factories.generate_key()
    ra = factories.RemoteActorFactory(
        base_url="https://microblog.pub",
        username="dev",
        public_key=pubkey,
    )
    k = Key(ra.ap_id, f"{ra.ap_id}#main-key")
    k.load(privkey)
    respx_mock.get(ra.ap_id).mock(return_value=httpx.Response(200, json=ra.ap_actor))

    doc = deepcopy(_SAMPLE_CREATE)

    ldsig.generate_signature(doc, k)
    assert (await ldsig.verify_signature(async_db_session, doc)) is True


@pytest.mark.asyncio
async def test_linked_data_sig_rejects_actor_impersonation(
    async_db_session: AsyncSession,
    respx_mock: MockRouter,
) -> None:
    # An attacker signs an activity with their own valid key but claims the
    # activity is authored by a different (victim) actor. The signature is
    # cryptographically valid for the attacker's key, but the key owner does
    # not match the claimed actor, so verification must fail.
    privkey, pubkey = factories.generate_key()
    attacker = factories.RemoteActorFactory(
        base_url="https://attacker.example",
        username="attacker",
        public_key=pubkey,
    )
    k = Key(attacker.ap_id, f"{attacker.ap_id}#main-key")
    k.load(privkey)
    respx_mock.get(attacker.ap_id).mock(
        return_value=httpx.Response(200, json=attacker.ap_actor)
    )

    # Build a genuinely valid RsaSignature2017 whose `creator` is the
    # attacker's key while `actor` stays the victim. Signing manually (rather
    # than via generate_signature, which derives creator from actor) so the
    # signature actually validates for the attacker's key — the only remaining
    # defense is the key-owner/actor binding.
    doc = deepcopy(_SAMPLE_CREATE)
    options = {
        "type": "RsaSignature2017",
        "creator": f"{attacker.ap_id}#main-key",
        "created": "2018-05-21T15:51:59Z",
    }
    doc["signature"] = options
    to_be_signed = ldsig._options_hash(doc) + ldsig._doc_hash(doc)
    signer = PKCS1_v1_5.new(k.privkey)
    digest = SHA256.new()
    digest.update(to_be_signed.encode("utf-8"))
    options["signatureValue"] = base64.b64encode(signer.sign(digest)).decode("utf-8")

    assert (await ldsig.verify_signature(async_db_session, doc)) is False
