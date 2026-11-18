"""Task 22, option 0, part 2: Lightning in and out, so no one needs a Cashu wallet.

In: the coordinator (standing in for the Operator) asks the mint for an invoice,
the client pays it from any Lightning wallet, and the coordinator mints straight
into the 2-of-3 escrow lock. The quote is bound to the client's key (NUT-20), so
the paid invoice can only be minted with the client's signature on the outputs.

Out: each party hands over a Lightning invoice. A payout that goes wholly to one
party is melted straight from the escrow to their invoice. A split by grade is
one swap signed by client and worker into two groups, each locked to its
recipient, and then each recipient signs the melt of their own group.

Each party's "own Lightning wallet" is simulated by an invoice from the same
test mint, whose quote turns PAID when the melt pays it.
"""

from __future__ import annotations

import asyncio
import hashlib
import time

from cashu.core.base import P2PKWitness, Proof, Unit
from cashu.core.p2pk import P2PKSecret
from cashu.core.secret import SecretKind, Tags
from cashu.core.split import amount_split
from cashu.wallet.v1_api import LedgerAPI
from cashu.wallet.wallet import Wallet
from spike import ESCROW, escrow_lock, fee, party, pub, sign

RESULTS: list[tuple[str, str, str]] = []


async def expect(label: str, path: str, coro, ok: bool):
    try:
        out = await coro
        RESULTS.append((label, path, "PASS" if ok else "FAIL (should have been refused)"))
        return out
    except Exception as exc:  # noqa: BLE001 — a refusal is a result to record
        reason = str(exc.args[0] if exc.args else exc)[:80]
        RESULTS.append((label, path, f"PASS (refused: {reason})" if not ok else f"FAIL: {reason}"))
        return None


def locked_outputs(coord: Wallet, amounts: list[int], lock: P2PKSecret):
    secs = [lock.serialize() for _ in amounts]  # serialize() draws a fresh nonce each time
    outs, rs = coord._construct_outputs(amounts, secs, [], coord.keyset_id)
    return outs, rs, secs


def to_recipient(recipient: str) -> P2PKSecret:
    return P2PKSecret(kind=SecretKind.P2PK.value, data=recipient, tags=Tags([["sigflag", "SIG_ALL"]]))


def nut20_message(quote: str, outs) -> bytes:
    def len32(b: bytes) -> bytes:
        return len(b).to_bytes(4, "big")

    msg = b"Cashu_MintQuoteSig_v1" + len32(quote.encode()) + quote.encode()
    for o in outs:
        amt = o.amount.to_bytes((o.amount.bit_length() + 7) // 8, "big")
        b_ = bytes.fromhex(o.B_)
        msg += len32(amt) + amt + len32(b_) + b_
    return msg


def nut20_message_legacy(quote: str, outs) -> bytes:
    return (quote + "".join(o.B_ for o in outs)).encode()


def sign_bytes(w: Wallet, msg: bytes) -> str:
    return w.private_key.sign_schnorr(hashlib.sha256(msg).digest(), None).hex()


async def wait_paid(coord: Wallet, quote: str, seconds: int = 30) -> str:
    for _ in range(seconds):
        state = (await LedgerAPI.get_mint_quote(coord, quote)).state
        if str(state).upper().split(".")[-1] in ("PAID", "ISSUED"):
            return str(state)
        await asyncio.sleep(1)
    return "UNPAID"


async def fund_by_lightning(coord: Wallet, client: Wallet, lock: P2PKSecret, sign_with: Wallet | None = None):
    """Lightning in: invoice → client pays → mint into the lock with the client's signature."""
    quote = await LedgerAPI.mint_quote(coord, ESCROW, Unit.sat, pubkey=pub(client))
    await wait_paid(coord, quote.quote)  # testnut pays its own invoices; a real client pays from any wallet
    outs, rs, secs = locked_outputs(coord, amount_split(ESCROW), lock)
    signer = sign_with or client
    for message in (nut20_message, nut20_message_legacy):
        try:
            sig = sign_bytes(signer, message(quote.quote, outs))
            promises = await LedgerAPI.mint(coord, outs, quote.quote, signature=sig)
            RESULTS.append(("  NUT-20 message form the mint accepted", message.__name__, "info"))
            return await coord._construct_proofs(promises, secs, rs, ["custom"] * len(secs))
        except Exception as exc:  # noqa: BLE001 — try the other message form, then give up
            last = exc
    raise last


async def invoice_for(coord: Wallet, amount: int):
    """A party's own Lightning wallet: an invoice we can later see as PAID."""
    return await LedgerAPI.mint_quote(coord, amount, Unit.sat)


def sig_all_melt_message(proofs: list[Proof], blanks, quote_id: str) -> str:
    return "".join(p.secret + p.C for p in proofs) + "".join(str(o.amount) + o.B_ for o in blanks) + quote_id


async def melt_to(coord: Wallet, proofs: list[Proof], recipient_pub: str, signers: list[Wallet]) -> tuple[int, str]:
    """Melt `proofs` to a fresh invoice for as much as they cover; returns (amount paid, invoice state)."""
    available = sum(p.amount for p in proofs) - fee(coord, proofs)
    amount = available
    for _ in range(3):  # settle the invoice amount against the fee reserve
        inv = await invoice_for(coord, amount)
        mq = await LedgerAPI.melt_quote(coord, inv.request, Unit.sat)
        if amount + mq.fee_reserve <= available:
            break
        amount = available - mq.fee_reserve
    # NUT-08 blank outputs for any fee change, locked to the recipient.
    n_blank = max(1, (mq.fee_reserve).bit_length()) if mq.fee_reserve else 0
    blank_secs = [to_recipient(recipient_pub).serialize() for _ in range(n_blank)]
    blanks, _ = coord._construct_outputs([1] * n_blank, blank_secs, [], coord.keyset_id)
    for b in blanks:
        b.amount = 0
    inputs = [p.model_copy() for p in proofs]
    for p in inputs:
        p.witness = None
    msg = sig_all_melt_message(inputs, blanks, mq.quote)
    inputs[0].witness = P2PKWitness(signatures=[sign(s, msg) for s in signers]).model_dump_json()
    resp = await LedgerAPI.melt(coord, mq.quote, inputs, blanks)
    change = sum(c.amount for c in (resp.change or []))
    return amount, await wait_paid(coord, inv.quote, seconds=10), f"fee reserve {mq.fee_reserve}, change back {change}"


async def swap(coord: Wallet, proofs: list[Proof], groups: list[tuple[str, int]], signers: list[Wallet]):
    """One SIG_ALL swap into groups, each locked (SIG_ALL) to its recipient."""
    outs_all, rs_all, secs_all, sizes = [], [], [], []
    for recipient, amount in groups:
        outs, rs, secs = locked_outputs(coord, amount_split(amount), to_recipient(recipient))
        outs_all += outs
        rs_all += rs
        secs_all += secs
        sizes.append(len(outs))
    inputs = [p.model_copy() for p in proofs]
    for p in inputs:
        p.witness = None
    msg = "".join(p.secret + p.C for p in inputs) + "".join(str(o.amount) + o.B_ for o in outs_all)
    inputs[0].witness = P2PKWitness(signatures=[sign(s, msg) for s in signers]).model_dump_json()
    promises = await LedgerAPI.split(coord, inputs, outs_all)
    proofs_out = await coord._construct_proofs(promises, secs_all, rs_all, ["custom"] * len(secs_all))
    split_at = sizes[0]
    return proofs_out[:split_at], proofs_out[split_at:]


async def main() -> None:
    c = await party("client")
    w = await party("worker")
    a = await party("arbiter")
    coord = await party("coordinator")
    far = int(time.time()) + 3600

    # IN: the wrong key cannot mint the client's paid invoice.
    await expect(
        "Mint the paid invoice signed by the worker, not the client",
        "W signs funding",
        fund_by_lightning(coord, c, escrow_lock(c, w, a, far), sign_with=w),
        ok=False,
    )

    # 1. Award split by grade: in by Lightning, out by Lightning to both.
    escrow = await expect(
        "Lightning in: client's invoice minted into the lock",
        "C signs funding",
        fund_by_lightning(coord, c, escrow_lock(c, w, a, far)),
        ok=True,
    )
    if escrow:
        net = ESCROW - fee(coord, escrow)
        to_worker = 700
        w_group, c_group = await expect(
            "Award: split into worker / client groups",
            "C+W",
            swap(coord, escrow, [(pub(w), to_worker), (pub(c), net - to_worker)], [c, w]),
            ok=True,
        ) or ([], [])
        if w_group:
            await expect(
                "Coordinator melts the worker's group to its own invoice",
                "coordinator",
                melt_to(coord, w_group, pub(coord), [coord]),
                ok=False,
            )
            paid = await expect(
                "Lightning out: worker's share to worker's invoice", "W", melt_to(coord, w_group, pub(w), [w]), ok=True
            )
            RESULTS.append(("  worker's invoice", "", f"{paid}"))
            paid = await expect(
                "Lightning out: client's share to client's invoice", "C", melt_to(coord, c_group, pub(c), [c]), ok=True
            )
            RESULTS.append(("  client's invoice", "", f"{paid}"))

    # 2. Dispute for the worker: escrow melted straight to the worker's invoice in one step.
    escrow = await fund_by_lightning(coord, c, escrow_lock(c, w, a, far))
    paid = await expect(
        "Dispute: escrow straight to worker's invoice", "A+W", melt_to(coord, escrow, pub(w), [a, w]), ok=True
    )
    RESULTS.append(("  worker's invoice", "", f"{paid}"))
    escrow = await fund_by_lightning(coord, c, escrow_lock(c, w, a, far))
    await expect("Worker alone melts the escrow to its invoice", "W", melt_to(coord, escrow, pub(w), [w]), ok=False)

    # 3. Refund date passes: client alone melts the escrow to the client's invoice.
    soon = int(time.time()) + 20
    escrow = await fund_by_lightning(coord, c, escrow_lock(c, w, a, soon))
    await asyncio.sleep(max(0, soon - int(time.time())) + 5)
    paid = await expect(
        "Refund: escrow straight to client's invoice", "C, after locktime", melt_to(coord, escrow, pub(c), [c]), ok=True
    )
    RESULTS.append(("  client's invoice", "", f"{paid}"))

    print(f"\nMint: testnut  escrow: {ESCROW} sats each\n")
    for label, path, verdict in RESULTS:
        print(f"{label:<62} {path:<24} {verdict}")


if __name__ == "__main__":
    asyncio.run(main())
