"""A job's Balance held in escrow at a Cashu mint, with no one needing a Cashu wallet.

Only a job whose cover page names an arbiter and asks for escrow has one. The
Balance (the top grade less the deposit and milestones, which stay wallet to
wallet) is locked to the three parties' own Nostr keys:

- any two of purchaser, performer and arbiter may move it;
- after the refund date the purchaser alone may take it back.

How the sats move:

1. In. The purchaser asks for an invoice (:func:`open_invoice`). It is bound to
   their key (NUT-20), so once paid from any Lightning wallet it mints into the
   lock only with their signature.
2. A split. Any party proposes how the escrow divides between performer and
   purchaser (:func:`propose`); for a graded job the default is the award. Two
   signatures, or the purchaser's alone after the refund date for a full
   refund, swap the escrow into two groups, each locked to its recipient.
3. Out. Each recipient collects their group (:func:`collect`): it is melted
   to an invoice from their own payout address, signed by them alone. What the
   mint's fee reserve does not use comes back locked to them, to collect later.

The Operator builds every message and submits it. It holds blinding factors
and locked proofs only, and no key in any lock, so it can never move the sats.
Every signature is a BIP-340 signature by the person's Nostr key over the
32-byte ``sign_digest`` this module hands out.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from pynostr.key import PublicKey
from tollbooth import bolt11, cashu

from taskbooth_mcp.lifecycle import Action, Role, State, deposit_sats

INVOICED = "invoiced"
FUNDED = "funded"
PROPOSED = "proposed"
SPLIT = "split"
SETTLED = "settled"
#: States in which the escrow holds, or held, the client's sats.
FUNDED_STATES = (FUNDED, PROPOSED, SPLIT, SETTLED)
PARTIES = ("purchaser", "performer", "arbiter")
#: Room left on an invoice before the mint must have paid it.
QUOTE_MARGIN_SECONDS = 60

MintFor = Callable[[str], cashu.MintClient]
InvoiceFor = Callable[[str, int], Awaitable[str]]


# ---------------------------------------------------------------------------
# Pure pieces
# ---------------------------------------------------------------------------


def lock_key(npub: str) -> str:
    """An npub as the key a Cashu lock names."""
    return cashu.lock_key(str(PublicKey.from_npub(npub).hex()))


def digest(message: str | bytes) -> str:
    """The 32 bytes a signer signs: SHA-256 of the message."""
    raw = message.encode("utf-8") if isinstance(message, str) else message
    return hashlib.sha256(raw).hexdigest()


def balance_sats(task: dict[str, Any]) -> int:
    """The top grade less the deposit and milestones: as fixed at the claim, or as they will be."""
    top = max(int(g["sats"]) for g in task["rubric"]["grades"])
    deposit = task.get("deposit") or {}
    ahead = int(deposit["sats"]) if "sats" in deposit else deposit_sats(top, int(task.get("deposit_pct") or 0))
    ahead += sum(int(m["sats"]) if "sats" in m else top * int(m["pct"]) // 100 for m in task.get("milestones") or [])
    return max(0, top - ahead)


def escrow_secret(parties: dict[str, str], locktime: int) -> str:
    """Purchaser, performer and arbiter, any two; after ``locktime`` the purchaser alone."""
    purchaser = lock_key(parties["purchaser"])
    return cashu.p2pk_secret(
        purchaser,
        pubkeys=[lock_key(parties["performer"]), lock_key(parties["arbiter"])],
        n_sigs=2,
        locktime=locktime,
        refund=[purchaser],
    )


def recipient_secret(npub: str) -> str:
    """Locked to one person; SIG_ALL so their signature covers where it goes."""
    return cashu.p2pk_secret(lock_key(npub))


def _total(proofs: list[dict[str, Any]]) -> int:
    return sum(int(p["amount"]) for p in proofs)


def _keys(record: dict[str, Any]) -> dict[int, str]:
    return {int(a): k for a, k in record["keyset"]["keys"].items()}


def _pending(stored: dict[str, Any]) -> cashu.Pending:
    return cashu.Pending(outputs=stored["outputs"], secrets=stored["secrets"], rs=stored["rs"])


def roles_in(record: dict[str, Any], npub: str) -> list[str]:
    return [role for role in PARTIES if record["parties"].get(role) == npub]


def escrow_summary(record: dict[str, Any], at: int) -> dict[str, Any]:
    """Where an escrow stands, as anyone who may see the task reads it."""
    view: dict[str, Any] = {
        "state": record["state"],
        "mint": record["mint"],
        "sats": record["sats"],
        "parties": record["parties"],
        "refund_at": record["locktime"],
        "refund_open": at >= int(record["locktime"]),
    }
    if record["state"] == INVOICED:
        view["invoice"] = {k: record["funding"][k] for k in ("invoice", "expires_at")}
    if record.get("funded_at"):
        view["funded_at"] = record["funded_at"]
        view["held_sats"] = _total(record.get("proofs") or [])
    proposal = record.get("proposal")
    if proposal:
        view["proposal"] = {k: proposal[k] for k in ("to_performer", "to_purchaser", "by", "at")}
        view["proposal"]["signed_by"] = sorted(proposal.get("signatures") or proposal.get("signed_by") or [])
    groups = record.get("groups") or {}
    if groups:
        view["to_collect"] = {role: _total(proofs) for role, proofs in groups.items()}
    if record.get("paid"):
        view["paid"] = record["paid"]
    return view


def _pending_for(record: dict[str, Any], npub: str) -> list[dict[str, Any]]:
    """What ``npub`` may sign now, most urgent first."""
    mine = roles_in(record, npub)
    out = []
    for role in mine:
        pending = (record.get("collect") or {}).get(role)
        if pending:
            out.append({"what": "collect", "role": role, "sats": pending["amount"],
                        "fee_reserve": pending["fee_reserve"], "sign_digest": digest(pending["message"])})
    if record["state"] == INVOICED and "purchaser" in mine:
        out.append({"what": "fund", "role": "purchaser", "sats": record["sats"],
                     "sign_digest": digest(bytes.fromhex(record["funding"]["message_hex"]))})
    proposal = record.get("proposal")
    if record["state"] == PROPOSED and proposal:
        for role in mine:
            if role not in proposal["signatures"]:
                out.append({"what": "proposal", "role": role, "to_performer": proposal["to_performer"],
                            "to_purchaser": proposal["to_purchaser"], "sign_digest": digest(proposal["message"])})
    return out


def _checked_signature(record_key: str, message: str | bytes, signature: Any) -> str:
    sig = signature.strip().lower() if isinstance(signature, str) else ""
    if len(sig) != 128 or any(c not in "0123456789abcdef" for c in sig):
        raise ValueError("A signature is 64 bytes of BIP-340 Schnorr, in 128 hex characters.")
    if not cashu.schnorr_verify(record_key, message, sig):
        raise ValueError("That signature is not over this sign_digest by your Nostr key. Read it again with "
                         "taskbooth_escrow_status.")
    return sig


def _mint_says(exc: cashu.CashuError) -> ValueError:
    return ValueError(f"The mint refused: {exc}")


# ---------------------------------------------------------------------------
# The moves
# ---------------------------------------------------------------------------


async def status(db: Any, npub: str, task_id: str, at: int) -> dict[str, Any]:
    task, _ = await db.escrow_task(npub, task_id)
    record = task.get("escrow")
    if not record:
        cover = task.get("cover") or {}
        return {"escrow": None, "offered": bool(cover.get("escrow")), "sats": balance_sats(task)}
    return {"escrow": escrow_summary(record, at), "for_you": _pending_for(record, npub)}


async def open_invoice(db: Any, mint_for: MintFor, npub: str, task_id: str, at: int) -> dict[str, Any]:
    """The invoice that funds a claimed job's escrow, and what the purchaser signs once it is paid."""
    task, roles = await db.escrow_task(npub, task_id)
    cover = task.get("cover") or {}
    if not cover.get("escrow"):
        raise ValueError("This job is not held in escrow.")
    if Role.PURCHASER not in roles:
        raise ValueError("Only the purchaser funds the escrow.")
    if task["state"] != State.CLAIMED:
        raise ValueError("The escrow is funded once the job is claimed and before it is marked done.")
    signed = task.get("agreement") or {}
    missing = [role for role in PARTIES if not signed.get(role)]
    if missing:
        raise ValueError(f"Everyone signs the offer first; still to sign: {', '.join(missing)}.")
    if task["performer"] == task["purchaser"]:
        raise ValueError("A job you perform yourself has no one to hold escrow against.")
    record = task.get("escrow")
    if record and record["state"] != INVOICED:
        raise ValueError("This job's escrow is already funded.")
    if record and int(record["funding"]["expires_at"]) > at + QUOTE_MARGIN_SECONDS:
        return _handed(record)
    sats = balance_sats(task)
    if sats < 1:
        raise ValueError("Nothing is left of the price to hold: the deposit and milestones cover it.")

    client = mint_for(cover["escrow_mint"])
    try:
        missing_nuts = cashu.missing_escrow_nuts(await client.info())
        if missing_nuts:
            raise ValueError(f"The escrow mint lacks what escrow needs (NUT {', '.join(missing_nuts)}).")
        keyset = await client.active_keyset()
        parties = {"purchaser": task["purchaser"], "performer": task["performer"], "arbiter": cover["arbiter_npub"]}
        quote = await client.mint_quote(sats, lock_key(task["purchaser"]))
    except cashu.CashuError as exc:
        raise _mint_says(exc) from None
    locktime = int(task["needed_by"]) + int(cover["refund_days"]) * 86400
    amounts = cashu.amount_split(sats)
    pending = cashu.outputs_for(amounts, [escrow_secret(parties, locktime) for _ in amounts], keyset["id"])
    message = cashu.mint_quote_message(quote["quote"], pending.outputs)
    record = {
        "state": INVOICED, "mint": cover["escrow_mint"], "sats": sats, "parties": parties, "locktime": locktime,
        "keyset": {"id": keyset["id"], "fee_ppk": keyset["input_fee_ppk"],
                   "keys": {str(a): k for a, k in keyset["keys"].items()}},
        "funding": {"quote": quote["quote"], "invoice": quote["request"], "expires_at": int(quote.get("expiry") or 0),
                    "pending": asdict(pending), "message_hex": message.hex()},
    }
    await db.save_escrow(task, record, actor=npub, action=Action.ESCROW_INVOICE,
                         detail={"sats": sats, "mint": record["mint"]}, at=at)
    return _handed(record)


def _handed(record: dict[str, Any]) -> dict[str, Any]:
    funding = record["funding"]
    return {"invoice": funding["invoice"], "sats": record["sats"], "expires_at": funding["expires_at"],
            "mint": record["mint"], "refund_at": record["locktime"],
            "sign_digest": digest(bytes.fromhex(funding["message_hex"]))}


async def propose(db: Any, npub: str, task_id: str, to_performer: int | None, at: int) -> dict[str, Any]:
    """Propose how the escrow divides. ``None`` proposes the award of a graded job."""
    task, _ = await db.escrow_task(npub, task_id)
    record = task.get("escrow")
    if not record or record["state"] not in (FUNDED, PROPOSED):
        raise ValueError("There is no funded escrow on this job to divide.")
    mine = roles_in(record, npub)
    if not mine:
        raise ValueError("Only the purchaser, the performer or the arbiter may propose a split.")
    proofs = record["proofs"]
    net = _total(proofs) - cashu.input_fee(proofs, int(record["keyset"]["fee_ppk"]))
    if to_performer is None:
        graded = task.get("assessment") or {}
        if not task["final"] or "sats" not in graded:
            raise ValueError("Name the sats for the performer; the job has no final grade to award yet.")
        to_performer = min(int(graded["sats"]), net)
    if not isinstance(to_performer, int) or isinstance(to_performer, bool) or not 0 <= to_performer <= net:
        raise ValueError(f"The performer's share is a whole number of sats from 0 to {net}.")
    shares = [(record["parties"]["performer"], to_performer), (record["parties"]["purchaser"], net - to_performer)]
    amounts, secrets = [], []
    for npub_to, sats in shares:
        for piece in cashu.amount_split(sats):
            amounts.append(piece)
            secrets.append(recipient_secret(npub_to))
    pending = cashu.outputs_for(amounts, secrets, record["keyset"]["id"])
    message = cashu.swap_message(proofs, pending.outputs)
    record = {**record, "state": PROPOSED, "proposal": {
        "to_performer": to_performer, "to_purchaser": net - to_performer, "by": mine[0], "at": at,
        "performer_outputs": len(cashu.amount_split(to_performer)),
        "pending": asdict(pending), "message": message, "signatures": {},
    }}
    await db.save_escrow(task, record, actor=npub, action=Action.ESCROW_PROPOSE,
                         detail={"to_performer": to_performer, "to_purchaser": net - to_performer}, at=at)
    return {"proposal": escrow_summary(record, at)["proposal"], "sign_digest": digest(message),
            "needs": "two of purchaser, performer and arbiter; or the purchaser alone after the refund date "
                     "for a full refund"}


async def collect(db: Any, mint_for: MintFor, invoice_for: InvoiceFor, npub: str, task_id: str, address: str,
                  at: int) -> dict[str, Any]:
    """Get an invoice from ``npub``'s payout address for their share, and what they sign to be paid."""
    task, _ = await db.escrow_task(npub, task_id)
    record = task.get("escrow")
    groups = (record or {}).get("groups") or {}
    mine = [role for role in roles_in(record or {"parties": {}}, npub) if groups.get(role)]
    if not record or not mine:
        raise ValueError("Nothing in this job's escrow is waiting for you to collect.")
    if not address:
        raise ValueError("Set your payout_address (a Lightning address) with taskbooth_set_my_property first.")
    role = mine[0]
    group = groups[role]
    available = _total(group) - cashu.input_fee(group, int(record["keyset"]["fee_ppk"]))
    if available < 1:
        raise ValueError("What is left is smaller than the mint's fee to move it.")
    client = mint_for(record["mint"])
    amount = available
    try:
        for _ in range(3):
            invoice = await invoice_for(address, amount)
            asked = bolt11.decode(invoice)
            if asked.amount_msat != amount * 1000:
                raise ValueError("Your payout address gave an invoice for a different amount.")
            quote = await client.melt_quote(invoice)
            reserve = int(quote.get("fee_reserve") or 0)
            if amount + reserve <= available:
                break
            amount = available - reserve
            if amount < 1:
                raise ValueError("What is left would all go to Lightning fees.")
        else:
            raise ValueError("The mint's fee kept moving; try again.")
    except cashu.CashuError as exc:
        raise _mint_says(exc) from None
    except bolt11.Bolt11Error as exc:
        raise ValueError(f"Your payout address gave no usable invoice: {exc}") from None
    blanks = cashu.blank_outputs(cashu.blanks_needed(reserve), lambda: recipient_secret(npub), record["keyset"]["id"])
    message = cashu.melt_message(group, blanks.outputs, quote["quote"])
    record = {**record, "collect": {**(record.get("collect") or {}), role: {
        "quote": quote["quote"], "amount": amount, "fee_reserve": reserve, "address": address,
        "pending": asdict(blanks), "message": message,
    }}}
    await db.save_escrow(task, record, actor=npub, action=Action.ESCROW_COLLECT,
                         detail={"role": role, "sats": amount, "fee_reserve": reserve}, at=at)
    return {"sats": amount, "fee_reserve": reserve, "address": address, "sign_digest": digest(message)}


async def sign(db: Any, mint_for: MintFor, npub: str, task_id: str, signature: str, at: int) -> dict[str, Any]:
    """Take ``npub``'s signature on whatever they have pending, and carry it out once it is enough."""
    task, _ = await db.escrow_task(npub, task_id)
    record = task.get("escrow")
    if not record:
        raise ValueError("This job has no escrow.")
    pending = _pending_for(record, npub)
    if not pending:
        raise ValueError("Nothing in this job's escrow is waiting for your signature.")
    first = pending[0]
    key = lock_key(npub)
    client = mint_for(record["mint"])
    try:
        if first["what"] == "collect":
            return await _paid_out(db, client, task, record, npub, first["role"], key, signature, at)
        if first["what"] == "fund":
            return await _funded(db, client, task, record, npub, key, signature, at)
        return await _signed(db, client, task, record, npub, first["role"], key, signature, at)
    except cashu.CashuError as exc:
        raise _mint_says(exc) from None


async def _funded(db: Any, client: cashu.MintClient, task: dict[str, Any], record: dict[str, Any], npub: str,
                  key: str, signature: str, at: int) -> dict[str, Any]:
    funding = record["funding"]
    sig = _checked_signature(key, bytes.fromhex(funding["message_hex"]), signature)
    state = str((await client.mint_quote_state(funding["quote"])).get("state", "")).upper()
    if state not in ("PAID", "ISSUED"):
        raise ValueError("The escrow invoice is not paid yet. Pay it, then sign again.")
    pending = _pending(funding["pending"])
    proofs = cashu.proofs_from(await client.mint(funding["quote"], pending.outputs, sig), pending, _keys(record))
    record = {**record, "state": FUNDED, "proofs": proofs, "funded_at": at,
              "funding": {k: funding[k] for k in ("quote", "invoice", "expires_at")}}
    await db.keep_escrow(task["task_id"], record, actor=npub, action=Action.ESCROW_FUND,
                         detail={"sats": _total(proofs)}, at=at)
    return {"funded": True, "escrow": escrow_summary(record, at)}


async def _signed(db: Any, client: cashu.MintClient, task: dict[str, Any], record: dict[str, Any], npub: str,
                  role: str, key: str, signature: str, at: int) -> dict[str, Any]:
    proposal = record["proposal"]
    sig = _checked_signature(key, proposal["message"], signature)
    signatures = {**proposal["signatures"], role: sig}
    refund_alone = (set(signatures) == {"purchaser"} and proposal["to_performer"] == 0
                    and at >= int(record["locktime"]))
    if len(signatures) < 2 and not refund_alone:
        record = {**record, "proposal": {**proposal, "signatures": signatures}}
        await db.save_escrow(task, record, actor=npub, action=Action.ESCROW_SIGN, detail={"role": role}, at=at)
        return {"split": False, "escrow": escrow_summary(record, at)}
    pending = _pending(proposal["pending"])
    witnessed = cashu.with_witness(record["proofs"], list(signatures.values()))
    made = cashu.proofs_from(await client.swap(witnessed, pending.outputs), pending, _keys(record))
    cut = int(proposal["performer_outputs"])
    groups = {"performer": made[:cut], "purchaser": made[cut:]}
    record = {**{k: v for k, v in record.items() if k != "proofs"}, "state": SPLIT,
              "groups": {r: g for r, g in groups.items() if g}, "split_at": at,
              "proposal": {k: v for k, v in proposal.items() if k not in ("pending", "message", "signatures")}
              | {"signed_by": sorted(signatures)}}
    await db.keep_escrow(task["task_id"], record, actor=npub, action=Action.ESCROW_SPLIT,
                         detail={"to_performer": proposal["to_performer"], "to_purchaser": proposal["to_purchaser"],
                                 "signed_by": sorted(signatures)}, at=at)
    return {"split": True, "escrow": escrow_summary(record, at)}


async def _paid_out(db: Any, client: cashu.MintClient, task: dict[str, Any], record: dict[str, Any], npub: str,
                    role: str, key: str, signature: str, at: int) -> dict[str, Any]:
    pending_collect = record["collect"][role]
    sig = _checked_signature(key, pending_collect["message"], signature)
    group = record["groups"][role]
    blanks = _pending(pending_collect["pending"])
    paid = await client.melt(pending_collect["quote"], cashu.with_witness(group, [sig]), blanks.outputs)
    if str(paid.get("state", "")).upper() != "PAID":
        raise ValueError("The mint has not finished paying yet. Sign again in a minute to see.")
    change = cashu.proofs_from(paid.get("change") or [], blanks, _keys(record)) if paid.get("change") else []
    groups = {**record["groups"], role: change}
    groups = {r: g for r, g in groups.items() if g}
    collect = {r: c for r, c in (record.get("collect") or {}).items() if r != role}
    total_paid = {**(record.get("paid") or {})}
    total_paid[role] = int(total_paid.get(role, 0)) + int(pending_collect["amount"])
    settled = not any(_total(g) for g in groups.values())
    record = {**record, "groups": groups, "collect": collect, "paid": total_paid,
              "state": SETTLED if settled else SPLIT}
    await db.keep_escrow(task["task_id"], record, actor=npub, action=Action.ESCROW_PAID_OUT,
                         detail={"role": role, "sats": pending_collect["amount"],
                                 "change": _total(change)}, at=at)
    if role == "performer" and task["state"] == State.OWED:
        await db.act(npub, task["task_id"], Action.RECORD_PAYMENT, as_operator=True, at=at)
    return {"paid": pending_collect["amount"], "to": pending_collect["address"],
            "escrow": escrow_summary(record, at)}
