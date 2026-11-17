"""Real signatures for tests: a signed reply plus an Operator witness, as tollbooth makes them."""

import json

from pynostr.event import Event
from pynostr.key import PrivateKey
from tollbooth.identity_proof import sign_signature_witness

#: The Operator that witnesses signed replies in tests.
OPERATOR = PrivateKey()
OPERATOR_NPUB = OPERATOR.public_key.bech32()


def dm_signature(signer: PrivateKey, digest: str, operator: PrivateKey = OPERATOR) -> dict:
    """``signer``'s signed reply over ``digest``, witnessed by ``operator``."""
    reply = Event(pubkey=signer.public_key.hex(), kind=4, content="agree?iv=x",
                  tags=[["p", operator.public_key.hex()]], created_at=1_800_000_000)
    reply.sign(signer.hex())
    witness = sign_signature_witness(
        operator.bech32(), signer_npub=signer.public_key.bech32(), statement_hash=digest,
        reply_event_id=reply.id, challenge_hash="cd" * 32,
    )
    return {"method": "dm_reply", "signer": signer.public_key.bech32(), "statement_hash": digest,
            "signed_at": reply.created_at, "reply_event": reply.to_dict(), "witness": json.loads(witness)}


async def agree(db, key: PrivateKey, task_id: str) -> dict:
    """Sign the task's offer as ``key``'s npub, as it stands now."""
    npub = key.public_key.bech32()
    digest = (await db.offer(npub, task_id))["statement_hash"]
    return await db.sign(npub, task_id, dm_signature(key, digest), operator_npub=OPERATOR_NPUB, at=1_800_000_005)
