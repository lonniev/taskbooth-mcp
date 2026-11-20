"""What is known about a Cashu mint, from the mint itself and from two public watchers.

The facts come from three places, and each one is passed on with its source and
the time it was observed:

- the mint's own ``/v1/info``: its name, its version and the NUTs it claims;
- cashumints.space: whether it is online, its uptime over 30 days, when it was
  first seen, and how many reviews it has, with the date of the latest;
- the Round Robin Auditor (audit.8333.space), which keeps paying Lightning
  invoices in a circle between mints: its swaps with this mint, how many
  failed, and since when the record is clean.

This Operator judges nothing. A page or an agent decides what record a mint
needs. A source that does not answer reads as ``{"unreachable": ...}``,
which says nothing about the mint itself.

Answers are kept in memory for ten minutes, so a page many people open does not
turn into many requests to someone else's service. Nothing runs on a timer.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
from tollbooth import cashu

CASHUMINTS = "cashumints.space"
CASHUMINTS_API = "https://cashumints.space/api"
AUDITOR = "audit.8333.space"
AUDITOR_API = "https://api.audit.8333.space"

KEEP_SECONDS = 600
#: Most mints one call may ask about.
MOST_MINTS = 60
#: How far back the auditor's record is read, and in what pages.
AUDIT_DAYS = 120
AUDIT_PAGE = 500
AUDIT_PAGES = 4
#: Requests in flight at once when many mints are asked about.
AT_ONCE = 8
_DAY = 86_400

_kept: dict[str, tuple[int, Any]] = {}


def _fresh(key: str, at: int) -> Any:
    kept = _kept.get(key)
    return kept[1] if kept and at - kept[0] < KEEP_SECONDS else None


def _keep(key: str, at: int, value: Any) -> Any:
    _kept[key] = (at, value)
    return value


def _client(http: httpx.AsyncClient | None) -> tuple[httpx.AsyncClient, bool]:
    if http is not None:
        return http, False
    return httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=False), True


def _unreachable(source: str, exc: Exception, at: int) -> dict[str, Any]:
    return {"source": source, "observed_at": at, "unreachable": type(exc).__name__}


def _nuts_of(info: dict[str, Any]) -> list[str]:
    nuts = info.get("nuts") or {}
    if not isinstance(nuts, dict):
        return []
    claimed = [n for n, entry in nuts.items() if isinstance(entry, dict) and entry.get("supported") is not False]
    return sorted(claimed, key=lambda n: (len(n), n))


def _short(value: Any, most: int) -> str:
    return value.strip()[:most] if isinstance(value, str) else ""


def _https_or_blank(value: Any) -> str:
    text = _short(value, 300)
    return text if text.startswith("https://") else ""


async def own_word(url: str, *, at: int, http: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """What the mint says of itself in ``/v1/info``."""
    key = f"own:{url}"
    if (kept := _fresh(key, at)) is not None:
        return dict(kept)
    try:
        info = await cashu.MintClient(url, client=http).info()
    except cashu.CashuError as exc:
        return {"source": "mint", "observed_at": at, "unreachable": str(exc)[:120]}
    return _keep(key, at, {
        "source": "mint",
        "observed_at": at,
        "name": _short(info.get("name"), 80),
        "version": _short(info.get("version"), 60),
        "icon_url": _https_or_blank(info.get("icon_url")),
        "nuts": _nuts_of(info),
        "missing_escrow_nuts": cashu.missing_escrow_nuts(info),
    })


async def _cashumints_list(at: int, http: httpx.AsyncClient) -> list[dict[str, Any]]:
    if (kept := _fresh("cashumints:list", at)) is not None:
        return list(kept)
    rows = (await http.get(f"{CASHUMINTS_API}/mints")).raise_for_status().json()
    if not isinstance(rows, list):
        raise ValueError("the mint list is not a list")
    return list(_keep("cashumints:list", at, [r for r in rows if isinstance(r, dict)]))


def _when(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


async def watched(url: str, *, at: int, http: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """What cashumints.space has seen of the mint: online, uptime, first seen, reviews."""
    key = f"cashumints:{url}"
    if (kept := _fresh(key, at)) is not None:
        return dict(kept)
    client, own = _client(http)
    try:
        listed = next((r for r in await _cashumints_list(at, client)
                       if isinstance(r.get("url"), str) and r["url"].rstrip("/") == url), None)
        if listed is None:
            return _keep(key, at, {"source": CASHUMINTS, "observed_at": at, "listed": False})
        detail = (await client.get(f"{CASHUMINTS_API}/mints/{quote(str(listed['host']), safe='')}")).raise_for_status().json()
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return _unreachable(CASHUMINTS, exc, at)
    finally:
        if own:
            await client.aclose()
    uptime = detail.get("uptime_30d")
    icon = _short(listed.get("icon"), 200)
    return _keep(key, at, {
        "source": CASHUMINTS,
        "observed_at": at,
        "listed": True,
        "status": _short(detail.get("status"), 20),
        "uptime_30d": float(uptime) if isinstance(uptime, (int, float)) and not isinstance(uptime, bool) else None,
        "first_seen": _when(detail.get("first_seen")),
        "reviews": int(detail.get("review_count") or 0) if isinstance(detail.get("review_count"), int) else 0,
        "latest_review_at": _when(detail.get("last_review_at")),
        "icon_url": f"https://{CASHUMINTS}{icon}" if icon.startswith("/") else _https_or_blank(icon),
    })


def _stamp(value: Any) -> int | None:
    """An auditor timestamp, ISO 8601 in UTC with or without a zone, as Unix seconds."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return int((parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp())


def audit_record(swaps: list[dict[str, Any]], *, at: int) -> dict[str, Any]:
    """The auditor's swaps with one mint, summed up. Pure, for tests.

    ``tracked_since`` is the oldest swap read, so the record is at least that
    old. ``clean_since`` is when the run of good swaps since the latest failure
    began; with no failure read, it is ``tracked_since``.
    """
    dated = sorted(((t, s) for s in swaps if (t := _stamp(s.get("created_at"))) is not None), key=lambda p: p[0])
    if not dated:
        return {"swaps": 0, "failed": 0, "failed_90d": 0, "tracked_since": None, "clean_since": None,
                "last_failure_at": None, "last_error": ""}
    failures = [(t, s) for t, s in dated if str(s.get("state", "")).upper() != "OK"]
    last_failure = failures[-1] if failures else None
    after = [t for t, _ in dated if last_failure is None or t > last_failure[0]]
    return {
        "swaps": len(dated),
        "failed": len(failures),
        "failed_90d": sum(1 for t, _ in failures if t >= at - 90 * _DAY),
        "tracked_since": dated[0][0],
        "clean_since": (after[0] if after else None) if last_failure else dated[0][0],
        "last_failure_at": last_failure[0] if last_failure else None,
        "last_error": _short(last_failure[1].get("error"), 160) if last_failure else "",
    }


async def audited(url: str, *, at: int, http: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """The Round Robin Auditor's record of the mint, read back about ``AUDIT_DAYS`` days."""
    key = f"auditor:{url}"
    if (kept := _fresh(key, at)) is not None:
        return dict(kept)
    client, own = _client(http)
    try:
        found = await client.get(f"{AUDITOR_API}/mints/url", params={"url": url})
        if found.status_code == 404:
            return _keep(key, at, {"source": AUDITOR, "observed_at": at, "tracked": False})
        mint_id = int(found.raise_for_status().json()["id"])
        swaps: list[dict[str, Any]] = []
        for page in range(AUDIT_PAGES):
            rows = (await client.get(f"{AUDITOR_API}/swaps/mint/{mint_id}",
                                     params={"skip": page * AUDIT_PAGE, "limit": AUDIT_PAGE})).raise_for_status().json()
            if not isinstance(rows, list):
                raise ValueError("the swap list is not a list")
            swaps.extend(r for r in rows if isinstance(r, dict))
            oldest = min((t for r in rows if (t := _stamp(r.get("created_at"))) is not None), default=None)
            if len(rows) < AUDIT_PAGE or (oldest is not None and oldest < at - AUDIT_DAYS * _DAY):
                break
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return _unreachable(AUDITOR, exc, at)
    finally:
        if own:
            await client.aclose()
    return _keep(key, at, {"source": AUDITOR, "observed_at": at, "tracked": True, **audit_record(swaps, at=at)})


def checked_mints(urls: Any) -> list[str]:
    """Distinct public https mint addresses, at most ``MOST_MINTS``, or ``ValueError``."""
    if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
        raise ValueError("Name the mints as a list of https addresses.")
    out: list[str] = []
    for u in urls:
        try:
            checked = cashu.checked_mint_url(u)
        except ValueError as exc:
            raise ValueError(f"{u[:80]!r}: {exc}") from None
        if checked not in out:
            out.append(checked)
    if len(out) > MOST_MINTS:
        raise ValueError(f"Ask about at most {MOST_MINTS} mints at once.")
    return out


async def facts(urls: list[str], *, at: int | None = None, http: httpx.AsyncClient | None = None) -> list[dict[str, Any]]:
    """Everything known about each mint, from all three sources."""
    at = int(time.time()) if at is None else at
    client, own = _client(http)
    gate = asyncio.Semaphore(AT_ONCE)

    async def one(url: str) -> dict[str, Any]:
        async with gate:
            mint, seen, audit = await asyncio.gather(
                own_word(url, at=at, http=client), watched(url, at=at, http=client), audited(url, at=at, http=client))
        return {"url": url, "mint": mint, "cashumints": seen, "auditor": audit}

    try:
        return list(await asyncio.gather(*(one(u) for u in urls)))
    finally:
        if own:
            await client.aclose()


async def candidates(*, at: int | None = None, http: httpx.AsyncClient | None = None) -> list[str]:
    """Online Cashu mints cashumints.space lists as claiming every NUT an escrow needs."""
    at = int(time.time()) if at is None else at
    client, own = _client(http)
    try:
        rows = await _cashumints_list(at, client)
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError(f"{CASHUMINTS} did not answer with its mint list: {type(exc).__name__}.") from None
    finally:
        if own:
            await client.aclose()
    out = []
    for r in rows:
        nuts = {str(n) for n in r.get("nuts") or []}
        if r.get("type") != "cashu" or r.get("status") != "online" or not set(cashu.ESCROW_NUTS) <= nuts:
            continue
        try:
            out.append(cashu.checked_mint_url(str(r.get("url", ""))))
        except ValueError:
            continue
    return sorted(set(out))[:MOST_MINTS]
