"""A live test of a mint's locks: a few of the Operator's own sats, locked and spent back.

A mint's ``/v1/info`` only says it supports NUT-11. This test shows what the
mint does with a lock:

1. **Fund.** The Operator pays an invoice. The sats are minted into two locks,
   each 2-of-3 over three throwaway keys A, B and C, with A as the refund key:
   - *multisig*, whose refund date is a month away;
   - *refund*, whose refund date has already passed.
2. **Spend.** The keys sign, and this module submits:
   - the multisig lock with B's signature alone: the mint must **refuse** it;
   - the multisig lock with B's and C's: the mint must take it;
   - the refund lock with A's alone: the mint must take it, as the refund date has passed.
   What each spend makes is locked to A alone.
3. **Return.** A signs a melt of all of it to the Operator's own payout
   address, less the mint's Lightning fee reserve. What the reserve does not
   use comes back as change locked to A, and stays at the mint.

The throwaway keys are made and kept by the page that runs the test. This
module never holds a private key: it builds every message, checks every
signature, submits, and keeps the result as a fact about the mint.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from tollbooth import bolt11, cashu

from taskbooth_mcp.escrow import InvoiceFor, MintFor, digest, lock_key, parts
from taskbooth_mcp.store import checked_npub

LEAST_SATS = 20
MOST_SATS = 1000
DEFAULT_SATS = 100
LOCK_DAYS = 30
#: How long ago the refund lock's refund date is set.
PAST_SECONDS = 600

INVOICED = "invoiced"
FUNDED = "funded"
SPENT = "spent"
DONE = "done"
STOPPED = "stopped"


def checked_keys(npubs: Any) -> list[str]:
    """Three distinct npubs, the test's keys A, B and C, or ``ValueError``."""
    if not isinstance(npubs, list) or len(npubs) != 3 or not all(isinstance(n, str) for n in npubs):
        raise ValueError("Name three throwaway npubs: the test's keys A, B and C.")
    keys = [checked_npub(n.strip(), "key") for n in npubs]
    if len(set(keys)) != 3:
        raise ValueError("The three keys must differ.")
    return keys


def checked_sats(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not LEAST_SATS <= value <= MOST_SATS:
        raise ValueError(f"A test locks {LEAST_SATS} to {MOST_SATS} sats.")
    return value


def _lock(keys: list[str], locktime: int) -> str:
    a, b, c = (lock_key(k) for k in keys)
    return cashu.p2pk_secret(a, pubkeys=[b, c], n_sigs=2, locktime=locktime, refund=[a])


def _total(proofs: list[dict[str, Any]]) -> int:
    return sum(int(p["amount"]) for p in proofs)


def _keys_of(record: dict[str, Any]) -> dict[int, str]:
    return {int(a): k for a, k in record["keyset"]["keys"].items()}


def _pending(stored: dict[str, Any]) -> cashu.Pending:
    return cashu.Pending(outputs=stored["outputs"], secrets=stored["secrets"], rs=stored["rs"])


def _item(npub: str, what: str, kind: str, message: str | bytes, inputs: list[dict[str, Any]],
          outputs: list[dict[str, Any]], quote: str = "", most: int = 0) -> dict[str, Any]:
    return {"npub": npub, "what": what, "sign_digest": digest(message), "most": most,
            "parts": parts(kind, inputs, outputs, quote)}


def summary(record: dict[str, Any]) -> dict[str, Any]:
    """The test as a page shows it: no proofs, no blinding factors."""
    keep = ("test_id", "mint", "sats", "state", "started_at", "finished_at", "checks", "returned_sats",
            "change_sats", "invoice", "expires_at", "stopped")
    return {k: record[k] for k in keep if k in record}


def to_sign(record: dict[str, Any]) -> list[dict[str, Any]]:
    """What the page's throwaway keys sign next."""
    a, b, c = record["keys"]
    if record["state"] == INVOICED:
        f = record["funding"]
        return [_item(a, "fund", "mint_quote", bytes.fromhex(f["message_hex"]), [], f["pending"]["outputs"],
                      f["quote"], most=record["sats"])]
    if record["state"] == FUNDED:
        m, r = record["spend"]["multisig"], record["spend"]["refund"]
        held_m, held_r = _total(record["proofs"]["multisig"]), _total(record["proofs"]["refund"])
        return [
            _item(b, "proposal", "swap", m["message"], record["proofs"]["multisig"], m["pending"]["outputs"], most=held_m),
            _item(c, "proposal", "swap", m["message"], record["proofs"]["multisig"], m["pending"]["outputs"], most=held_m),
            _item(a, "proposal", "swap", r["message"], record["proofs"]["refund"], r["pending"]["outputs"], most=held_r),
        ]
    if record["state"] == SPENT and record.get("melt"):
        melt = record["melt"]
        return [_item(a, "collect", "melt", melt["message"], record["held"], melt["pending"]["outputs"],
                      melt["quote"], most=melt["amount"])]
    return []


async def start(mint_for: MintFor, mint: str, npubs: Any, sats: Any, *, at: int, test_id: str) -> dict[str, Any]:
    """Check the mint's NUTs, then the invoice that funds the test. Nothing is spent if the NUTs fall short."""
    keys, sats = checked_keys(npubs), checked_sats(sats)
    url = cashu.checked_mint_url(mint) if isinstance(mint, str) else ""
    if not url:
        raise ValueError("Name the mint by its https address.")
    client = mint_for(url)
    record: dict[str, Any] = {"test_id": test_id, "mint": url, "sats": sats, "keys": keys,
                              "started_at": at, "checks": {}}
    try:
        missing = cashu.missing_escrow_nuts(await client.info())
        if missing:
            return {**record, "state": STOPPED, "finished_at": at,
                    "checks": {"nuts": f"missing NUT {', '.join(missing)}"},
                    "stopped": "The mint does not claim every NUT an escrow needs, so no sats were locked."}
        keyset = await client.active_keyset()
        quote = await client.mint_quote(sats, lock_key(keys[0]))
    except cashu.CashuError as exc:
        raise ValueError(f"The mint refused: {exc}") from None
    half = sats // 2
    plan = [("multisig", half, at + LOCK_DAYS * 86_400), ("refund", sats - half, at - PAST_SECONDS)]
    amounts: list[int] = []
    secrets: list[str] = []
    split: dict[str, int] = {}
    for name, amount, locktime in plan:
        pieces = cashu.amount_split(amount)
        split[name] = len(pieces)
        amounts += pieces
        secrets += [_lock(keys, locktime) for _ in pieces]
    pending = cashu.outputs_for(amounts, secrets, keyset["id"])
    return {**record, "state": INVOICED, "checks": {"nuts": "ok"},
            "keyset": {"id": keyset["id"], "fee_ppk": keyset["input_fee_ppk"],
                       "keys": {str(a): k for a, k in keyset["keys"].items()}},
            "invoice": quote["request"], "expires_at": int(quote.get("expiry") or 0),
            "funding": {"quote": quote["quote"], "pending": asdict(pending), "split": split,
                        "message_hex": cashu.mint_quote_message(quote["quote"], pending.outputs).hex()}}


def _signature(signatures: dict[str, str], npub: str, message: str | bytes) -> str:
    sig = signatures.get(npub, "")
    sig = sig.strip().lower() if isinstance(sig, str) else ""
    if len(sig) != 128 or not cashu.schnorr_verify(lock_key(npub), message, sig):
        raise ValueError("A signature is missing, or is not over its sign_digest by its key.")
    return sig


def _to_a(record: dict[str, Any], proofs: list[dict[str, Any]]) -> dict[str, Any]:
    """A swap of ``proofs`` into outputs locked to key A alone, less the mint's input fee."""
    net = _total(proofs) - cashu.input_fee(proofs, int(record["keyset"]["fee_ppk"]))
    if net < 1:
        raise ValueError("The mint's fee would take the whole lock; test with more sats.")
    pieces = cashu.amount_split(net)
    pending = cashu.outputs_for(pieces, [cashu.p2pk_secret(lock_key(record["keys"][0])) for _ in pieces],
                                record["keyset"]["id"])
    return {"pending": asdict(pending), "message": cashu.swap_message(proofs, pending.outputs)}


async def _funded(client: cashu.MintClient, record: dict[str, Any], signatures: dict[str, str]) -> dict[str, Any]:
    funding = record["funding"]
    sig = _signature(signatures, record["keys"][0], bytes.fromhex(funding["message_hex"]))
    state = str((await client.mint_quote_state(funding["quote"])).get("state", "")).upper()
    if state not in ("PAID", "ISSUED"):
        raise ValueError("The test invoice is not paid yet. Pay it, then sign again.")
    pending = _pending(funding["pending"])
    made = cashu.proofs_from(await client.mint(funding["quote"], pending.outputs, sig), pending, _keys_of(record))
    cut = int(funding["split"]["multisig"])
    proofs = {"multisig": made[:cut], "refund": made[cut:]}
    spend = {name: _to_a(record, group) for name, group in proofs.items()}
    return {**{k: v for k, v in record.items() if k not in ("funding", "invoice", "expires_at")},
            "state": FUNDED, "proofs": proofs, "spend": spend, "checks": {**record["checks"], "fund": "ok"}}


async def _attempt(client: cashu.MintClient, record: dict[str, Any], name: str,
                   sigs: list[str]) -> tuple[list[dict[str, Any]] | None, str]:
    """Submit one spend; the proofs it made, or ``None`` and what the mint said."""
    spend = record["spend"][name]
    pending = _pending(spend["pending"])
    try:
        promises = await client.swap(cashu.with_witness(record["proofs"][name], sigs), pending.outputs)
    except cashu.CashuError as exc:
        return None, str(exc)[:160]
    return cashu.proofs_from(promises, pending, _keys_of(record)), ""


async def _spent(client: cashu.MintClient, record: dict[str, Any], signatures: dict[str, str]) -> dict[str, Any]:
    a, b, c = record["keys"]
    m, r = record["spend"]["multisig"], record["spend"]["refund"]
    sig_b, sig_c = _signature(signatures, b, m["message"]), _signature(signatures, c, m["message"])
    sig_a = _signature(signatures, a, r["message"])
    checks = dict(record["checks"])
    held: list[dict[str, Any]] = []

    alone, said = await _attempt(client, record, "multisig", [sig_b])
    if alone is None:
        checks["one_signature"] = "refused, as it must be"
        both, said = await _attempt(client, record, "multisig", [sig_b, sig_c])
        checks["two_signatures"] = "ok" if both is not None else f"refused: {said}"
        held += both or []
    else:
        checks["one_signature"] = "TAKEN: the mint let one of three signatures spend a 2-of-3 lock"
        checks["two_signatures"] = "not tried: the one-signature spend already moved the sats"
        held += alone
    back, said = await _attempt(client, record, "refund", [sig_a])
    checks["refund"] = "ok" if back is not None else f"refused: {said}"
    held += back or []

    record = {**{k: v for k, v in record.items() if k not in ("proofs", "spend")},
              "state": SPENT, "held": held, "checks": checks}
    if not held:
        return {**record, "state": DONE, "returned_sats": 0,
                "stopped": "The mint took none of the spends, so the sats stay in their locks."}
    return record


async def _melt_plan(client: cashu.MintClient, invoice_for: InvoiceFor, address: str,
                     record: dict[str, Any]) -> dict[str, Any]:
    held = record["held"]
    available = _total(held) - cashu.input_fee(held, int(record["keyset"]["fee_ppk"]))
    amount = available
    try:
        for _ in range(3):
            if amount < 1:
                raise ValueError("What the test holds would all go to Lightning fees; test with more sats.")
            invoice = await invoice_for(address, amount)
            if bolt11.decode(invoice).amount_msat != amount * 1000:
                raise ValueError("Your payout address gave an invoice for a different amount.")
            quote = await client.melt_quote(invoice)
            reserve = int(quote.get("fee_reserve") or 0)
            if amount + reserve <= available:
                break
            amount = available - reserve
        else:
            raise ValueError("The mint's fee kept moving; sign again to retry.")
    except bolt11.Bolt11Error as exc:
        raise ValueError(f"Your payout address gave no usable invoice: {exc}") from None
    blanks = cashu.blank_outputs(cashu.blanks_needed(reserve), lambda: cashu.p2pk_secret(lock_key(record["keys"][0])),
                                 record["keyset"]["id"])
    return {"quote": quote["quote"], "amount": amount, "fee_reserve": reserve, "address": address,
            "pending": asdict(blanks), "message": cashu.melt_message(held, blanks.outputs, quote["quote"])}


async def _returned(client: cashu.MintClient, record: dict[str, Any], signatures: dict[str, str],
                    at: int) -> dict[str, Any]:
    melt = record["melt"]
    sig = _signature(signatures, record["keys"][0], melt["message"])
    blanks = _pending(melt["pending"])
    paid = await client.melt(melt["quote"], cashu.with_witness(record["held"], [sig]), blanks.outputs)
    if str(paid.get("state", "")).upper() != "PAID":
        raise ValueError("The mint has not finished paying yet. Sign again in a minute to see.")
    change = cashu.proofs_from(paid.get("change") or [], blanks, _keys_of(record)) if paid.get("change") else []
    return {**{k: v for k, v in record.items() if k not in ("held", "melt")}, "state": DONE, "finished_at": at,
            "checks": {**record["checks"], "melt": "ok"}, "returned_sats": int(melt["amount"]),
            "change_sats": _total(change), "change": change}


async def advance(mint_for: MintFor, invoice_for: InvoiceFor, address: str, record: dict[str, Any],
                  signatures: Any, *, at: int) -> dict[str, Any]:
    """Take the keys' signatures for the current step and carry it out.

    Once the spends are done, a call with no signatures readies the return to
    ``address`` (and readies it afresh if its quote went stale); the next call
    carries A's signature on it.
    """
    if not isinstance(signatures, dict) or not all(isinstance(k, str) and isinstance(v, str)
                                                   for k, v in signatures.items()):
        raise ValueError("Pass the signatures as {npub: signature}.")
    client = mint_for(record["mint"])
    try:
        if record["state"] == INVOICED:
            return await _funded(client, record, signatures)
        if record["state"] == FUNDED:
            return await _spent(client, record, signatures)
        if record["state"] == SPENT and record.get("melt") and signatures:
            return await _returned(client, record, signatures, at)
        if record["state"] == SPENT:
            if not address:
                raise ValueError("Set your payout_address, so the test's sats can come back to you.")
            return {**record, "melt": await _melt_plan(client, invoice_for, address, record)}
    except cashu.CashuError as exc:
        raise ValueError(f"The mint refused: {exc}") from None
    raise ValueError("This test is finished.")


def verdict(record: dict[str, Any]) -> str:
    """``passed``, ``failed`` or ``unfinished``, from the checks alone."""
    checks = record.get("checks") or {}
    if record.get("state") not in (DONE, STOPPED):
        return "unfinished"
    good = (checks.get("nuts") == "ok" and checks.get("one_signature", "").startswith("refused")
            and checks.get("two_signatures") == "ok" and checks.get("refund") == "ok")
    return "passed" if good else "failed"
