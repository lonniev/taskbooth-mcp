"""What an npub has done in each role on commissions, as counts anyone may read.

Only jobs under the Standard Terms count (a task with a cover page), so a
household chore says nothing about anyone. Nothing here names a job, its
title or its sats: a count is the whole answer, and the reader ranks it.
"""

from __future__ import annotations

from typing import Any

from taskbooth_mcp.lifecycle import APPEAL_DAYS, ARBITER_DAYS

__all__ = ["reputation_of"]

DAY = 86_400

_VERDICTS = ("verified", "concerns", "not_verified")
_GRADED = ("assessed", "appealed", "owed", "paid")


def _verdicts() -> dict[str, int]:
    return dict.fromkeys(_VERDICTS, 0)


def _grade_pct(task: dict[str, Any]) -> int | None:
    """The final grade's price as a share of the top grade's, before any late discount."""
    graded = task.get("assessment") or {}
    grades = (task.get("rubric") or {}).get("grades") or []
    top = max((int(g["sats"]) for g in grades), default=0)
    chosen = next((int(g["sats"]) for g in grades if g["name"] == graded.get("grade")), None)
    if not top or chosen is None:
        return None
    return round(100 * chosen / top)


def _client(tasks: list[dict[str, Any]]) -> dict[str, int]:
    out = {"commissions": len(tasks), "graded": 0, "graded_in_time": 0, "owed": 0, "paid": 0, "appeals_lost": 0}
    for t in tasks:
        graded = t.get("assessment") or {}
        if graded:
            out["graded"] += 1
            out["graded_in_time"] += 0 if graded.get("deemed") else 1
            appeal = graded.get("appeal") or {}
            if appeal.get("decided_at") and int(graded.get("sats", 0)) > int(appeal.get("first_sats", 0)):
                out["appeals_lost"] += 1
        if t["state"] in ("owed", "paid"):
            out["owed"] += 1
            out["paid"] += t["state"] == "paid"
    return out


def _artist(tasks: list[dict[str, Any]], failures: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "commissions": len(tasks), "delivered": 0, "on_time": 0, "late": 0, "failed_without_cause": 0,
        "failed_with_cause": 0, "appeals_won": 0, "appeals_lost": 0, "grade_pct": None, "verdicts": _verdicts(),
    }
    shares: list[int] = []
    for t in tasks:
        if t.get("accomplished_at"):
            out["delivered"] += 1
            on_time = int(t["accomplished_at"]) <= int(t["needed_by"])
            out["on_time" if on_time else "late"] += 1
        failure = failures.get(t["task_id"]) or {}
        if failure.get("without_cause") is True:
            out["failed_without_cause"] += 1
        elif failure.get("without_cause") is False:
            out["failed_with_cause"] += 1
        graded = t.get("assessment") or {}
        appeal = graded.get("appeal") or {}
        if appeal.get("decided_at"):
            won = int(graded.get("sats", 0)) > int(appeal.get("first_sats", 0))
            out["appeals_won" if won else "appeals_lost"] += 1
        if t["state"] in _GRADED and (pct := _grade_pct(t)) is not None:
            shares.append(pct)
        verdict = (t.get("inspection") or {}).get("verdict")
        if verdict in _VERDICTS:
            out["verdicts"][verdict] += 1
    if shares:
        out["grade_pct"] = round(sum(shares) / len(shares))
    return out


def _arbiter(tasks: list[dict[str, Any]]) -> dict[str, int]:
    out = {"named": len(tasks), "signed": 0, "decided": 0, "decided_in_time": 0}
    for t in tasks:
        out["signed"] += bool((t.get("agreement") or {}).get("arbiter"))
        asked_decided = [
            ((t.get("assessment") or {}).get("appeal") or {}, "at", "decided_at"),
            (t.get("cause") or {}, "at", "ruled_at"),
        ]
        for record, asked, decided in asked_decided:
            if record.get(decided):
                out["decided"] += 1
                out["decided_in_time"] += int(record[decided]) - int(record[asked]) <= ARBITER_DAYS * DAY
    return out


def _inspector(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {"named": len(tasks), "signed": 0, "opinions": 0, "opinions_in_time": 0,
                           "verdicts": _verdicts()}
    for t in tasks:
        out["signed"] += bool((t.get("agreement") or {}).get("inspector"))
        opinion = t.get("inspection") or {}
        if not opinion:
            continue
        out["opinions"] += 1
        out["verdicts"][opinion["verdict"]] += 1
        # In time is before the client must grade, so the opinion can still inform the grade.
        window = int(t["cover"].get("deemed_acceptance_days") or APPEAL_DAYS) * DAY
        out["opinions_in_time"] += int(opinion["at"]) <= int(t["accomplished_at"]) + window
    return out


def reputation_of(tasks: list[dict[str, Any]], npub: str, failures: dict[str, Any]) -> dict[str, Any]:
    """Counts of what ``npub`` did as client, artist, arbiter and inspector.

    ``failures`` maps a task id to where its failure to deliver stands, as the store reads it.
    """
    jobs = [t for t in tasks if t.get("cover")]
    return {
        "npub": npub,
        "client": _client([t for t in jobs if t["purchaser"] == npub]),
        "artist": _artist([t for t in jobs if t.get("performer") == npub], failures),
        "arbiter": _arbiter([t for t in jobs if t["cover"].get("arbiter_npub") == npub]),
        "inspector": _inspector([t for t in jobs if t["cover"].get("inspector_npub") == npub]),
    }
