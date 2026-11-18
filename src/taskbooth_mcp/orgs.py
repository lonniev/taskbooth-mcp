"""Organizations and their Projects: what a caller hands in, checked, and who may do what.

An Organization is owned by the npub that made it. Its owner adds members by
npub. Members see the Organization, its members and its Projects, see every
task filed in its Projects, and may post tasks into them; only the owner
changes the Organization, its members or its Projects.

A Project carries what a new task in it starts from: a brief, a budget in
sats, a default circle of npubs, and default cover terms. The site prefills a
new task from these; the Operator applies none of them on its own, so the
offer both parties sign always says exactly what was posted.

Pure: the store (:mod:`taskbooth_mcp.store`) keeps the rows.
"""

from __future__ import annotations

from typing import Any

MAX_ORGS_PER_OWNER = 20
MAX_MEMBERS = 200
MAX_PROJECTS = 100
MAX_CIRCLE = 500
#: Every sat there will ever be.
MAX_BUDGET_SATS = 2_100_000_000_000_000

OWNER = "owner"
MEMBER = "member"

#: The cover fields a Project may suggest. Deliverables, the spec and the deemed grade belong to each task.
DEFAULT_FIELDS = ("deemed_acceptance_days", "ip_option", "ip_custom_text", "confidential", "client_is_consumer",
                  "third_party_allowed", "governing_law", "venue", "arbiter_npub")


def standing_in(owner: str, members: set[str] | frozenset[str], npub: str) -> str:
    """``"owner"``, ``"member"``, or ``""`` for someone with no place in the Organization."""
    if npub == owner:
        return OWNER
    return MEMBER if npub in members else ""


def may_see(place: str) -> bool:
    """The owner and the members see the Organization, its members, its Projects and their tasks."""
    return place in (OWNER, MEMBER)


def may_post(place: str) -> bool:
    """The owner and the members may file a new task in one of its Projects."""
    return place in (OWNER, MEMBER)


def may_manage(place: str) -> bool:
    """Only the owner changes the Organization, its members or its Projects."""
    return place == OWNER


def checked_defaults(value: Any, *, text: Any, whole: Any, flag: Any, npub: Any, ip_options: tuple[str, ...],
                     max_days: int, max_text: int) -> dict[str, Any]:
    """A Project's default cover terms, checked as a cover page checks the same fields.

    ``arbiter_npub`` is the arbiter a new task in the Project names, whom the
    professional may accept or decline when they take it on.

    ``text``, ``whole``, ``flag`` and ``npub`` are the store's own checkers, so a default
    is refused for exactly the reasons the same value on a cover page would be.
    """
    if value is None or value == {}:
        return {}
    if not isinstance(value, dict):
        raise ValueError("A Project's default terms are an object of named fields.")
    unknown = sorted(set(value) - set(DEFAULT_FIELDS))
    if unknown:
        raise ValueError(f"A Project has no default {', '.join(unknown)}. Its defaults: {', '.join(DEFAULT_FIELDS)}.")
    ip = value.get("ip_option", "assign")
    if ip not in ip_options:
        raise ValueError(f"The IP option is one of: {', '.join(ip_options)}.")
    days = value.get("deemed_acceptance_days", 0)
    return {
        "deemed_acceptance_days": whole(days, "deemed-acceptance period in days", most=max_days) if days else 0,
        "ip_option": ip,
        "ip_custom_text": text(value.get("ip_custom_text", ""), "custom IP terms", max_text) if ip == "custom" else "",
        "confidential": flag(value.get("confidential", False), "confidentiality flag"),
        "client_is_consumer": flag(value.get("client_is_consumer", False), "consumer flag"),
        "third_party_allowed": flag(value.get("third_party_allowed", False), "third-party flag"),
        "governing_law": text(value.get("governing_law", ""), "governing law", 200),
        "venue": text(value.get("venue", ""), "venue", 200),
        "arbiter_npub": npub(value["arbiter_npub"], "default arbiter") if value.get("arbiter_npub") else "",
    }


def project_totals(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """What a Project's tasks have committed and paid, in sats, and how many stand where.

    ``tasks`` are viewed tasks (each with its ``standing``). A task still open or
    under way commits its top grade. One graded commits what it earned. One that
    closed without being graded commits only what was already paid ahead. Paid
    is what the performer confirmed arrived: the deposit, milestones, and the
    award once paid.
    """
    committed = paid = 0
    by_standing: dict[str, int] = {}
    for task in tasks:
        ahead = sum(int(a.get("sats") or 0) for a in [task.get("deposit") or {}, *(task.get("milestones") or [])]
                    if a.get("paid_at"))
        owed = int((task.get("assessment") or {}).get("sats") or 0)
        top = max((int(g["sats"]) for g in task["rubric"]["grades"]), default=0)
        state = task["standing"]
        if state in ("withdrawn", "rejected", "missed", "expired"):
            committed += ahead
        elif task.get("assessment"):
            committed += ahead + owed
        else:
            committed += top
        paid += ahead + (owed if state == "paid" else 0)
        by_standing[state] = by_standing.get(state, 0) + 1
    return {"committed_sats": committed, "paid_sats": paid, "tasks": len(tasks), "by_standing": by_standing}
