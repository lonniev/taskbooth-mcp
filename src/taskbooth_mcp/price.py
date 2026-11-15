"""What a bitcoin costs, today and on this day in earlier years.

Read from mempool.space when asked, and handed on with the time each price was
observed. This Operator computes nothing from the prices and vouches for none
of them: a site that shows a projection does its own sums and says so.

A reading is kept in memory for an hour, so a page that many people open does
not become many requests to someone else's service. Nothing runs on a timer.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

SOURCE = "mempool.space"
SOURCE_URL = "https://mempool.space/api/v1"

#: The currencies mempool.space quotes.
FIATS = ("USD", "EUR", "GBP", "CAD", "CHF", "AUD", "JPY")

MAX_YEARS = 10
KEEP_SECONDS = 3600
_YEAR = 365 * 86_400

_kept: dict[tuple[str, int], tuple[int, dict[str, Any]]] = {}


def checked_fiat(value: Any) -> str:
    """One of the quoted currencies, upper-cased, or ``ValueError``."""
    text = value.strip().upper() if isinstance(value, str) else ""
    if text not in FIATS:
        raise ValueError(f"Prices are quoted in {', '.join(FIATS)}.")
    return text


def checked_years(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_YEARS:
        raise ValueError(f"Ask for 0 to {MAX_YEARS} years of history.")
    return value


def _price(row: Any, fiat: str) -> float:
    """A positive price out of a mempool.space row, or ``ValueError``: the answer is someone else's."""
    price = row.get(fiat) if isinstance(row, dict) else None
    if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
        raise ValueError(f"{SOURCE} gave no {fiat} price.")
    return float(price)


async def reading(fiat: str, years: int, *, at: int | None = None, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Today's price and the price on this day in each of the last ``years`` years.

    Raises:
        ValueError: the currency or span is not offered, or mempool.space did not answer usably.
    """
    fiat, years = checked_fiat(fiat), checked_years(years)
    at = int(time.time()) if at is None else at
    kept = _kept.get((fiat, years))
    if kept and at - kept[0] < KEEP_SECONDS:
        return kept[1]

    own = client is None
    http = client or httpx.AsyncClient(timeout=10.0)
    try:
        now = (await http.get(f"{SOURCE_URL}/prices")).raise_for_status().json()
        history = []
        for back in range(years, 0, -1):
            when = at - back * _YEAR
            answer = (await http.get(f"{SOURCE_URL}/historical-price", params={"currency": fiat, "timestamp": when}))
            rows = answer.raise_for_status().json().get("prices") or []
            if rows:
                history.append({"observed_at": int(rows[0].get("time", when)), "price": _price(rows[0], fiat)})
    except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{SOURCE} did not answer with a price: {type(exc).__name__}.") from exc
    finally:
        if own:
            await http.aclose()

    out = {
        "fiat": fiat,
        "price": _price(now, fiat),
        "observed_at": int(now.get("time", at)),
        "history": history,
        "source": SOURCE,
        "source_url": "https://mempool.space",
    }
    _kept[(fiat, years)] = (at, out)
    return out
