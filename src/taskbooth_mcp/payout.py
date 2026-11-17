"""The invoices a purchaser pays: a deposit, a milestone, and an Owed task's award.

The reward goes from the purchaser's own wallet to the performer's. This
Operator never holds it. What it does is fetch an invoice from the performer's
Lightning address, check that the invoice asks for exactly what the grade
earns, write the payment hash down, and only then hand the invoice over.

A performer with no Lightning address makes the invoice in their own wallet and
hands it in; it passes the same checks. Every wallet can do that much.

Nothing here runs on a timer. An invoice is fetched when the purchaser or the
performer asks, and the same one is handed back until it expires.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from tollbooth import bolt11
from tollbooth.lnurl import LnurlResolutionError, resolve_lightning_address

from taskbooth_mcp.lifecycle import State

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


def checked_invoice(value: Any) -> str:
    """A BOLT11 invoice as a wallet shows it, tidied, or ``ValueError``.

    Wallets hand an invoice over bare, behind ``lightning:``, or in capitals
    (the form a QR code carries). All three are the same invoice.
    """
    text = value.strip() if isinstance(value, str) else ""
    if text[:10].lower() == "lightning:":
        text = text[10:]
    text = text.lower()
    if not text.startswith("ln") or len(text) > 4000 or any(c.isspace() for c in text):
        raise ValueError("That is not a Lightning invoice. Copy the whole request from your wallet.")
    return text


def _checked(invoice: str, sats: int, *, at: int, whose: str, details: dict[str, Any]) -> dict[str, Any]:
    """An invoice read and held to exactly ``sats``, as the record kept before anyone sees it."""
    try:
        asked = bolt11.decode(invoice)
    except bolt11.Bolt11Error as exc:
        raise ValueError(f"{whose} gave no usable invoice: {exc}") from exc
    if asked.amount_msat != sats * 1000:
        # Never shown: a payee must not be able to ask for more than is due.
        raise ValueError(f"{whose} gave an invoice for a different amount than the {sats} sats owed.")
    if asked.expires_at <= at + EXPIRY_MARGIN_SECONDS:
        raise ValueError(f"{whose} gave an invoice that has already expired.")
    return {
        "sats": sats,
        "invoice": invoice,
        "payment_hash": asked.payment_hash,
        "expires_at": asked.expires_at,
        "requested_at": at,
    } | details


async def _recorded(db: Any, task: dict[str, Any], invoice: str, *, at: int, whose: str,
                    details: dict[str, Any]) -> dict[str, Any]:
    """Check an invoice against the grade and write its payment hash down before it is shown."""
    payout = _checked(invoice, int(task["assessment"]["sats"]), at=at, whose=whose, details=details)
    # The payment hash is on record before the invoice leaves this Operator.
    await db.save_payout(task["task_id"], payout)
    return as_handed_over(payout)


async def invoice_from_performer(
    db: Any, task: dict[str, Any], invoice: Any, *, performer: str, at: int | None = None,
) -> dict[str, Any]:
    """The invoice the performer made in their own wallet for exactly the sats owed.

    It replaces any invoice already kept: the performer says where they are paid.

    Raises:
        ValueError: the task is not Owed, or the invoice is unreadable, expired,
            or asks for anything but the grade's sats.
    """
    at = int(time.time()) if at is None else at
    if task["state"] != State.OWED:
        raise ValueError("An invoice is taken only for a task that is owed.")
    return await _recorded(
        db, task, checked_invoice(invoice), at=at, whose="Your wallet",
        details={"method": "performer_invoice", "requested_by": performer},
    )


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

    With no ``address``, only an invoice already kept can be handed over.

    Raises:
        ValueError: the task is not Owed, no invoice is kept and the performer
            has no address, the performer's wallet gave no usable invoice, or
            the invoice asks for anything but the grade's sats.
    """
    at = int(time.time()) if at is None else at
    if task["state"] != State.OWED:
        raise ValueError("An invoice is made only for a task that is owed.")
    sats = int(task["assessment"]["sats"])

    kept = await db.payout(task["task_id"])
    if kept and kept["sats"] == sats and kept["expires_at"] > at + EXPIRY_MARGIN_SECONDS:
        return as_handed_over(kept)

    if not address:
        raise ValueError(
            f"The performer has not set a payout_address. They make an invoice for exactly {sats} sats "
            "in their own wallet and pass it to taskbooth_request_payout as invoice."
        )
    address = checked_address(address)
    try:
        invoice = await (resolve or resolve_lightning_address)(address, sats)
    except LnurlResolutionError as exc:
        raise ValueError(f"The performer's wallet gave no usable invoice: {exc}") from exc
    return await _recorded(
        db, task, invoice, at=at, whose="The performer's wallet",
        details={"method": "invoice", "address": address, "requested_by": requested_by},
    )


async def advance_invoice(
    db: Any,
    task: dict[str, Any],
    which: int | None,
    *,
    address: str,
    invoice: Any = "",
    requested_by: str,
    at: int | None = None,
    resolve: Resolver | None = None,
) -> dict[str, Any]:
    """The invoice that pays a task's deposit (``which`` None) or milestone ``which``, held to its exact sats.

    ``task`` is the store's own copy, from ``db.advance_task``. A performer's
    own ``invoice`` replaces any kept; otherwise one still fresh is handed back,
    or a new one is fetched from ``address``. Its payment hash is written onto
    the task before it is shown.

    Raises:
        ValueError: no usable invoice, or one for any other amount.
    """
    at = int(time.time()) if at is None else at
    advance = db.advance_of(task, which)
    sats = int(advance["sats"])
    if not invoice and advance.get("invoice") and advance["expires_at"] > at + EXPIRY_MARGIN_SECONDS:
        return as_handed_over(advance)
    if invoice:
        record = _checked(checked_invoice(invoice), sats, at=at, whose="Your wallet",
                          details={"method": "performer_invoice", "requested_by": requested_by})
    else:
        if not address:
            tool = "taskbooth_request_deposit" if which is None else "taskbooth_request_milestone"
            raise ValueError(
                f"The performer has not set a payout_address. They make an invoice for exactly {sats} sats "
                f"in their own wallet and pass it to {tool} as invoice."
            )
        try:
            fetched = await (resolve or resolve_lightning_address)(checked_address(address), sats)
        except LnurlResolutionError as exc:
            raise ValueError(f"The performer's wallet gave no usable invoice: {exc}") from exc
        record = _checked(fetched, sats, at=at, whose="The performer's wallet",
                          details={"method": "invoice", "requested_by": requested_by})
    await db.save_advance(requested_by, task, which, record, at)
    return as_handed_over(record)
