"""Task 22, option 0: a Cashu 2-of-3 escrow (NUT-11) exercised against testnut.cashu.space.

Parties: client C, worker W, arbiter A, each with their own key. The coordinator
stands in for the Task Booth Operator: it builds the lock and the payout
outputs, gathers signatures and submits the swap, and holds no key in the lock.

The lock: data = C, pubkeys = [W, A], n_sigs = 2, locktime = the refund date,
refund = [C], sigflag = SIG_ALL. Every payout output is itself locked to its
recipient, so the coordinator, who builds the outputs and unblinds them, can
never spend what it hands over.

Testnut mints pretend sats: no real money moves.
"""

from __future__ import annotations

import asyncio
import copy
import secrets
import time

from cashu.core.base import P2PKWitness, Proof
from cashu.core.crypto.secp import PrivateKey
from cashu.core.migrations import migrate_databases
from cashu.core.p2pk import P2PKSecret, schnorr_sign
from cashu.core.secret import SecretKind, Tags
from cashu.core.split import amount_split
from cashu.wallet import migrations
from cashu.wallet.v1_api import LedgerAPI
from cashu.wallet.wallet import Wallet

MINT = "https://testnut.cashu.space"
ESCROW = 1000
RESULTS: list[tuple[str, str, str]] = []


async def party(name: str) -> Wallet:
    w = await Wallet.with_db(MINT, f"data/{name}", name)
    await migrate_databases(w.db, migrations)
    w.private_key = PrivateKey(secrets.token_bytes(32))
    await w.load_mint()
    return w


def pub(w: Wallet) -> str:
    return w.private_key.public_key.format().hex()


def escrow_lock(c: Wallet, w: Wallet, a: Wallet, locktime: int, sigflag: str = "SIG_ALL") -> P2PKSecret:
    tags = Tags(
        [
            ["pubkeys", pub(w), pub(a)],
            ["n_sigs", "2"],
            ["locktime", str(locktime)],
            ["refund", pub(c)],
            ["sigflag", sigflag],
        ]
    )
    return P2PKSecret(kind=SecretKind.P2PK.value, data=pub(c), tags=tags)


async def topup(c: Wallet, amount: int) -> None:
    quote = await c.request_mint(amount)
    for _ in range(30):
        try:
            await c.mint(amount, quote_id=quote.quote)
            return
        except Exception:  # noqa: BLE001 — poll until the test mint marks the quote paid
            await asyncio.sleep(1)
    raise RuntimeError("test mint never paid the quote")


async def fund(c: Wallet, lock: P2PKSecret) -> list[Proof]:
    """The client locks ESCROW sats; returns the locked proofs (the escrow)."""
    plain = [p for p in c.proofs if not p.secret.startswith("[")]
    _, locked = await c.split(plain, ESCROW, secret_lock=lock)
    return locked


def payout(coord: Wallet, shares: list[tuple[str, int]]):
    """Blinded outputs, each locked to its recipient's key."""
    amounts, secs = [], []
    for recipient, amount in shares:
        for piece in amount_split(amount):
            amounts.append(piece)
            secs.append(
                P2PKSecret(
                    kind=SecretKind.P2PK.value, data=recipient, tags=Tags([["sigflag", "SIG_INPUTS"]])
                ).serialize()
            )
    outs, rs = coord._construct_outputs(amounts, secs, [], coord.keyset_id)
    return outs, rs, secs


def sig_all_message(proofs: list[Proof], outs) -> str:
    """NUT-11 swap aggregation: secret_i || C_i ... || amount_j || B_j."""
    return "".join(p.secret + p.C for p in proofs) + "".join(str(o.amount) + o.B_ for o in outs)


def sign(w: Wallet, message: str) -> str:
    return schnorr_sign(message.encode("utf-8"), w.private_key).hex()


async def submit(coord: Wallet, proofs: list[Proof], outs, rs, secs, sigs: list[str]) -> list[Proof]:
    inputs = copy.deepcopy(proofs)
    for p in inputs:
        p.witness = None
    inputs[0].witness = P2PKWitness(signatures=sigs).model_dump_json()
    promises = await LedgerAPI.split(coord, inputs, outs)
    return await coord._construct_proofs(promises, secs, rs, ["custom"] * len(secs))


def fee(coord: Wallet, proofs: list[Proof]) -> int:
    return coord.get_fees_for_proofs(proofs)


async def expect(label: str, path: str, coro, ok: bool) -> object:
    try:
        out = await coro
        RESULTS.append((label, path, "PASS" if ok else "FAIL (should have been refused)"))
        return out
    except Exception as exc:  # noqa: BLE001 — a refusal is a result to record
        reason = str(exc.args[0] if exc.args else exc)[:90]
        RESULTS.append((label, path, f"PASS (refused: {reason})" if not ok else f"FAIL: {reason}"))
        return None


async def spend(coord, locked, shares, signers):
    outs, rs, secs = payout(coord, shares)
    msg = sig_all_message(locked, outs)
    return await submit(coord, locked, outs, rs, secs, [sign(s, msg) for s in signers])


async def run() -> None:
    c = await party("client")
    w = await party("worker")
    a = await party("arbiter")
    coord = await party("coordinator")
    await topup(c, 9 * ESCROW)
    far = int(time.time()) + 3600

    # 1. Award: client and worker sign a 70/30 split by grade.
    locked = await fund(c, escrow_lock(c, w, a, far))
    net = ESCROW - fee(coord, locked)
    to_worker = 700
    shares = [(pub(w), to_worker), (pub(c), net - to_worker)]
    outs, rs, secs = payout(coord, shares)
    msg = sig_all_message(locked, outs)
    sigs = [sign(c, msg), sign(w, msg)]

    # 1a. Tamper: the coordinator swaps in outputs paying itself, reusing those signatures.
    t_outs, t_rs, t_secs = payout(coord, [(pub(coord), net)])
    await expect(
        "Tampered outputs with the award signatures",
        "C+W, outputs replaced",
        submit(coord, locked, t_outs, t_rs, t_secs, sigs),
        ok=False,
    )
    paid = await expect(
        "Award split by grade (700 / rest)", "C+W", submit(coord, locked, outs, rs, secs, sigs), ok=True
    )
    if paid:
        mine = paid[: len(amount_split(to_worker))]
        theirs = paid[len(amount_split(to_worker)) :]
        await expect(
            "Coordinator spends the worker's payout", "coordinator alone", coord.redeem(copy.deepcopy(mine)), ok=False
        )
        await expect("Worker spends their payout", "W", w.redeem(mine), ok=True)
        await expect("Client spends their refund share", "C", c.redeem(theirs), ok=True)
    await expect("Same escrow paid twice", "C+W again", spend(coord, locked, shares, [c, w]), ok=False)

    # 2-3. Disputes.
    locked = await fund(c, escrow_lock(c, w, a, far))
    net = ESCROW - fee(coord, locked)
    await expect("Dispute: arbiter sides with worker", "A+W", spend(coord, locked, [(pub(w), net)], [a, w]), ok=True)
    locked = await fund(c, escrow_lock(c, w, a, far))
    net = ESCROW - fee(coord, locked)
    await expect("Dispute: arbiter sides with client", "A+C", spend(coord, locked, [(pub(c), net)], [a, c]), ok=True)

    # 4. One signer alone, before the refund date.
    locked = await fund(c, escrow_lock(c, w, a, far))
    net = ESCROW - fee(coord, locked)
    await expect("Worker alone before the refund date", "W", spend(coord, locked, [(pub(w), net)], [w]), ok=False)
    await expect("Client alone before the refund date", "C", spend(coord, locked, [(pub(c), net)], [c]), ok=False)
    await expect("Arbiter alone", "A", spend(coord, locked, [(pub(a), net)], [a]), ok=False)

    # 5. Refund date passes: the client alone recovers; the worker alone still cannot.
    soon = int(time.time()) + 20
    locked = await fund(c, escrow_lock(c, w, a, soon))
    net = ESCROW - fee(coord, locked)
    await asyncio.sleep(max(0, soon - int(time.time())) + 5)
    await expect(
        "Worker alone after the refund date", "W, after locktime", spend(coord, locked, [(pub(w), net)], [w]), ok=False
    )
    await expect(
        "Refund: client alone after the date", "C, after locktime", spend(coord, locked, [(pub(c), net)], [c]), ok=True
    )

    # 6. Why SIG_ALL: under SIG_INPUTS a co-signer's signature does not cover the outputs.
    locked = await fund(c, escrow_lock(c, w, a, far, sigflag="SIG_INPUTS"))
    net = ESCROW - fee(coord, locked)
    stolen = copy.deepcopy(locked)
    for p in stolen:
        p.witness = P2PKWitness(signatures=[sign(c, p.secret), sign(w, p.secret)]).model_dump_json()
    outs, rs, secs = payout(coord, [(pub(w), net)])
    await expect(
        "SIG_INPUTS: worker takes all with the client's signatures",
        "C+W sigs, worker picks outputs",
        LedgerAPI.split(coord, stolen, outs),
        ok=True,
    )

    print(f"\nMint: {MINT}  escrow: {ESCROW} sats each  input fee per escrow: {fee(coord, locked)} sat\n")
    for label, path, verdict in RESULTS:
        print(f"{label:<62} {path:<34} {verdict}")


if __name__ == "__main__":
    asyncio.run(run())
