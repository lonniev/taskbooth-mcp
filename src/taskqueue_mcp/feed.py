"""A person's tasks as a calendar a phone can subscribe to.

A calendar client speaks HTTP and iCalendar, and has nowhere to carry an npub
proof. So a feed is reached by an unguessable token, and the token is its whole
credential: anyone holding it reads what the feed holds. For that reason a feed
names no person and carries no npub. It holds each task's title, its details,
the day it is due, and what it can earn.

The feed is built when it is asked for, from the tasks as they stand, so it is
never out of date and nothing runs on a timer.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tollbooth import ical

PRODID = "-//Tollbooth DPYC//Task Queue//EN"
UID_DOMAIN = "taskqueue.tollbooth-dpyc.com"

#: Feeds one person may keep at once.
MAX_FEEDS = 5

#: Standings left off a calendar: nobody has anything left to do on these.
_GONE = frozenset({"withdrawn", "rejected", "expired"})
#: Standings shown as finished.
_FINISHED = frozenset({"owed", "paid"})


def new_token() -> str:
    """An unguessable feed token: 128 bits, as 32 hex characters."""
    return secrets.token_hex(16)


def checked_token(value: Any) -> str:
    """A feed token as this Operator mints them, or ``ValueError``."""
    text = value.strip().lower() if isinstance(value, str) else ""
    if len(text) != 32 or any(c not in "0123456789abcdef" for c in text):
        raise ValueError("That is not a calendar feed.")
    return text


def token_key(token: str) -> str:
    """What a token is stored and looked up by, so the stored row alone opens no feed."""
    return hashlib.sha256(token.encode()).hexdigest()


def checked_zone(value: Any) -> str:
    """An IANA time zone name such as ``America/New_York``, or ``ValueError``.

    A task is due at the end of a day where its people live; the zone decides
    which day that is on the calendar.
    """
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > 64:
        raise ValueError("Name the time zone, like America/New_York.")
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(f"{text!r} is not a time zone this service knows.") from None
    return text


def _top_sats(task: dict[str, Any]) -> int:
    graded = task.get("assessment") or {}
    if "sats" in graded:
        return int(graded["sats"])
    return max((int(g["sats"]) for g in task["rubric"]["grades"]), default=0)


def chores_ics(tasks: list[dict[str, Any]], *, name: str, zone: str, token: str, at: int) -> str:
    """The tasks as an iCalendar feed: one all-day event on the day each is due.

    All-day events, since every calendar app shows them; many ignore to-dos in
    a subscribed feed. A task that is owed or paid is marked with a tick.
    """
    tz = ZoneInfo(zone)
    stamp = datetime.fromtimestamp(at, UTC)
    entries = []
    for task in tasks:
        if task["standing"] in _GONE:
            continue
        day = datetime.fromtimestamp(int(task["needed_by"]), tz).date()
        done = task["standing"] in _FINISHED
        sats = _top_sats(task)
        earns = f"Earned {sats} sats." if done else f"Earns up to {sats} sats."
        detail = "\n".join(part for part in (task.get("description") or "", earns) if part)
        entries.append(ical.vevent(
            ical.uid_for(token_key(token)[:16], "task", task["task_id"], domain=UID_DOMAIN),
            f"✓ {task['title']}" if done else task["title"],
            day,
            description=detail,
            stamp=stamp,
        ))
    return ical.calendar(name, entries, prodid=PRODID, refresh_hours=1)
