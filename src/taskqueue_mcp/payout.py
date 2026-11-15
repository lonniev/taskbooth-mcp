"""The invoice a purchaser pays when a task is Owed.

The reward goes from the purchaser's own wallet to the performer's. This
Operator never holds it. What it does is fetch an invoice from the performer's
Lightning address, check that the invoice asks for exactly what the grade
earns, write the payment hash down, and only then hand the invoice over.

Nothing here runs on a timer. An invoice is fetched when the purchaser or the
performer asks, and the same one is handed back until it expires.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from tollbooth import bolt11
from tollbooth.lnurl import LnurlResolutionError, resolve_lightning_address

from taskqueue_mcp.lifecycle import State

#: An invoice this close to expiry is replaced: nobody should scan a QR code
#: that dies while their wallet is opening.
EXPIRY_MARGIN_SECONDS = 120

Resolver = Callable[[str, int], Awaitable[str]]


def checked_address(value: Any) -> str:
    """A Lightning address (``name@domain``), or ``ValueError``."""
    text = value.strip() if isinstance(value, str) else ""
    name, at, domain = text.partition("@")
    well_formed = bool(at and name and "." in domain) and not any(c.isspace() or c == "@" for c in name + domain)
    if not well_formed or len(text) > 200:
        raise ValueError("A payout address is a Lightning address, like name@wallet.example.")
    return text.lower()


def as_handed_over(payout: dict[str, Any]) -> dict[str, Any]:
    """A stored payout as the people on the task see it."""
    return {
        "sats": payout["sats"],
        "invoice": payout["invoice"],
        # Plain BOLT11 behind the standard scheme: any wallet opens it.
        "lightning_uri": f"lightning:{payout['invoice']}",
        "payment_hash": payout["payment_hash"],
        "expires_at": payout["expires_at"],
        "paid_at": payout.get("paid_at"),
    }


async def invoice_for(
    db: Any,
    task: dict[str, Any],
    address: str,
    *,
    requested_by: str,
    at: int | None = None,
    resolve: Resolver | None = None,
) -> dict[str, Any]:
    """The invoice that settles an Owed task, fetched once and reused until it expires.

    Raises:
        ValueError: the task is not Owed, the performer's wallet gave no usable
            invoice, or the invoice asks for anything but the grade's sats.
    """
    at = int(time.time()) if at is None else at
    if task["state"] != State.OWED:
        raise ValueError("An invoice is made only for a task that is owed.")
    sats = int(task["assessment"]["sats"])

    kept = await db.payout(task["task_id"])
    if kept and kept["sats"] == sats and kept["expires_at"] > at + EXPIRY_MARGIN_SECONDS:
        return as_handed_over(kept)

    address = checked_address(address)
    try:
        invoice = await (resolve or resolve_lightning_address)(address, sats)
        asked = bolt11.decode(invoice)
    except (LnurlResolutionError, bolt11.Bolt11Error) as exc:
        raise ValueError(f"The performer's wallet gave no usable invoice: {exc}") from exc
    if asked.amount_msat != sats * 1000:
        # Never shown: a payee must not be able to ask for more than the grade earns.
        raise ValueError(
            f"The performer's wallet returned an invoice for a different amount than the {sats} sats owed."
        )
    if asked.expires_at <= at + EXPIRY_MARGIN_SECONDS:
        raise ValueError("The performer's wallet returned an invoice that has already expired.")

    payout = {
        "sats": sats,
        "invoice": invoice,
        "payment_hash": asked.payment_hash,
        "expires_at": asked.expires_at,
        "method": "invoice",
        "address": address,
        "requested_by": requested_by,
        "requested_at": at,
    }
    # The payment hash is on record before the invoice leaves this Operator.
    await db.save_payout(task["task_id"], payout)
    return as_handed_over(payout)
