"""BOLT11 invoices for tests: readable by ``tollbooth.bolt11``, payable by nobody."""

import hashlib
import time

from pynostr.bech32 import Encoding, bech32_encode


def _groups(value: int, count: int) -> list[int]:
    return [(value >> (5 * i)) & 31 for i in reversed(range(count))]


def payment_hash_of(preimage: str) -> str:
    return hashlib.sha256(bytes.fromhex(preimage)).hexdigest()


def make_invoice(sats: int | None, *, preimage: str = "11" * 32, created_at: int | None = None,
                 expiry: int = 3600) -> str:
    """An invoice with a blank signature — enough for a reader, not for a node."""
    hrp = "lnbc" + (f"{sats * 10}n" if sats is not None else "")
    data = _groups(created_at if created_at is not None else int(time.time()), 7)
    data += [1, 1, 20] + _groups(int(payment_hash_of(preimage), 16) << 4, 52)
    data += [6, 0, 4] + _groups(expiry, 4)
    data += [0] * 104
    return str(bech32_encode(hrp, data, Encoding.BECH32))
