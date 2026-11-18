"""A Cashu mint in memory, for escrow tests: it keeps the rules a real mint keeps.

It signs with DLEQ proofs, refuses a spent proof, checks NUT-20 quote signatures,
and enforces NUT-11 P2PK locks with SIG_ALL: two of the named keys, or the refund
keys after the locktime, over the exact swap or melt message. Its clock is
``now``, set by the test.
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from coincurve import PrivateKey, PublicKey
from tollbooth import bolt11, cashu

KEYSET = "00fa4e5c0ffee000"
MINT_URL = "https://mint.example"


class FakeMint:
    def __init__(self, *, fee_ppk: int = 100, now: int = 0) -> None:
        self.fee_ppk = fee_ppk
        self.now = now
        self._k = {1 << i: PrivateKey() for i in range(24)}
        self.keys = {a: k.public_key.format().hex() for a, k in self._k.items()}
        self.spent: set[str] = set()
        self.quotes: dict[str, dict[str, Any]] = {}
        self.melts: dict[str, dict[str, Any]] = {}
        self.paid_invoices: list[tuple[str, int]] = []
        self.info_nuts = {n: {"supported": True} for n in cashu.ESCROW_NUTS}

    # -- what a client calls ---------------------------------------------------

    async def info(self) -> dict[str, Any]:
        return {"name": "fake", "nuts": self.info_nuts}

    async def active_keyset(self, unit: str = "sat") -> dict[str, Any]:
        return {"id": KEYSET, "input_fee_ppk": self.fee_ppk, "keys": dict(self.keys)}

    async def mint_quote(self, amount: int, pubkey: str, unit: str = "sat") -> dict[str, Any]:
        quote = secrets.token_hex(8)
        self.quotes[quote] = {"amount": amount, "pubkey": pubkey, "state": "UNPAID"}
        return {"quote": quote, "request": f"lnbc-fake-{quote}", "expiry": self.now + 3600, "state": "UNPAID"}

    def pay(self, quote: str) -> None:
        """The purchaser pays the invoice from their own wallet."""
        self.quotes[quote]["state"] = "PAID"

    async def mint_quote_state(self, quote: str) -> dict[str, Any]:
        return {"quote": quote, "state": self.quotes[quote]["state"]}

    async def mint(self, quote: str, outputs: list[dict[str, Any]], signature: str) -> list[dict[str, Any]]:
        q = self.quotes.get(quote)
        if q is None or q["state"] != "PAID":
            raise cashu.CashuError("quote not paid", code=20001)
        if not cashu.schnorr_verify(q["pubkey"], cashu.mint_quote_message(quote, outputs), signature):
            raise cashu.CashuError("Signature missing or invalid", code=20008)
        if sum(o["amount"] for o in outputs) != q["amount"]:
            raise cashu.CashuError("outputs do not match the quote", code=11000)
        q["state"] = "ISSUED"
        return [self._sign(o) for o in outputs]

    async def swap(self, inputs: list[dict[str, Any]], outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self._spend(inputs, cashu.swap_message(inputs, outputs))
        fee = cashu.input_fee(inputs, self.fee_ppk)
        if sum(p["amount"] for p in inputs) - fee != sum(o["amount"] for o in outputs):
            raise cashu.CashuError("inputs and outputs do not balance", code=11002)
        self._mark(inputs)
        return [self._sign(o) for o in outputs]

    async def melt_quote(self, invoice: str, unit: str = "sat") -> dict[str, Any]:
        amount = bolt11.decode(invoice).amount_msat // 1000
        quote = secrets.token_hex(8)
        reserve = 2 if amount > 100 else 0
        self.melts[quote] = {"amount": amount, "invoice": invoice, "fee_reserve": reserve}
        return {"quote": quote, "amount": amount, "fee_reserve": reserve, "state": "UNPAID"}

    async def melt(self, quote: str, inputs: list[dict[str, Any]], outputs: list[dict[str, Any]]) -> dict[str, Any]:
        m = self.melts[quote]
        self._spend(inputs, cashu.melt_message(inputs, outputs, quote))
        fee = cashu.input_fee(inputs, self.fee_ppk)
        if sum(p["amount"] for p in inputs) - fee < m["amount"] + m["fee_reserve"]:
            raise cashu.CashuError("not enough inputs for the invoice and its fee reserve", code=11002)
        self._mark(inputs)
        self.paid_invoices.append((m["invoice"], m["amount"]))
        # Lightning cost nothing here: the whole reserve comes back as change.
        change = []
        for output, amount in zip(outputs, cashu.amount_split(m["fee_reserve"]), strict=False):
            change.append(self._sign({**output, "amount": amount}))
        return {"quote": quote, "state": "PAID", "change": change}

    # -- the mint's own rules --------------------------------------------------

    def _sign(self, output: dict[str, Any]) -> dict[str, Any]:
        k = self._k[output["amount"]]
        b_ = cashu._point(output["B_"])
        c_ = b_.multiply(k.secret)
        r = PrivateKey()
        e = cashu.hash_e(r.public_key, b_.multiply(r.secret), k.public_key, c_)
        s = (int.from_bytes(r.secret, "big") + int.from_bytes(e, "big") * int.from_bytes(k.secret, "big")) % cashu.N
        return {"amount": output["amount"], "id": KEYSET, "C_": c_.format().hex(),
                "dleq": {"e": e.hex(), "s": s.to_bytes(32, "big").hex()}}

    def _mark(self, inputs: list[dict[str, Any]]) -> None:
        for p in inputs:
            self.spent.add(p["secret"])

    def _spend(self, inputs: list[dict[str, Any]], message: str) -> None:
        for p in inputs:
            if p["secret"] in self.spent:
                raise cashu.CashuError("Token already spent", code=11001)
            if not cashu.mint_verify(self._k[p["amount"]].secret, p["secret"], p["C"]):
                raise cashu.CashuError("invalid proof", code=10003)
        if not inputs[0]["secret"].startswith("["):
            return
        locks = [cashu.secret_lock(p["secret"]) for p in inputs]
        if any(lock != locks[0] for lock in locks):
            raise cashu.CashuError("SIG_ALL inputs differ", code=20008)
        tags = {t[0]: t[1:] for t in locks[0]["tags"]}
        if tags.get("sigflag", ["SIG_INPUTS"])[0] != "SIG_ALL":
            raise cashu.CashuError("this fake mint keeps SIG_ALL only", code=20008)
        signatures = json.loads(inputs[0].get("witness") or "{}").get("signatures", [])

        def count(keys: list[str]) -> int:
            return sum(1 for key in set(keys) if any(cashu.schnorr_verify(key, message, s) for s in signatures))

        signers = [locks[0]["data"], *tags.get("pubkeys", [])]
        if count(signers) >= int(tags.get("n_sigs", ["1"])[0]):
            return
        after_locktime = "locktime" in tags and self.now >= int(tags["locktime"][0])
        if after_locktime and count(tags.get("refund", [])) >= int(tags.get("n_sigs_refund", ["1"])[0]):
            return
        raise cashu.CashuError("P2PK spend conditions are not met", code=20008)


def sign_digest(key: Any, digest: str) -> str:
    """BIP-340 over a 32-byte digest, as a signer's own Nostr key makes it."""
    return PrivateKey(key.raw_secret).sign_schnorr(bytes.fromhex(digest)).hex()


__all__ = ["MINT_URL", "FakeMint", "PublicKey", "sign_digest"]
