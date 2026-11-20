"""Tasks and their whole history, on the operator's Neon schema.

A task is posted by a purchaser with a rubric, claimed by a performer, marked
done, assessed, and paid or rejected. :mod:`taskbooth_mcp.lifecycle` says which
of those moves is legal; this module is the only thing that writes one down.

What a person wrote and what anything is worth (titles, descriptions, rubrics,
grades, sats, reasons, invoices) is kept in one ``data_enc`` column per row,
sealed with the vault's cipher. The plain columns are ids, npubs, states and
times: what a query must match on, nothing more.

Three rules the SQL keeps:

- A move is a compare-and-swap on the task's ``version``. Of two people acting
  at once, one wins and the other is told to read the task again.
- ``taskbooth_events`` is append-only: one row per move, numbered by the
  version it produced, with the npub that caused it. Nothing here updates or
  deletes an event.
- There is no clock. A posted task past its needed-by time *reads* as expired,
  and a recurring task's next occurrence is created by the call that finishes
  the current one, or by the first read that finds it expired. A task whose
  purchaser chose "cancel if missed" is closed as missed by the first read or
  move after its deadline, the same way.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from taskbooth_mcp import feed, orgs, terms
from taskbooth_mcp.agreement import VERDICTS, inspection_hash, offer_hash, offer_statement
from taskbooth_mcp.escrow import FUNDED_STATES, INVOICED, SETTLED, escrow_summary
from taskbooth_mcp.lifecycle import (
    DEFAULT_WINDOW_HOURS,
    EXPIRED,
    LATE_KINDS,
    MAX_DEPOSIT_PCT,
    MAX_WINDOW_HOURS,
    WINDOWS,
    Action,
    Facts,
    Role,
    Standing,
    State,
    days_late,
    deposit_sats,
    hours_late,
    late_sats,
    next_needed_by,
    standing,
    step,
    step_sats,
    window_hours,
)
from taskbooth_mcp.reputation import reputation_of

MAX_TITLE = 200
MAX_TEXT = 4000
MAX_OPINION = 2000
#: Where delivered work stands, and so may be inspected: from delivery to payment, or rejected.
INSPECTABLE = (State.DONE, State.ASSESSED, State.APPEALED, State.OWED, State.PAID, State.REJECTED)
MAX_GRADES = 12
MAX_GRADE_SATS = 100_000_000
MAX_PEOPLE = 500
MAX_SIGNATURE = 4096
#: A purchaser may remind once a day and a half before the deadline, or after it.
NUDGE_DUE_WINDOW = 2 * 86_400
#: The least time between two reminders of one kind about one task.
NUDGE_GAP = 12 * 3_600
#: The task fields an edit may change, and nothing else.
EDITABLE = ("title", "description", "rubric", "audience", "needed_by", "every_days", "late", "deposit_pct",
            "cover", "milestones")

_TASK_COLUMNS = (
    "task_id, purchaser_npub, performer_npub, state, final, needed_by, series_id, occurrence, "
    "rolled, org_id, project_id, version, created_at, updated_at, data_enc"
)


class StoreUnavailable(Exception):
    """The store cannot answer; the caller should retry, never assume."""


class NotYours(LookupError):
    """No task with that id is the caller's to see."""


def new_id() -> str:
    return secrets.token_hex(8)


# ---------------------------------------------------------------------------
# What a caller hands in is checked before it is stored
# ---------------------------------------------------------------------------


def checked_npub(value: Any, what: str = "npub") -> str:
    """A well-formed npub, or ``ValueError``."""
    from pynostr.key import PublicKey

    if not isinstance(value, str) or not value.startswith("npub1") or len(value) > 80:
        raise ValueError(f"The {what} must be an npub (npub1…).")
    try:
        return str(PublicKey.from_npub(value).bech32())
    except Exception as exc:  # any decode failure is the same refusal
        raise ValueError(f"The {what} is not a valid npub.") from exc


def _text(value: Any, what: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"The {what} must be text.")
    text = value.strip()
    if required and not text:
        raise ValueError(f"The {what} is required.")
    if len(text) > limit:
        raise ValueError(f"The {what} is longer than {limit} characters.")
    return text


def _whole(value: Any, what: str, *, least: int = 0, most: int = 2**53) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not least <= value <= most:
        raise ValueError(f"The {what} must be a whole number from {least} to {most}.")
    return value


def checked_site(value: Any) -> str:
    """The site a task was posted from, e.g. "chipin": a short lowercase name, or "" for none.

    Several sites share this Operator. Each stamps the tasks it posts and lists
    its own; a task with no stamp is listed on every site.
    """
    site = _text(value, "site", 32)
    if site and not (site[0].isalpha() and all(c.isascii() and (c.islower() or c.isdigit() or c == "-") for c in site)):
        raise ValueError("A site is a short lowercase name such as chipin.")
    return site


def _on_site(task: dict[str, Any], site: str) -> bool:
    """Whether a site lists this task: its own, and every task no site stamped."""
    return not site or task.get("site", "") in ("", site)


def checked_rubric(rubric: Any) -> dict[str, Any]:
    """A rubric as it is stored: its grades, what each earns, and the appeal rule."""
    if not isinstance(rubric, dict) or not isinstance(rubric.get("grades"), list):
        raise ValueError("A rubric is an object with a list of grades.")
    grades = rubric["grades"]
    if not 1 <= len(grades) <= MAX_GRADES:
        raise ValueError(f"A rubric has from 1 to {MAX_GRADES} grades.")
    checked: list[dict[str, Any]] = []
    for grade in grades:
        if not isinstance(grade, dict):
            raise ValueError("Each grade is an object with a name, sats and criteria.")
        checked.append({
            "name": _text(grade.get("name"), "grade name", 60, required=True),
            "sats": _whole(grade.get("sats"), "grade's sats", most=MAX_GRADE_SATS),
            "criteria": _text(grade.get("criteria", ""), "grade's criteria", 500),
        })
    names = [g["name"].casefold() for g in checked]
    if len(set(names)) != len(names):
        raise ValueError("Two grades in a rubric cannot share a name.")
    return {"grades": checked, "appeal": _text(rubric.get("appeal", ""), "appeal rule", 500)}


#: The most late steps a task may set, and the latest hour one may start.
MAX_LATE_STEPS = 6
MAX_LATE_HOURS = 365 * 24


def checked_late_steps(value: Any) -> list[dict[str, int]]:
    """Late steps: each a percentage off once the work is more than ``after_hours`` late, both rising."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_LATE_STEPS:
        raise ValueError(f"Late steps are a list of 1 to {MAX_LATE_STEPS}.")
    steps: list[dict[str, int]] = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"after_hours", "pct_off"}:
            raise ValueError('Each late step is {"after_hours": <hours late>, "pct_off": <percent>}.')
        hours = _whole(entry["after_hours"], "hours late a step starts after", most=MAX_LATE_HOURS - 1)
        pct = _whole(entry["pct_off"], "percent a step takes off", least=1, most=100)
        if steps and (hours <= steps[-1]["after_hours"] or pct <= steps[-1]["pct_off"]):
            raise ValueError("Each late step starts later, and takes off more, than the one before.")
        steps.append({"after_hours": hours, "pct_off": pct})
    return steps


def checked_late(value: Any) -> dict[str, Any]:
    """What happens when the task is done late: pay in full, a percentage off each day, steps, or cancel."""
    if value is None or value == {}:
        return {"kind": "full"}
    if not isinstance(value, dict) or value.get("kind") not in LATE_KINDS:
        raise ValueError(f"The late rule is an object whose kind is one of: {', '.join(LATE_KINDS)}.")
    if value["kind"] == "discount":
        return {"kind": "discount", "pct_per_day": _whole(value.get("pct_per_day"), "percent off each day late",
                                                          least=1, most=100)}
    if value["kind"] == "steps":
        return {"kind": "steps", "steps": checked_late_steps(value.get("steps"))}
    return {"kind": value["kind"]}


#: What a cover page may say about who owns the finished work.
IP_OPTIONS = ("assign", "license", "worker_keeps", "custom")
_COVER_FIELDS = ("spec_url", "spec_hash", "deliverables", "deemed_grade", "deemed_acceptance_days", "ip_option",
                 "ip_custom_text", "confidential", "client_is_consumer", "third_party_allowed", "governing_law",
                 "venue", "arbiter_npub", "inspector_npub", "escrow", "escrow_mint", "refund_days",
                 "fail_after_hours", *WINDOWS)
MAX_DELIVERABLES = 20
MAX_DEEMED_DAYS = 60
MAX_REFUND_DAYS = 90


def fail_days(fail_after_hours: int) -> int:
    """The failure point in whole days, counting any part of a day as one."""
    return -(-fail_after_hours // 24)


def least_refund_days(deemed_days: int, fail_after_hours: int, appeal_hours: int, ruling_hours: int) -> int:
    """The shortest refund period that cannot cut an appeal or a claim of cause short.

    The client alone may take the escrow back from the refund date (terms 6.6),
    so it must fall after the latest moment the arbiter could still be deciding:
    a delivery just before the failure point, a grade on the last day of the
    deemed-acceptance period, an appeal at the end of its window, and the
    arbiter's ruling time. A failure to deliver needs no more: its claim of
    cause takes the same window and its ruling the same time, from earlier.
    """
    return deemed_days + fail_days(fail_after_hours + appeal_hours + ruling_hours)
MAX_MILESTONES = 6


def _flag(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"The {what} is true or false.")
    return value


def checked_escrow(value: dict[str, Any], *, purchaser: str, mints: tuple[str, ...], deemed_days: int = 0,
                   fail_after_hours: int = 0, appeal_hours: int = 0, ruling_hours: int = 0) -> dict[str, Any]:
    """The arbiter and escrow fields of a cover page.

    Escrow needs a named arbiter (without one the dates decide and the job stays
    Lightning-only), a deemed grade (a client who never grades must not hold
    the escrow hostage), a failure point (a job never delivered must end), a
    mint this Operator offers, and a refund date late enough that an appeal is
    settled before it (``least_refund_days``).
    """
    from tollbooth.cashu import checked_mint_url

    arbiter = checked_npub(value["arbiter_npub"], "arbiter") if value.get("arbiter_npub") else ""
    if arbiter and arbiter == purchaser:
        raise ValueError("The arbiter must be someone other than the purchaser.")
    held = _flag(value.get("escrow", False), "escrow flag")
    mint, days = value.get("escrow_mint", ""), value.get("refund_days", 0)
    if not held:
        if mint or days:
            raise ValueError("A mint and a refund period belong only to a job held in escrow.")
        return {"arbiter_npub": arbiter, "escrow": False, "escrow_mint": "", "refund_days": 0}
    if not arbiter:
        raise ValueError("Escrow needs a named arbiter. Without one the job is paid by Lightning alone.")
    if not deemed_days:
        raise ValueError("Escrow needs a deemed grade and a deemed-acceptance period, so a silent client "
                         "cannot hold the escrow until the refund date.")
    if not fail_after_hours:
        raise ValueError("Escrow needs a failure point (fail_after_hours), so work never delivered ends the job "
                         "and its escrow can be settled.")
    if not mints:
        raise ValueError("This Operator offers no escrow mint yet.")
    try:
        mint = checked_mint_url(_text(mint, "escrow mint", 300, required=True))
    except ValueError as exc:
        raise ValueError(f"The escrow mint: {exc}.") from None
    if mint not in mints:
        raise ValueError(f"The escrow mint must be one this Operator offers: {', '.join(mints)}.")
    least = least_refund_days(deemed_days, fail_after_hours, appeal_hours, ruling_hours)
    if isinstance(days, int) and not isinstance(days, bool) and 0 < days < least:
        raise ValueError(
            f"The refund period must be at least {least} days: the {deemed_days}-day deemed-acceptance period, "
            f"then {fail_after_hours} hours to the failure point, the {appeal_hours}-hour appeal window and the "
            f"arbiter's {ruling_hours}-hour ruling time, rounded up to whole days. Before then the client alone "
            "could take the escrow back mid-appeal.")
    days = _whole(days, "refund period in days", least=least, most=MAX_REFUND_DAYS)
    return {"arbiter_npub": arbiter, "escrow": True, "escrow_mint": mint, "refund_days": days}


def checked_cover(value: Any, rubric: dict[str, Any], *, purchaser: str = "",
                  mints: tuple[str, ...] = ()) -> dict[str, Any] | None:
    """A job's cover page as it is stored, or ``None`` for a task that has none.

    The Standard Terms' version and hash are stamped here, never taken from the caller.
    ``mints`` are the escrow mints this Operator offers.
    """
    if value is None or value == {}:
        return None
    if not isinstance(value, dict):
        raise ValueError("A cover page is an object of named fields.")
    unknown = sorted(set(value) - set(_COVER_FIELDS))
    if unknown:
        raise ValueError(f"A cover page has no field {', '.join(unknown)}. Its fields: {', '.join(_COVER_FIELDS)}.")

    url = _text(value.get("spec_url", ""), "spec URL", 500)
    if url and (not url.startswith("https://") or any(c.isspace() for c in url)):
        raise ValueError("The spec URL must be an https:// address.")
    digest = _text(value.get("spec_hash", ""), "spec hash", 64)
    if digest and (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
        raise ValueError("The spec hash is a sha256 in 64 lowercase hex characters.")
    items = value.get("deliverables", [])
    if not isinstance(items, list) or len(items) > MAX_DELIVERABLES:
        raise ValueError(f"The deliverables are a list of at most {MAX_DELIVERABLES}.")
    deemed = value.get("deemed_grade", "")
    if deemed:
        deemed = _grade(rubric, deemed)[0]["name"]
    days = value.get("deemed_acceptance_days", 0)
    if deemed:
        days = _whole(days, "deemed-acceptance period in days", least=1, most=MAX_DEEMED_DAYS)
    elif days:
        raise ValueError("A deemed-acceptance period needs a deemed grade to award when it runs out.")
    ip = value.get("ip_option", "assign")
    if ip not in IP_OPTIONS:
        raise ValueError(f"The IP option is one of: {', '.join(IP_OPTIONS)}.")
    custom = _text(value.get("ip_custom_text", ""), "custom IP terms", MAX_TEXT, required=ip == "custom")
    # Hours past the needed-by time after which undelivered work is a failure to deliver (terms 14.7); 0 for none.
    fail = _whole(value.get("fail_after_hours", 0), "failure point in hours late", most=MAX_LATE_HOURS)
    # How long to appeal or claim cause, for the arbiter to rule, and for the client to pay (terms 4.6, 14.8, 7.2).
    names = {"appeal_hours": "appeal window", "ruling_hours": "arbiter's ruling time", "pay_hours": "payment window"}
    windows = {w: _whole(value.get(w, DEFAULT_WINDOW_HOURS), f"{names[w]} in hours", least=1, most=MAX_WINDOW_HOURS)
               for w in WINDOWS}
    held = checked_escrow(value, purchaser=purchaser, mints=mints, deemed_days=days, fail_after_hours=fail,
                          appeal_hours=windows["appeal_hours"], ruling_hours=windows["ruling_hours"])
    # An independent party who signs the offer and may give one signed opinion of the work (terms 8).
    inspector = checked_npub(value["inspector_npub"], "inspector") if value.get("inspector_npub") else ""
    if inspector and inspector in (purchaser, held["arbiter_npub"]):
        raise ValueError("The inspector must be someone other than the purchaser and the arbiter.")

    return {
        "terms_version": terms.CURRENT,
        "terms_hash": terms.terms_hash(),
        "spec_url": url,
        "spec_hash": digest,
        "deliverables": [_text(d, "deliverable", 200, required=True) for d in items],
        "deemed_grade": deemed,
        "deemed_acceptance_days": days,
        "ip_option": ip,
        "ip_custom_text": custom if ip == "custom" else "",
        "confidential": _flag(value.get("confidential", False), "confidentiality flag"),
        "client_is_consumer": _flag(value.get("client_is_consumer", False), "consumer flag"),
        "third_party_allowed": _flag(value.get("third_party_allowed", False), "third-party flag"),
        "governing_law": _text(value.get("governing_law", ""), "governing law", 200),
        "venue": _text(value.get("venue", ""), "venue", 200),
        "fail_after_hours": fail,
        **windows,
        "inspector_npub": inspector,
        **held,
    }


def checked_milestones(value: Any, *, deposit_pct: int, needed_by: int, at: int) -> list[dict[str, Any]]:
    """Dated part-payments of the top grade, soonest first.

    With the deposit they ask for at most :data:`MAX_DEPOSIT_PCT` of the top
    grade, so the grade always has something left to decide.
    """
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_MILESTONES:
        raise ValueError(f"Milestones are a list of at most {MAX_MILESTONES}.")
    checked = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("Each milestone is an object with a name, a due_at time and a pct.")
        due = _whole(entry.get("due_at"), "milestone's due_at time")
        if not at < due <= needed_by:
            raise ValueError("A milestone falls due after now and no later than the needed-by time.")
        checked.append({
            "name": _text(entry.get("name"), "milestone name", 60, required=True),
            "due_at": due,
            "pct": _whole(entry.get("pct"), "milestone's percentage", least=1, most=MAX_DEPOSIT_PCT),
        })
    if deposit_pct + sum(m["pct"] for m in checked) > MAX_DEPOSIT_PCT:
        raise ValueError(f"The deposit and milestones together ask for at most {MAX_DEPOSIT_PCT}% of the top grade.")
    return sorted(checked, key=lambda m: m["due_at"])


def _grade(rubric: dict[str, Any], name: Any) -> tuple[dict[str, Any], bool]:
    """The named grade, and whether it is the rubric's highest."""
    wanted = _text(name, "grade", 60, required=True).casefold()
    most = max(g["sats"] for g in rubric["grades"])
    for grade in rubric["grades"]:
        if grade["name"].casefold() == wanted:
            return grade, grade["sats"] == most
    offered = ", ".join(g["name"] for g in rubric["grades"])
    raise ValueError(f"The rubric has no grade named that. Its grades are: {offered}.")


class TaskStore:
    """Neon-backed tasks. Every read and every move names the npub asking."""

    def __init__(self, neon_vault: Any) -> None:
        cipher = getattr(neon_vault, "_cipher", None)
        if cipher is None:
            raise StoreUnavailable("the vault has no cipher; tasks would be stored in the clear")
        self._neon = neon_vault
        self._cipher = cipher

    # -- plumbing ------------------------------------------------------------

    def _t(self, table: str) -> str:
        return str(self._neon._t(table))

    async def _rows(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        result = await self._neon._execute(sql, params)
        return list(result.get("rows") or [])

    def _seal(self, data: dict[str, Any]) -> str:
        return str(self._cipher.encrypt(json.dumps(data, separators=(",", ":"))))

    def _open(self, sealed: str) -> dict[str, Any]:
        opened = json.loads(self._cipher.decrypt(sealed))
        return opened if isinstance(opened, dict) else {}

    async def ensure_schema(self) -> None:
        for sql in (
            (f"CREATE TABLE IF NOT EXISTS {self._t('taskbooth_tasks')} ("
             "task_id TEXT PRIMARY KEY, purchaser_npub TEXT NOT NULL, performer_npub TEXT, "
             "state TEXT NOT NULL, final INTEGER NOT NULL, needed_by BIGINT NOT NULL, "
             "series_id TEXT NOT NULL, occurrence INTEGER NOT NULL, rolled INTEGER NOT NULL, "
             "org_id TEXT, project_id TEXT, version INTEGER NOT NULL, "
             "created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL, data_enc TEXT NOT NULL, "
             "UNIQUE (series_id, occurrence))"),
            f"CREATE INDEX IF NOT EXISTS idx_taskbooth_tasks_purchaser ON {self._t('taskbooth_tasks')}(purchaser_npub)",
            f"CREATE INDEX IF NOT EXISTS idx_taskbooth_tasks_performer ON {self._t('taskbooth_tasks')}(performer_npub)",
            (f"CREATE TABLE IF NOT EXISTS {self._t('taskbooth_people')} ("
             "task_id TEXT NOT NULL, npub TEXT NOT NULL, role TEXT NOT NULL, "
             "PRIMARY KEY (task_id, npub, role))"),
            f"CREATE INDEX IF NOT EXISTS idx_taskbooth_people_npub ON {self._t('taskbooth_people')}(npub)",
            (f"CREATE TABLE IF NOT EXISTS {self._t('taskbooth_events')} ("
             "task_id TEXT NOT NULL, seq INTEGER NOT NULL, at BIGINT NOT NULL, actor_npub TEXT NOT NULL, "
             "action TEXT NOT NULL, from_state TEXT NOT NULL, to_state TEXT NOT NULL, "
             "detail_enc TEXT NOT NULL, PRIMARY KEY (task_id, seq))"),
            (f"CREATE TABLE IF NOT EXISTS {self._t('taskbooth_payouts')} ("
             "task_id TEXT PRIMARY KEY, paid_at BIGINT, data_enc TEXT NOT NULL)"),
            (f"CREATE TABLE IF NOT EXISTS {self._t('taskbooth_rubric_templates')} ("
             "template_id TEXT PRIMARY KEY, owner_npub TEXT NOT NULL, data_enc TEXT NOT NULL, "
             "created_at BIGINT NOT NULL)"),
            (f"CREATE INDEX IF NOT EXISTS idx_taskbooth_templates_owner "
             f"ON {self._t('taskbooth_rubric_templates')}(owner_npub)"),
            (f"CREATE TABLE IF NOT EXISTS {self._t('taskbooth_feeds')} ("
             "token_key TEXT PRIMARY KEY, owner_npub TEXT NOT NULL, created_at BIGINT NOT NULL, "
             "fetches INTEGER NOT NULL, last_fetch BIGINT, data_enc TEXT NOT NULL)"),
            f"CREATE INDEX IF NOT EXISTS idx_taskbooth_feeds_owner ON {self._t('taskbooth_feeds')}(owner_npub)",
            f"CREATE INDEX IF NOT EXISTS idx_taskbooth_tasks_org ON {self._t('taskbooth_tasks')}(org_id)",
            f"CREATE INDEX IF NOT EXISTS idx_taskbooth_tasks_project ON {self._t('taskbooth_tasks')}(project_id)",
            (f"CREATE TABLE IF NOT EXISTS {self._t('taskbooth_orgs')} ("
             "org_id TEXT PRIMARY KEY, owner_npub TEXT NOT NULL, created_at BIGINT NOT NULL, data_enc TEXT NOT NULL)"),
            f"CREATE INDEX IF NOT EXISTS idx_taskbooth_orgs_owner ON {self._t('taskbooth_orgs')}(owner_npub)",
            (f"CREATE TABLE IF NOT EXISTS {self._t('taskbooth_org_members')} ("
             "org_id TEXT NOT NULL, npub TEXT NOT NULL, data_enc TEXT NOT NULL, PRIMARY KEY (org_id, npub))"),
            f"CREATE INDEX IF NOT EXISTS idx_taskbooth_org_members_npub ON {self._t('taskbooth_org_members')}(npub)",
            (f"CREATE TABLE IF NOT EXISTS {self._t('taskbooth_projects')} ("
             "project_id TEXT PRIMARY KEY, org_id TEXT NOT NULL, created_at BIGINT NOT NULL, "
             "archived INTEGER NOT NULL, data_enc TEXT NOT NULL)"),
            f"CREATE INDEX IF NOT EXISTS idx_taskbooth_projects_org ON {self._t('taskbooth_projects')}(org_id)",
        ):
            await self._neon._execute(sql, [])

    # -- reading -------------------------------------------------------------

    def _task(self, row: dict[str, Any]) -> dict[str, Any]:
        """A stored row as a task: plain columns, then what was sealed."""
        return {
            "task_id": row["task_id"],
            "purchaser": row["purchaser_npub"],
            "performer": row["performer_npub"],
            "state": row["state"],
            "final": bool(row["final"]),
            "needed_by": int(row["needed_by"]),
            "series_id": row["series_id"],
            "occurrence": int(row["occurrence"]),
            "rolled": bool(row["rolled"]),
            "org_id": row["org_id"] or "",
            "project_id": row["project_id"] or "",
            "version": int(row["version"]),
            "created_at": int(row["created_at"]),
            "updated_at": int(row["updated_at"]),
            **self._open(row["data_enc"]),
        }

    async def _load(self, task_id: str) -> dict[str, Any] | None:
        rows = await self._rows(
            f"SELECT {_TASK_COLUMNS} FROM {self._t('taskbooth_tasks')} WHERE task_id = $1", [str(task_id)]
        )
        return self._task(rows[0]) if rows else None

    @staticmethod
    def _roles(task: dict[str, Any], npub: str, orgs_in: frozenset[str] = frozenset()) -> frozenset[Role]:
        """Every role this npub holds on this task. One npub may hold several.

        ``orgs_in`` are the Organizations ``npub`` owns or belongs to: a task filed
        under one of them is theirs to see, as a member.
        """
        roles: set[Role] = set()
        if task.get("org_id") and task["org_id"] in orgs_in:
            roles.add(Role.MEMBER)
        if npub == task["purchaser"]:
            roles.add(Role.PURCHASER)
        if npub == task["performer"]:
            roles.add(Role.PERFORMER)
        if npub in task["audience"]:
            roles.add(Role.AUDIENCE)
        if npub in task["assessors"]:
            roles.add(Role.ASSESSOR)
        if npub == task["tie_breaker"]:
            roles.add(Role.TIE_BREAKER)
        if npub and npub == (task.get("cover") or {}).get("arbiter_npub"):
            roles.add(Role.ARBITER)
        if npub and npub == (task.get("cover") or {}).get("inspector_npub"):
            roles.add(Role.INSPECTOR)
        return frozenset(roles)

    @staticmethod
    def _may_see(task: dict[str, Any], roles: frozenset[Role]) -> bool:
        """The audience sees a task only while it is open to claim."""
        if not roles:
            return False
        return roles != {Role.AUDIENCE} or task["state"] == State.POSTED

    def _view(self, task: dict[str, Any], roles: frozenset[Role], at: int) -> dict[str, Any]:
        now = Standing(State(task["state"]), task["final"])
        view = {k: v for k, v in task.items() if k not in ("version", "rolled")}
        view["standing"] = standing(now, task["needed_by"], at)
        view["your_roles"] = sorted(r.value for r in roles)
        view.setdefault("late", {"kind": "full"})
        view["days_late"] = self._days_late(task, at)
        view.setdefault("nudges", {})
        view.setdefault("deposit_pct", 0)
        view.setdefault("cover", None)
        view.setdefault("milestones", [])
        signed = task.get("agreement") or {}
        view["agreement"] = {"purchaser_signed": bool(signed.get("purchaser")),
                             "performer_signed": bool(signed.get("performer")),
                             "arbiter_signed": bool(signed.get("arbiter")),
                             "inspector_signed": bool(signed.get("inspector"))}
        view["inspection"] = {k: v for k, v in (task.get("inspection") or {}).items() if k != "record"} or None
        view.pop("named_tie_breaker", None)
        if task.get("escrow"):
            # Blinding factors and proofs stay here; a viewer sees where the escrow stands.
            view["escrow"] = escrow_summary(task["escrow"], at)
        view.pop("cause", None)
        view["failure"] = self._failure_view(task, at)
        if view["failure"] and view["failure"]["without_cause"] and task.get("escrow"):
            # Terms 14.10: the arbiter signs with the client to release the whole escrow to the client.
            view["escrow_due_to"] = "client"
        return view

    @staticmethod
    def _days_late(task: dict[str, Any], at: int) -> int:
        """How late the work is or was: to when it was done, or to now while it is still being done.

        A task nobody claimed is expired, not late; one cancelled or missed is
        no longer anyone's work.
        """
        if task.get("accomplished_at"):
            return days_late(task["needed_by"], int(task["accomplished_at"]))
        if task["state"] == State.CLAIMED:
            return days_late(task["needed_by"], at)
        return 0

    @staticmethod
    def _paid_ahead(task: dict[str, Any]) -> int:
        """The sats the performer confirmed arrived before grading: the deposit and any milestones."""
        ahead = [task.get("deposit") or {}, *(task.get("milestones") or [])]
        return sum(int(a["sats"]) for a in ahead if a.get("paid_at"))

    @staticmethod
    def _wants_deemed(task: dict[str, Any], at: int) -> bool:
        """Finished work the purchaser did not grade within the cover page's deemed-acceptance period."""
        cover = task.get("cover") or {}
        return bool(
            task["state"] == State.DONE and cover.get("deemed_grade") and task.get("accomplished_at")
            and int(task["accomplished_at"]) + int(cover["deemed_acceptance_days"]) * 86_400 < at
        )

    async def _deem(self, task: dict[str, Any], at: int) -> None:
        """Grade it at the deemed grade, as the terms say. A lost race means someone graded it first."""
        try:
            await self.act(task["purchaser"], task["task_id"], Action.ASSESS, grade=task["cover"]["deemed_grade"],
                           as_operator=True, deemed=True, at=at)
        except ValueError:
            return

    @staticmethod
    def _appeal_ends(task: dict[str, Any]) -> int | None:
        """When the appeal window for a provisional grade closes, for a job under the Standard Terms (4.6)."""
        graded = (task.get("assessment") or {}).get("at")
        if not task.get("cover") or graded is None:
            return None
        return int(graded) + window_hours(task["cover"], "appeal_hours") * 3_600

    @classmethod
    def _wants_lapse(cls, task: dict[str, Any], at: int) -> bool:
        """A provisional grade the performer neither accepted nor appealed in time."""
        ends = cls._appeal_ends(task)
        return bool(task["state"] == State.ASSESSED and not task["final"] and ends is not None and ends < at)

    async def _lapse(self, task: dict[str, Any], at: int) -> None:
        """Accept the grade for the performer, as the terms say. A lost race means they moved first."""
        try:
            await self.act(task["performer"], task["task_id"], Action.ACCEPT, as_operator=True, lapsed=True, at=at)
        except ValueError:
            return

    @staticmethod
    def _wants_miss(task: dict[str, Any], at: int) -> bool:
        """A cancel-if-missed task whose deadline passed before it was done, or a job still
        undelivered at its cover page's failure point (terms 14.7)."""
        if ((task.get("late") or {}).get("kind") == "cancel" and task["state"] in (State.POSTED, State.CLAIMED)
                and task["needed_by"] < at):
            return True
        fail = int((task.get("cover") or {}).get("fail_after_hours") or 0)
        return bool(fail and task["state"] == State.CLAIMED and task["needed_by"] + fail * 3_600 < at)

    @staticmethod
    def _failure_view(task: dict[str, Any], at: int) -> dict[str, Any] | None:
        """Where a failure to deliver stands: when cause may be claimed until, and whether it is without cause.

        ``without_cause`` is ``None`` while a claim may still come or awaits the arbiter's ruling.
        """
        failure = task.get("failure")
        if not failure:
            return None
        arbiter = (task.get("cover") or {}).get("arbiter_npub")
        cause = task.get("cause") or {}
        ends = int(failure["at"]) + window_hours(task.get("cover"), "appeal_hours") * 3_600 if arbiter else None
        if not arbiter or cause.get("ruling") == "rejected" or (not cause and ends is not None and ends < at):
            without: bool | None = True
        elif cause.get("ruling") == "upheld":
            without = False
        else:
            without = None
        return {"at": failure["at"], "cause_ends": ends, "cause": cause or None, "without_cause": without}

    async def _miss(self, task: dict[str, Any], at: int) -> None:
        """Close a missed task, as the purchaser's own rule says. A lost race means someone else did it."""
        now = step(Standing(State(task["state"]), task["final"]), Action.MISS, {Role.OPERATOR})
        data = {k: task[k] for k in self._sealed_keys(task)}
        data.pop("nudges", None)
        failed = bool(task.get("cover")) and task["state"] == State.CLAIMED
        if failed:
            # A job under the Standard Terms that was claimed and never delivered: a failure to deliver (14.7).
            data["failure"] = {"at": at}
        moved = await self._rows(
            f"UPDATE {self._t('taskbooth_tasks')} SET state = $1, final = $2, data_enc = $3, "
            "version = version + 1, updated_at = $4 WHERE task_id = $5 AND version = $6 RETURNING version",
            [now.state.value, int(now.final), self._seal(data), at, task["task_id"], task["version"]],
        )
        if not moved:
            return
        # The purchaser's rule closed it, so the event names the purchaser.
        await self._record(
            task["task_id"], task["version"] + 1, at, task["purchaser"], Action.MISS.value,
            task["state"], now.state.value, {"failure": True} if failed else {},
        )

    async def get(self, npub: str, task_id: str, at: int | None = None) -> dict[str, Any]:
        """The task as ``npub`` may see it, or :class:`NotYours`."""
        at = int(time.time()) if at is None else at
        task = await self._load(task_id)
        roles = self._roles(task, npub, await self.orgs_in(npub)) if task else frozenset()
        if task is None or not self._may_see(task, roles):
            raise NotYours(f"No task {task_id!r} is yours to see.")
        if self._wants_miss(task, at):
            await self._miss(task, at)
            task = await self._load(task_id) or task
            if task["every_days"] and not task["rolled"]:
                await self._recur(task, npub, at)
        if self._wants_deemed(task, at):
            await self._deem(task, at)
            task = await self._load(task_id) or task
        if self._wants_lapse(task, at):
            await self._lapse(task, at)
            task = await self._load(task_id) or task
        return self._view(task, roles, at)

    async def tasks_of(self, npub: str, at: int | None = None, site: str = "") -> list[dict[str, Any]]:
        """Every task ``npub`` posted, performs, grades, or may claim, and every task filed in their
        Organizations; soonest needed first.

        A recurring task found expired is given its next occurrence here, by
        this read, since nothing runs in the background to do it.
        """
        at = int(time.time()) if at is None else at
        for _ in range(2):
            rows = await self._rows(
                f"SELECT {_TASK_COLUMNS} FROM {self._t('taskbooth_tasks')} WHERE purchaser_npub = $1 "
                f"OR performer_npub = $1 OR task_id IN "
                f"(SELECT task_id FROM {self._t('taskbooth_people')} WHERE npub = $1) "
                f"OR org_id IN (SELECT org_id FROM {self._t('taskbooth_orgs')} WHERE owner_npub = $1) "
                f"OR org_id IN (SELECT org_id FROM {self._t('taskbooth_org_members')} WHERE npub = $1) "
                "ORDER BY needed_by, task_id",
                [npub],
            )
            tasks = [self._task(r) for r in rows]
            missed = [t for t in tasks if self._wants_miss(t, at)]
            stale = [t for t in tasks if self._wants_next(t, at)]
            deemed = [t for t in tasks if self._wants_deemed(t, at)]
            lapsed = [t for t in tasks if self._wants_lapse(t, at)]
            if not missed and not stale and not deemed and not lapsed:
                break
            for task in deemed:
                await self._deem(task, at)
            for task in lapsed:
                await self._lapse(task, at)
            for task in missed:
                await self._miss(task, at)
            for task in stale:
                await self._recur(task, npub, at)
        orgs_in = await self.orgs_in(npub)
        seen = [(t, self._roles(t, npub, orgs_in)) for t in tasks]
        site = checked_site(site)
        return [self._view(t, roles, at) for t, roles in seen if self._may_see(t, roles) and _on_site(t, site)]

    async def events(self, npub: str, task_id: str) -> list[dict[str, Any]]:
        """The task's history, oldest first, for anyone with a part in it beyond the audience."""
        task = await self._load(task_id)
        roles = self._roles(task, npub, await self.orgs_in(npub)) if task else frozenset()
        if task is None or not roles - {Role.AUDIENCE}:
            raise NotYours(f"No task {task_id!r} is yours to see.")
        rows = await self._rows(
            f"SELECT seq, at, actor_npub, action, from_state, to_state, detail_enc "
            f"FROM {self._t('taskbooth_events')} WHERE task_id = $1 ORDER BY seq",
            [task_id],
        )
        return [
            {"seq": int(r["seq"]), "at": int(r["at"]), "actor": r["actor_npub"], "action": r["action"],
             "from": r["from_state"], "to": r["to_state"], **self._open(r["detail_enc"])}
            for r in rows
        ]

    # -- posting -------------------------------------------------------------

    async def post(
        self,
        purchaser: str,
        *,
        title: str,
        rubric: dict[str, Any],
        audience: list[str],
        needed_by: int,
        description: str = "",
        earliest_start: int = 0,
        every_days: int = 0,
        tie_breaker: str = "",
        assessors: list[str] | None = None,
        org_id: str = "",
        project_id: str = "",
        purchaser_signature: str = "",
        site: str = "",
        late: dict[str, Any] | None = None,
        deposit_pct: int = 0,
        cover: dict[str, Any] | None = None,
        milestones: list[dict[str, Any]] | None = None,
        escrow_mints: tuple[str, ...] = (),
        at: int | None = None,
    ) -> str:
        """Post a task. The purchaser may name themselves in the audience.

        ``escrow_mints`` are the mints this Operator offers for escrow.
        """
        at = int(time.time()) if at is None else at
        purchaser = checked_npub(purchaser, "purchaser")
        needed_by, data = self._terms(
            purchaser, title=title, rubric=rubric, audience=audience, needed_by=needed_by,
            description=description, earliest_start=earliest_start, every_days=every_days,
            tie_breaker=tie_breaker, assessors=assessors, signature=purchaser_signature, late=late,
            deposit_pct=deposit_pct, cover=cover, milestones=milestones, mints=escrow_mints, at=at,
        )
        data["site"] = checked_site(site)
        org_id, project_id = await self._filing(purchaser, org_id, project_id)
        if data["cover"] and not project_id:
            raise ValueError("A commission is filed in a Project of an Organization you are in: "
                             "pass its org_id and project_id.")
        task_id = new_id()
        await self._insert(
            task_id=task_id, purchaser=purchaser, needed_by=needed_by, series_id=task_id, occurrence=0,
            org_id=org_id, project_id=project_id,
            data=data, actor=purchaser, action="post", at=at,
        )
        return task_id

    @staticmethod
    def _terms(
        purchaser: str, *, title: Any, rubric: Any, audience: Any, needed_by: Any, description: Any,
        earliest_start: Any, every_days: Any, tie_breaker: Any, assessors: Any, signature: Any,
        late: Any, at: int, deposit_pct: Any = 0, cover: Any = None, milestones: Any = None,
        mints: tuple[str, ...] = (),
    ) -> tuple[int, dict[str, Any]]:
        """A task's terms checked as a post or an edit hands them in: its needed-by time and sealed data.

        A cover page that names an arbiter makes the arbiter the one who decides an
        appeal (terms 4.7), in place of any tie-breaker named.
        """
        if not isinstance(audience, list) or not 1 <= len(audience) <= MAX_PEOPLE:
            raise ValueError(f"A task names from 1 to {MAX_PEOPLE} npubs who may claim it.")
        delegates = assessors or []
        if not isinstance(delegates, list) or len(delegates) > MAX_PEOPLE:
            raise ValueError(f"A task names at most {MAX_PEOPLE} assessors.")
        needed_by = _whole(needed_by, "needed-by time")
        if needed_by <= at:
            raise ValueError("The needed-by time must be in the future.")
        earliest_start = _whole(earliest_start, "earliest-start time")
        if earliest_start > needed_by:
            raise ValueError("The earliest-start time cannot be after the needed-by time.")

        rubric = checked_rubric(rubric)
        deposit_pct = _whole(deposit_pct, "deposit percentage", most=MAX_DEPOSIT_PCT)
        page = checked_cover(cover, rubric, purchaser=purchaser, mints=mints)
        late = checked_late(late)
        fail = (page or {}).get("fail_after_hours", 0)
        if fail and late["kind"] == "steps" and late["steps"][-1]["after_hours"] >= fail:
            raise ValueError("The failure point comes after the last late step.")
        named = checked_npub(tie_breaker, "tie-breaker") if tie_breaker else purchaser
        return needed_by, {
            "title": _text(title, "title", MAX_TITLE, required=True),
            "description": _text(description, "description", MAX_TEXT),
            "rubric": rubric,
            "audience": sorted({checked_npub(n, "audience member") for n in audience}),
            # The purchaser always grades; anyone else named here grades on their behalf.
            "assessors": sorted({purchaser, *(checked_npub(n, "assessor") for n in delegates)}),
            "tie_breaker": (page or {}).get("arbiter_npub") or named,
            "named_tie_breaker": named,
            "earliest_start": earliest_start,
            "every_days": _whole(every_days, "repeat interval in days", most=3660),
            "signatures": {"purchaser": _text(signature, "signature", MAX_SIGNATURE), "performer": ""},
            "late": late,
            # Part of the top grade paid when the task is claimed; 0 asks for none.
            "deposit_pct": deposit_pct,
            "cover": page,
            "milestones": checked_milestones(milestones, deposit_pct=deposit_pct, needed_by=needed_by, at=at),
        }

    async def _insert(
        self, *, task_id: str, purchaser: str, needed_by: int, series_id: str, occurrence: int,
        org_id: str, project_id: str, data: dict[str, Any], actor: str, action: str, at: int,
    ) -> bool:
        """Write a new posted task, its people and its first event. False if the occurrence exists."""
        made = await self._rows(
            f"INSERT INTO {self._t('taskbooth_tasks')} ({_TASK_COLUMNS}) VALUES "
            "($1, $2, NULL, $3, 0, $4, $5, $6, 0, $7, $8, 1, $9, $9, $10) "
            "ON CONFLICT (series_id, occurrence) DO NOTHING RETURNING task_id",
            [task_id, purchaser, State.POSTED.value, needed_by, series_id, occurrence,
             org_id, project_id, at, self._seal(data)],
        )
        if not made:
            return False
        people = (
            [(n, Role.AUDIENCE) for n in data["audience"]]
            + [(n, Role.ASSESSOR) for n in data["assessors"]]
            + [(data["tie_breaker"], Role.TIE_BREAKER)]
            + self._inspector_row(data)
        )
        for npub, role in people:
            await self._neon._execute(
                f"INSERT INTO {self._t('taskbooth_people')} (task_id, npub, role) VALUES ($1, $2, $3) "
                "ON CONFLICT (task_id, npub, role) DO NOTHING",
                [task_id, npub, role.value],
            )
        await self._record(task_id, 1, at, actor, action, "", State.POSTED.value, {})
        return True

    @staticmethod
    def _inspector_row(data: dict[str, Any]) -> list[tuple[str, Role]]:
        """The inspector a cover page names, as a person on the task, so their list finds it."""
        named = (data.get("cover") or {}).get("inspector_npub")
        return [(named, Role.INSPECTOR)] if named else []

    async def _record(
        self, task_id: str, seq: int, at: int, actor: str, action: str, was: str, now: str,
        detail: dict[str, Any],
    ) -> None:
        """Append one event. Events are only ever inserted."""
        await self._neon._execute(
            f"INSERT INTO {self._t('taskbooth_events')} "
            "(task_id, seq, at, actor_npub, action, from_state, to_state, detail_enc) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            [task_id, seq, at, actor, action, was, now, self._seal(detail)],
        )

    # -- moving --------------------------------------------------------------

    async def act(
        self,
        npub: str,
        task_id: str,
        action: Action,
        *,
        grade: str = "",
        reasons: str = "",
        ruling: str = "",
        performer_signature: str = "",
        as_operator: bool = False,
        deemed: bool = False,
        lapsed: bool = False,
        at: int | None = None,
    ) -> dict[str, Any]:
        """Make one move on a task, as ``npub``, and return the task as it now stands.

        ``as_operator`` is for this service recording a payment a wallet proved,
        or grading at the deemed grade (``deemed``), or accepting a grade whose
        days to appeal ran out (``lapsed``); no npub earns that role by being
        named anywhere.

        Raises:
            NotYours: ``npub`` has no part in this task.
            ValueError: the move is not legal, or someone else moved first.
        """
        at = int(time.time()) if at is None else at
        action = Action(action)
        task = await self._load(task_id)
        roles = self._roles(task, npub) if task else frozenset()
        if as_operator:
            roles = roles | {Role.OPERATOR}
        if task is None or not self._may_see(task, roles):
            raise NotYours(f"No task {task_id!r} is yours to see.")
        if self._wants_miss(task, at):
            await self._miss(task, at)
            if task["every_days"] and not task["rolled"]:
                await self._recur(await self._load(task_id) or task, npub, at)
            raise ValueError("The deadline passed before this task was done, so it was cancelled as missed.")
        if not deemed and self._wants_deemed(task, at):
            await self._deem(task, at)
            task = await self._load(task_id) or task
        if not lapsed and self._wants_lapse(task, at):
            await self._lapse(task, at)
            task = await self._load(task_id) or task
            if action is Action.APPEAL:
                raise ValueError("The appeal window for this grade has closed (terms 4.6), "
                                 "so it was accepted as it stands.")
        if as_operator and action is Action.ACCEPT and not lapsed:
            raise ValueError("This service accepts a grade only when its appeal window has closed.")

        was = Standing(State(task["state"]), task["final"])
        reasons = _text(reasons, "reasons", MAX_TEXT)
        detail: dict[str, Any] = {"reasons": reasons} if reasons else {}
        facts = Facts(expired=standing(was, task["needed_by"], at) == EXPIRED)
        late_off: dict[str, Any] = {}
        if action in (Action.ASSESS, Action.DECIDE_APPEAL):
            chosen, top = _grade(task["rubric"], grade)
            sats = chosen["sats"]
            rule = task.get("late") or {}
            days = self._days_late(task, at)
            if rule.get("kind") == "discount" and days:
                sats = late_sats(chosen["sats"], int(rule["pct_per_day"]), days)
                late_off = {"full_sats": chosen["sats"], "days_late": days,
                            "pct_off": min(100, int(rule["pct_per_day"]) * days)}
            elif rule.get("kind") == "steps" and task.get("accomplished_at"):
                sats, pct = step_sats(chosen["sats"], rule["steps"],
                                      hours_late(task["needed_by"], int(task["accomplished_at"])))
                if pct:
                    late_off = {"full_sats": chosen["sats"], "days_late": days, "pct_off": pct}
            paid = self._paid_ahead(task)
            if paid:
                # The deposit and milestones were part of the reward, paid early: the award is what remains.
                late_off |= {"earned_sats": sats, "paid_ahead_sats": paid}
                sats = max(0, sats - paid)
            if deemed:
                late_off["deemed"] = True
            facts = Facts(sats=sats, top_grade=top)
            detail |= {"grade": chosen["name"], "sats": sats, **late_off}
        elif action is Action.ACCEPT:
            facts = Facts(sats=int((task.get("assessment") or {}).get("sats", 0)))

        now = step(was, action, roles, facts)  # raises ValueError on an illegal move
        if action in (Action.CLAIM_CAUSE, Action.DECIDE_CAUSE):
            self._check_cause(task, action, reasons, ruling, at)
        if task.get("cover"):
            # A job under the Standard Terms is a contract only once both parties signed one offer (terms 2.3).
            signed = task.get("agreement") or {}
            if action is Action.CLAIM and not signed.get("purchaser"):
                raise ValueError("The purchaser has not signed this offer yet, so it cannot be claimed.")
            if action is Action.MARK_DONE and not signed.get("performer"):
                raise ValueError("Sign the offer first (taskbooth_sign_offer or taskbooth_request_offer_signature).")
            cover = task["cover"]
            if action is Action.CLAIM and npub == cover.get("arbiter_npub"):
                raise ValueError("The arbiter cannot also do the work.")
            if action is Action.CLAIM and npub == cover.get("inspector_npub"):
                raise ValueError("The inspector cannot also do the work.")
            if action is Action.MARK_DONE and cover.get("arbiter_npub") and not signed.get("arbiter"):
                raise ValueError("The arbiter has not signed this offer yet.")
            if action is Action.MARK_DONE and cover.get("inspector_npub") and not signed.get("inspector"):
                raise ValueError("The inspector has not signed this offer yet.")
            held = (task.get("escrow") or {}).get("state", "")
            if action is Action.MARK_DONE and cover.get("escrow") and held not in FUNDED_STATES:
                raise ValueError("The Balance is not in escrow yet. The purchaser funds it with "
                                 "taskbooth_escrow_invoice, then taskbooth_escrow_sign.")
            if action is Action.RELEASE and held in FUNDED_STATES and held != SETTLED:
                raise ValueError("The escrow holds sats for this job. Settle it first: propose a refund "
                                 "with taskbooth_escrow_propose.")

        data = {k: task[k] for k in self._sealed_keys(task)}
        data.pop("nudges", None)  # a reminder is about the task as it stood; it moved on
        performer = task["performer"]
        match action:
            case Action.CLAIM:
                performer = npub
                data["signatures"] = {
                    **data["signatures"],
                    "performer": _text(performer_signature, "signature", MAX_SIGNATURE),
                }
                data["claimed_at"] = at
                top_sats = max(g["sats"] for g in data["rubric"]["grades"])
                asked = deposit_sats(top_sats, int(data.get("deposit_pct") or 0))
                if asked:
                    data["deposit"] = {"pct": int(data["deposit_pct"]), "sats": asked}
                # What each milestone pays is fixed now, from the price the performer agreed to.
                data["milestones"] = [{**m, "sats": top_sats * m["pct"] // 100} for m in data.get("milestones") or []]
            case Action.RELEASE:
                performer = None
                if (data.get("escrow") or {}).get("state") == INVOICED:
                    data.pop("escrow")  # an unpaid invoice; the next performer's escrow starts afresh
                if data.get("agreement"):
                    data["agreement"] = {k: v for k, v in data["agreement"].items() if k != "performer"}
                data["signatures"] = {**data["signatures"], "performer": ""}
                data.pop("claimed_at", None)
                gone = data.pop("deposit", None) or {}
                if gone.get("paid_at"):
                    # Paid straight to that performer's wallet, and a payment is final (terms 5.11):
                    # the performer keeps it, whether they gave the job back or were released.
                    detail["deposit_kept_by_performer"] = gone["sats"]
                kept = sum(int(m["sats"]) for m in data.get("milestones") or [] if m.get("paid_at"))
                if kept:
                    detail["milestones_kept_by_performer"] = kept
                data["milestones"] = [{k: m[k] for k in ("name", "due_at", "pct")} for m in data.get("milestones") or []]
            case Action.MARK_DONE:
                data["accomplished_at"] = at
            case Action.ASSESS:
                data["assessment"] = {
                    "grade": detail["grade"], "sats": detail["sats"], "assessor": npub,
                    "reasons": reasons, "at": at, **late_off,
                }
            case Action.REJECT:
                data["rejection"] = {"by": npub, "reasons": reasons, "at": at}
            case Action.APPEAL:
                data["assessment"] = {**data["assessment"], "appeal": {"reasons": reasons, "at": at}}
            case Action.DECIDE_APPEAL:
                first = data["assessment"]
                kept = {k: v for k, v in first.items()
                        if k not in ("full_sats", "days_late", "pct_off", "earned_sats", "paid_ahead_sats", "deemed")}
                data["assessment"] = {
                    **kept, "grade": detail["grade"], "sats": detail["sats"], **late_off,
                    "appeal": {
                        **first["appeal"], "first_grade": first["grade"], "first_sats": first["sats"],
                        "decided_by": npub, "decision_reasons": reasons, "decided_at": at,
                    },
                }
            case Action.CONFIRM_PAID | Action.RECORD_PAYMENT:
                data["paid_at"] = at
            case Action.CLAIM_CAUSE:
                data["cause"] = {"reasons": reasons, "at": at}
            case Action.DECIDE_CAUSE:
                data["cause"] = {**data["cause"], "ruling": ruling, "ruling_reasons": reasons, "ruled_at": at}
                detail["ruling"] = ruling

        moved = await self._rows(
            f"UPDATE {self._t('taskbooth_tasks')} SET state = $1, final = $2, performer_npub = $3, "
            "data_enc = $4, version = version + 1, updated_at = $5 "
            "WHERE task_id = $6 AND version = $7 RETURNING version",
            [now.state.value, int(now.final), performer, self._seal(data), at, task_id, task["version"]],
        )
        if not moved:
            raise ValueError("Someone else changed this task first. Read it again.")
        await self._record(
            task_id, task["version"] + 1, at, npub, action.value, was.state.value, now.state.value, detail
        )

        if now.state is State.PAID:
            await self._neon._execute(
                f"UPDATE {self._t('taskbooth_payouts')} SET paid_at = $1 WHERE task_id = $2", [at, task_id]
            )
        if action is Action.MARK_DONE and data["every_days"]:
            await self._recur(task, npub, at)
        after = await self._load(task_id)
        if after is None:  # pragma: no cover — tasks are never deleted
            raise StoreUnavailable("the task could not be read back")
        return self._view(after, self._roles(after, npub) | (roles & {Role.OPERATOR}), at)

    @staticmethod
    def _check_cause(task: dict[str, Any], action: Action, reasons: str, ruling: str, at: int) -> None:
        """A claim of cause, or the arbiter's ruling on one, is open (terms 14.8)."""
        failure = task.get("failure")
        if not failure:
            raise ValueError("This job did not fail to deliver, so there is no cause to claim.")
        if not (task.get("cover") or {}).get("arbiter_npub"):
            raise ValueError("No arbiter is named, so nobody can rule on cause and the failure stands (terms 14.8).")
        cause = task.get("cause") or {}
        if action is Action.CLAIM_CAUSE:
            if cause:
                raise ValueError("Cause has already been claimed for this failure.")
            if at > int(failure["at"]) + window_hours(task.get("cover"), "appeal_hours") * 3_600:
                raise ValueError("The appeal window to claim cause has closed (terms 14.8), "
                                 "so the failure stands without cause.")
            if not reasons:
                raise ValueError("Say what the cause was.")
            return
        if not cause:
            raise ValueError("Nobody has claimed cause for this failure.")
        if cause.get("ruling"):
            raise ValueError("The arbiter has already ruled on this claim of cause.")
        if ruling not in ("upheld", "rejected"):
            raise ValueError('The ruling is "upheld" or "rejected".')

    @staticmethod
    def _sealed_keys(task: dict[str, Any]) -> list[str]:
        plain = {"task_id", "purchaser", "performer", "state", "final", "needed_by", "series_id",
                 "occurrence", "rolled", "org_id", "project_id", "version", "created_at", "updated_at"}
        return [k for k in task if k not in plain]

    async def set_assessor(
        self, purchaser: str, task_id: str, assessor: str, on: bool, at: int | None = None
    ) -> dict[str, Any]:
        """Let another npub grade this task for the purchaser, or stop letting them.

        Only the purchaser delegates, and the purchaser always remains an assessor.
        """
        at = int(time.time()) if at is None else at
        task = await self._load(task_id)
        if task is None or purchaser != task["purchaser"]:
            raise NotYours(f"No task {task_id!r} is yours to delegate.")
        assessor = checked_npub(assessor, "assessor")
        if assessor == purchaser and not on:
            raise ValueError("The purchaser always grades their own task.")
        wanted = sorted(({*task["assessors"]} | {assessor}) if on else ({*task["assessors"]} - {assessor}))
        if len(wanted) > MAX_PEOPLE:
            raise ValueError(f"A task names at most {MAX_PEOPLE} assessors.")
        data = {k: task[k] for k in self._sealed_keys(task)} | {"assessors": wanted}
        moved = await self._rows(
            f"UPDATE {self._t('taskbooth_tasks')} SET data_enc = $1, version = version + 1, updated_at = $2 "
            "WHERE task_id = $3 AND version = $4 RETURNING version",
            [self._seal(data), at, task_id, task["version"]],
        )
        if not moved:
            raise ValueError("Someone else changed this task first. Read it again.")
        if on:
            await self._neon._execute(
                f"INSERT INTO {self._t('taskbooth_people')} (task_id, npub, role) VALUES ($1, $2, $3) "
                "ON CONFLICT (task_id, npub, role) DO NOTHING",
                [task_id, assessor, Role.ASSESSOR.value],
            )
        else:
            await self._neon._execute(
                f"DELETE FROM {self._t('taskbooth_people')} WHERE task_id = $1 AND npub = $2 AND role = $3",
                [task_id, assessor, Role.ASSESSOR.value],
            )
        await self._record(
            task_id, task["version"] + 1, at, purchaser, "delegate" if on else "undelegate",
            task["state"], task["state"], {"assessor": assessor},
        )
        return await self.get(purchaser, task_id, at)

    # -- editing and reminding -----------------------------------------------

    async def _bump(
        self, task: dict[str, Any], data: dict[str, Any], at: int, actor: str, action: Action,
        detail: dict[str, Any], *, needed_by: int | None = None,
    ) -> None:
        """Rewrite a task's sealed data (and needed-by time) without moving its state; one event."""
        moved = await self._rows(
            f"UPDATE {self._t('taskbooth_tasks')} SET data_enc = $1, needed_by = $2, version = version + 1, "
            "updated_at = $3 WHERE task_id = $4 AND version = $5 RETURNING version",
            [self._seal(data), task["needed_by"] if needed_by is None else needed_by, at,
             task["task_id"], task["version"]],
        )
        if not moved:
            raise ValueError("Someone else changed this task first. Read it again.")
        await self._record(
            task["task_id"], task["version"] + 1, at, actor, action.value, task["state"], task["state"], detail
        )

    async def file(self, purchaser: str, task_id: str, org_id: Any, project_id: Any,
                   at: int | None = None) -> dict[str, Any]:
        """Move a task into a Project, or from one Project to another. Only the purchaser may.

        Where a task is filed is no term of it: the offer names no Project, so signatures stand,
        and the Project's default terms, which only start a new task, are not compared.
        """
        at = int(time.time()) if at is None else at
        task = await self._load(task_id)
        if task is None or task["purchaser"] != purchaser:
            raise NotYours(f"No task {task_id!r} is yours to move.")
        org_id, project_id = await self._filing(purchaser, org_id, project_id)
        if not project_id:
            raise ValueError("Name the Project to move it into: its org_id and project_id.")
        if (org_id, project_id) == (task["org_id"], task["project_id"]):
            return await self.get(purchaser, task_id, at)
        moved = await self._rows(
            f"UPDATE {self._t('taskbooth_tasks')} SET org_id = $1, project_id = $2, version = version + 1, "
            "updated_at = $3 WHERE task_id = $4 AND version = $5 RETURNING version",
            [org_id, project_id, at, task_id, task["version"]],
        )
        if not moved:
            raise ValueError("Someone else changed this task first. Read it again.")
        await self._record(task_id, task["version"] + 1, at, purchaser, Action.FILE.value, task["state"],
                           task["state"], {"from_project": task["project_id"], "project_id": project_id})
        return await self.get(purchaser, task_id, at)

    async def edit(
        self, purchaser: str, task_id: str, changes: dict[str, Any], signature: str = "",
        escrow_mints: tuple[str, ...] = (), at: int | None = None,
    ) -> dict[str, Any]:
        """Change a task's terms while nobody has claimed it. Only the purchaser may.

        ``changes`` names any of :data:`EDITABLE`; each is checked as a post
        checks it. The purchaser's signature was on the old terms, so it is
        cleared unless a new one comes with the edit.
        """
        at = int(time.time()) if at is None else at
        task = await self._load(task_id)
        roles = self._roles(task, purchaser) if task else frozenset()
        if task is None or Role.PURCHASER not in roles:
            raise NotYours(f"No task {task_id!r} is yours to edit.")
        if not isinstance(changes, dict) or not changes:
            raise ValueError("Say what to change: any of " + ", ".join(EDITABLE) + ".")
        unknown = sorted(set(changes) - set(EDITABLE))
        if unknown:
            raise ValueError(f"These cannot be edited: {', '.join(unknown)}. Editable: {', '.join(EDITABLE)}.")
        step(Standing(State(task["state"]), task["final"]), Action.EDIT, roles)

        merged = {k: changes.get(k, task.get(k)) for k in EDITABLE}
        if "cover" not in changes and merged["cover"]:
            # The stored page carries the stamped terms; they are stamped afresh below.
            merged["cover"] = {k: v for k, v in merged["cover"].items() if k not in ("terms_version", "terms_hash")}
        needed_by, terms = self._terms(
            task["purchaser"], title=merged["title"], rubric=merged["rubric"], audience=merged["audience"],
            needed_by=merged["needed_by"], description=merged["description"],
            earliest_start=min(task["earliest_start"], merged["needed_by"]) if isinstance(merged["needed_by"], int)
            else task["earliest_start"],
            every_days=merged["every_days"], tie_breaker=task.get("named_tie_breaker") or task["tie_breaker"],
            assessors=[n for n in task["assessors"] if n != task["purchaser"]],
            signature=signature, late=merged["late"], deposit_pct=merged["deposit_pct"] or 0,
            cover=merged["cover"], milestones=merged["milestones"], mints=escrow_mints, at=at,
        )
        data = {k: task[k] for k in self._sealed_keys(task)} | terms
        data.pop("nudges", None)
        data.pop("agreement", None)  # the offer changed, so the signature on the old one no longer holds
        await self._bump(task, data, at, purchaser, Action.EDIT, {"changed": sorted(changes)}, needed_by=needed_by)
        if data["tie_breaker"] != task["tie_breaker"]:
            # Naming or dropping an arbiter changes who decides an appeal.
            await self._neon._execute(
                f"DELETE FROM {self._t('taskbooth_people')} WHERE task_id = $1 AND role = $2",
                [task_id, Role.TIE_BREAKER.value],
            )
            await self._neon._execute(
                f"INSERT INTO {self._t('taskbooth_people')} (task_id, npub, role) VALUES ($1, $2, $3) "
                "ON CONFLICT (task_id, npub, role) DO NOTHING",
                [task_id, data["tie_breaker"], Role.TIE_BREAKER.value],
            )
        if (data.get("cover") or {}).get("inspector_npub") != (task.get("cover") or {}).get("inspector_npub"):
            await self._neon._execute(
                f"DELETE FROM {self._t('taskbooth_people')} WHERE task_id = $1 AND role = $2",
                [task_id, Role.INSPECTOR.value],
            )
            for npub, role in self._inspector_row(data):
                await self._neon._execute(
                    f"INSERT INTO {self._t('taskbooth_people')} (task_id, npub, role) VALUES ($1, $2, $3) "
                    "ON CONFLICT (task_id, npub, role) DO NOTHING",
                    [task_id, npub, role.value],
                )
        if "audience" in changes:
            await self._neon._execute(
                f"DELETE FROM {self._t('taskbooth_people')} WHERE task_id = $1 AND role = $2",
                [task_id, Role.AUDIENCE.value],
            )
            for npub in data["audience"]:
                await self._neon._execute(
                    f"INSERT INTO {self._t('taskbooth_people')} (task_id, npub, role) VALUES ($1, $2, $3) "
                    "ON CONFLICT (task_id, npub, role) DO NOTHING",
                    [task_id, npub, Role.AUDIENCE.value],
                )
        return await self.get(purchaser, task_id, at)

    async def nudge(self, npub: str, task_id: str, kind: str, at: int | None = None) -> dict[str, Any]:
        """Remind the other side of a task: ``pay`` (the performer asks to be paid) or ``due``
        (the purchaser says the deadline is near, or past). It changes nothing else, and one of
        each kind may be sent per task every :data:`NUDGE_GAP` seconds.
        """
        at = int(time.time()) if at is None else at
        actions = {"pay": Action.NUDGE_PAY, "due": Action.NUDGE_DUE}
        if kind not in actions:
            raise ValueError("A reminder is 'pay' or 'due'.")
        task = await self._load(task_id)
        roles = self._roles(task, npub) if task else frozenset()
        if task is None or not self._may_see(task, roles):
            raise NotYours(f"No task {task_id!r} is yours to see.")
        step(Standing(State(task["state"]), task["final"]), actions[kind], roles)
        if kind == "due" and task["needed_by"] - at > NUDGE_DUE_WINDOW:
            raise ValueError("A reminder that it is due can be sent from two days before the deadline.")
        sent = dict(task.get("nudges") or {})
        last = (sent.get(kind) or {}).get("at", 0)
        if at - int(last) < NUDGE_GAP:
            raise ValueError("A reminder was sent less than 12 hours ago.")
        sent[kind] = {"by": npub, "at": at}
        data = {k: task[k] for k in self._sealed_keys(task)} | {"nudges": sent}
        await self._bump(task, data, at, npub, actions[kind], {})
        return await self.get(npub, task_id, at)

    # -- recurrence ----------------------------------------------------------

    @staticmethod
    def _wants_next(task: dict[str, Any], at: int) -> bool:
        """A recurring task nobody claimed in time, or one missed, whose next occurrence is not yet made."""
        return bool(
            task["every_days"] and not task["rolled"]
            and (task["state"] == State.MISSED or (task["state"] == State.POSTED and task["needed_by"] < at))
        )

    async def _recur(self, task: dict[str, Any], actor: str, at: int) -> None:
        """Post the next occurrence of a recurring task, once."""
        due = next_needed_by(task["needed_by"], task["every_days"], at)
        shift = due - task["needed_by"]
        data = {k: task[k] for k in (
            "title", "description", "rubric", "audience", "assessors", "tie_breaker", "every_days",
        )}
        data["late"] = task.get("late") or {"kind": "full"}
        data["deposit_pct"] = int(task.get("deposit_pct") or 0)
        data["cover"] = task.get("cover")
        data["milestones"] = [{"name": m["name"], "due_at": m["due_at"] + shift, "pct": m["pct"]}
                              for m in task.get("milestones") or []]
        data["earliest_start"] = task["earliest_start"] + shift if task["earliest_start"] else 0
        data["signatures"] = {"purchaser": task["signatures"]["purchaser"], "performer": ""}
        data["site"] = task.get("site", "")
        await self._insert(
            task_id=new_id(), purchaser=task["purchaser"], needed_by=due, series_id=task["series_id"],
            occurrence=task["occurrence"] + 1, org_id=task["org_id"], project_id=task["project_id"],
            data=data, actor=actor, action="recur", at=at,
        )
        await self._neon._execute(
            f"UPDATE {self._t('taskbooth_tasks')} SET rolled = 1 WHERE task_id = $1", [task["task_id"]]
        )

    # -- the signed offer ---------------------------------------------------

    async def offer(self, npub: str, task_id: str, at: int | None = None) -> dict[str, Any]:
        """The statement both parties sign, its hash, and who has signed it."""
        task = await self._load(task_id)
        roles = self._roles(task, npub, await self.orgs_in(npub)) if task else frozenset()
        if task is None or not self._may_see(task, roles):
            raise NotYours(f"No task {task_id!r} is yours to see.")
        signed = task.get("agreement") or {}
        return {"statement": offer_statement(task), "statement_hash": offer_hash(task),
                "signatures": {role: signed[role] for role in ("purchaser", "performer", "arbiter", "inspector")
                               if signed.get(role)}}

    async def sign(self, npub: str, task_id: str, record: dict[str, Any], *, operator_npub: str,
                   at: int | None = None) -> dict[str, Any]:
        """Keep ``npub``'s signature on the offer: the purchaser while it is open, the performer once claimed,
        and a named arbiter at either time.

        The record must verify, be ``npub``'s, and be over this offer as it stands now.
        """
        from tollbooth.tools.signature import verify_signature_record

        at = int(time.time()) if at is None else at
        task = await self._load(task_id)
        roles = self._roles(task, npub) if task else frozenset()
        if task is None or not self._may_see(task, roles):
            raise NotYours(f"No task {task_id!r} is yours to see.")
        step(Standing(State(task["state"]), task["final"]), Action.SIGN, roles)
        if Role.ARBITER in roles:
            role = "arbiter"
        elif Role.INSPECTOR in roles:
            role = "inspector"
        else:
            role = "purchaser" if task["state"] == State.POSTED else "performer"
        digest = offer_hash(task)
        why = verify_signature_record(record, statement_hash=digest, signer_npub=npub, operator_npub=operator_npub)
        if why is not None:
            raise ValueError(f"That signature does not stand: {why}. Read the offer again with taskbooth_offer_terms.")
        data = {k: task[k] for k in self._sealed_keys(task)}
        data["agreement"] = {**(task.get("agreement") or {}), "statement_hash": digest, role: record}
        await self._bump(task, data, at, npub, Action.SIGN,
                         {"role": role, "statement_hash": digest, "method": record.get("method", "")})
        return await self.get(npub, task_id, at)

    async def inspection_task(self, npub: str, task_id: str, verdict: Any, reasons: Any) -> tuple[dict[str, Any], str, str]:
        """The task ``npub`` inspects, and the verdict and reasons checked, ready to be signed.

        Raises:
            NotYours: ``npub`` is not this job's inspector.
            ValueError: the work is not delivered, it already has an opinion, or the opinion is malformed.
        """
        task = await self._load(task_id)
        roles = self._roles(task, npub) if task else frozenset()
        if task is None or Role.INSPECTOR not in roles:
            raise NotYours(f"No task {task_id!r} names you its inspector.")
        if task["state"] not in INSPECTABLE or not task.get("accomplished_at"):
            raise ValueError("The work has not been delivered, so there is nothing to inspect yet.")
        if task.get("inspection"):
            raise ValueError("This job already has the inspector's opinion, and it stands as signed.")
        if verdict not in VERDICTS:
            raise ValueError(f"The verdict is one of: {', '.join(VERDICTS)}.")
        return task, str(verdict), _text(reasons, "reasons", MAX_OPINION, required=True)

    async def inspect(self, npub: str, task_id: str, verdict: Any, reasons: Any, record: dict[str, Any], *,
                      operator_npub: str, at: int | None = None) -> dict[str, Any]:
        """Keep the inspector's signed opinion of the finished work (terms 8). It moves nothing.

        The record must verify, be ``npub``'s, and be over this opinion of this offer.
        """
        from tollbooth.tools.signature import verify_signature_record

        at = int(time.time()) if at is None else at
        task, verdict, reasons = await self.inspection_task(npub, task_id, verdict, reasons)
        digest = inspection_hash(task, verdict, reasons)
        why = verify_signature_record(record, statement_hash=digest, signer_npub=npub, operator_npub=operator_npub)
        if why is not None:
            raise ValueError(f"That signature does not stand: {why}. Sign this verdict and these reasons again.")
        data = {k: task[k] for k in self._sealed_keys(task)}
        data["inspection"] = {"verdict": verdict, "reasons": reasons, "at": at, "inspector": npub,
                              "statement_hash": digest, "method": record.get("method", ""), "record": record}
        await self._bump(task, data, at, npub, Action.INSPECT, {"verdict": verdict, "statement_hash": digest})
        return await self.get(npub, task_id, at)

    async def reputation(self, of: str, at: int | None = None) -> dict[str, Any]:
        """What ``of`` has done in each role on commissions: counts only, nothing about any one job."""
        at = int(time.time()) if at is None else at
        of = checked_npub(of, "npub")
        rows = await self._rows(
            f"SELECT {_TASK_COLUMNS} FROM {self._t('taskbooth_tasks')} WHERE purchaser_npub = $1 "
            f"OR performer_npub = $1 OR task_id IN "
            f"(SELECT task_id FROM {self._t('taskbooth_people')} WHERE npub = $1)",
            [of],
        )
        tasks = [self._task(r) for r in rows]
        failures = {t["task_id"]: self._failure_view(t, at) for t in tasks}
        return reputation_of(tasks, of, failures)

    # -- escrow ----------------------------------------------------------------

    async def escrow_task(self, npub: str, task_id: str) -> tuple[dict[str, Any], frozenset[Role]]:
        """A task ``npub`` may see, with their roles on it, for an escrow move."""
        task = await self._load(task_id)
        roles = self._roles(task, npub) if task else frozenset()
        if task is None or not self._may_see(task, roles):
            raise NotYours(f"No task {task_id!r} is yours to see.")
        return task, roles

    async def save_escrow(self, task: dict[str, Any], record: dict[str, Any], *, actor: str, action: Action,
                          detail: dict[str, Any], at: int) -> None:
        """Keep a new escrow record, unless the task changed since it was read."""
        data = {k: task[k] for k in self._sealed_keys(task)}
        data["escrow"] = record
        await self._bump(task, data, at, actor, action, detail)

    async def keep_escrow(self, task_id: str, record: dict[str, Any], *, actor: str, action: Action,
                          detail: dict[str, Any], at: int) -> None:
        """Keep an escrow record after the mint acted on it: what the mint did is so, whatever else changed."""
        for _ in range(5):
            task = await self._load(task_id)
            if task is None:  # pragma: no cover — tasks are never deleted
                raise StoreUnavailable("the task could not be read back")
            try:
                await self.save_escrow(task, record, actor=actor, action=action, detail=detail, at=at)
                return
            except ValueError:
                continue
        raise StoreUnavailable("the escrow could not be written after the mint acted; read it again")

    # -- paid ahead: the deposit and milestones ------------------------------

    @staticmethod
    def advance_of(task: dict[str, Any], which: int | None) -> dict[str, Any]:
        """The deposit (``which`` is None) or milestone ``which`` of a task, as stored."""
        if which is None:
            return task.get("deposit") or {}
        milestones = task.get("milestones") or []
        if not isinstance(which, int) or isinstance(which, bool) or not 0 <= which < len(milestones):
            raise ValueError(f"This task has {len(milestones)} milestones, numbered from 0.")
        return milestones[which]

    @staticmethod
    def _moves(which: int | None) -> tuple[Action, Action]:
        return ((Action.DEPOSIT_INVOICE, Action.CONFIRM_DEPOSIT) if which is None
                else (Action.MILESTONE_INVOICE, Action.CONFIRM_MILESTONE))

    async def _advance_move(
        self, npub: str, task_id: str, which: int | None, action: Action, at: int,
    ) -> dict[str, Any]:
        """The task, once ``npub`` may make this move on its deposit or milestone ``which``."""
        task = await self._load(task_id)
        roles = self._roles(task, npub) if task else frozenset()
        if task is None or not self._may_see(task, roles):
            raise NotYours(f"No task {task_id!r} is yours to see.")
        step(Standing(State(task["state"]), task["final"]), action, roles)
        advance = self.advance_of(task, which)
        what = "deposit" if which is None else f"milestone {which}"
        if not advance.get("sats"):
            raise ValueError("This task asks for no deposit." if which is None
                             else f"Nothing is due for {what} of this task.")
        if advance.get("paid_at"):
            raise ValueError(f"This task's {what} is already paid.")
        if which is not None and at < int(advance["due_at"]):
            raise ValueError(f"Milestone {which} ({advance['name']}) falls due at {advance['due_at']} (Unix seconds).")
        return task

    async def advance_task(self, npub: str, task_id: str, which: int | None = None,
                           at: int | None = None) -> dict[str, Any]:
        """A task whose deposit or milestone ``which`` is due and unpaid, for the purchaser or the performer."""
        at = int(time.time()) if at is None else at
        return await self._advance_move(npub, task_id, which, self._moves(which)[0], at)

    async def save_advance(self, npub: str, task: dict[str, Any], which: int | None,
                           invoice: dict[str, Any], at: int) -> None:
        """Keep a checked invoice on the deposit or milestone, with its payment hash, before anyone sees it."""
        data = {k: task[k] for k in self._sealed_keys(task)}
        if which is None:
            data["deposit"] = {**task["deposit"], **invoice}
        else:
            data["milestones"] = [{**m, **invoice} if i == which else m for i, m in enumerate(task["milestones"])]
        detail = {"sats": invoice["sats"], "payment_hash": invoice["payment_hash"]}
        if which is not None:
            detail["milestone"] = which
        await self._bump(task, data, at, npub, self._moves(which)[0], detail)

    async def confirm_advance(self, npub: str, task_id: str, which: int | None = None,
                              at: int | None = None) -> dict[str, Any]:
        """The performer says a deposit or milestone arrived. Its invoice must have been made here first."""
        at = int(time.time()) if at is None else at
        task = await self._advance_move(npub, task_id, which, self._moves(which)[1], at)
        advance = self.advance_of(task, which)
        if not advance.get("payment_hash"):
            what = "deposit" if which is None else f"milestone {which}"
            raise ValueError(f"No {what} invoice was made for this task, so there is no payment to confirm.")
        data = {k: task[k] for k in self._sealed_keys(task)}
        if which is None:
            data["deposit"] = {**advance, "paid_at": at}
        else:
            data["milestones"] = [{**m, "paid_at": at} if i == which else m for i, m in enumerate(task["milestones"])]
        detail = {"sats": advance["sats"], "payment_hash": advance["payment_hash"]}
        if which is not None:
            detail["milestone"] = which
        await self._bump(task, data, at, npub, self._moves(which)[1], detail)
        return await self.get(npub, task_id, at)

    # -- payouts -------------------------------------------------------------

    async def save_payout(self, task_id: str, payout: dict[str, Any]) -> None:
        """Keep what is known of a task's payout: invoice, payment hash, expiry, sats, how it is paid.

        For the Operator's own payout step. Call it with the payment hash
        before an invoice is shown or paid.
        """
        await self._neon._execute(
            f"INSERT INTO {self._t('taskbooth_payouts')} (task_id, paid_at, data_enc) VALUES ($1, NULL, $2) "
            "ON CONFLICT (task_id) DO UPDATE SET data_enc = EXCLUDED.data_enc",
            [task_id, self._seal(payout)],
        )

    async def payout(self, task_id: str) -> dict[str, Any] | None:
        rows = await self._rows(
            f"SELECT paid_at, data_enc FROM {self._t('taskbooth_payouts')} WHERE task_id = $1", [task_id]
        )
        if not rows:
            return None
        paid_at = rows[0]["paid_at"]
        return {"paid_at": int(paid_at) if paid_at is not None else None, **self._open(rows[0]["data_enc"])}

    # -- rubric templates ----------------------------------------------------

    async def save_template(self, owner: str, name: str, rubric: dict[str, Any], template_id: str = "") -> str:
        """Keep a rubric to reuse. Naming an existing template of the owner's replaces it."""
        sealed = self._seal({"name": _text(name, "template name", 80, required=True),
                             "rubric": checked_rubric(rubric)})
        if template_id:
            changed = await self._rows(
                f"UPDATE {self._t('taskbooth_rubric_templates')} SET data_enc = $1 "
                "WHERE template_id = $2 AND owner_npub = $3 RETURNING template_id",
                [sealed, template_id, owner],
            )
            if not changed:
                raise NotYours(f"No rubric template {template_id!r} is yours.")
            return template_id
        template_id = new_id()
        await self._neon._execute(
            f"INSERT INTO {self._t('taskbooth_rubric_templates')} (template_id, owner_npub, data_enc, created_at) "
            "VALUES ($1, $2, $3, $4)",
            [template_id, checked_npub(owner, "owner"), sealed, int(time.time())],
        )
        return template_id

    async def templates_of(self, owner: str) -> list[dict[str, Any]]:
        rows = await self._rows(
            f"SELECT template_id, data_enc, created_at FROM {self._t('taskbooth_rubric_templates')} "
            "WHERE owner_npub = $1 ORDER BY created_at, template_id",
            [owner],
        )
        return [{"template_id": r["template_id"], "created_at": int(r["created_at"]), **self._open(r["data_enc"])}
                for r in rows]

    # -- organizations and projects -----------------------------------------

    async def orgs_in(self, npub: str) -> frozenset[str]:
        """The Organizations ``npub`` owns or belongs to."""
        rows = await self._rows(
            f"SELECT org_id FROM {self._t('taskbooth_orgs')} WHERE owner_npub = $1 "
            f"UNION SELECT org_id FROM {self._t('taskbooth_org_members')} WHERE npub = $1",
            [npub],
        )
        return frozenset(r["org_id"] for r in rows)

    async def _org_row(self, org_id: str) -> dict[str, Any] | None:
        rows = await self._rows(
            f"SELECT org_id, owner_npub, created_at, data_enc FROM {self._t('taskbooth_orgs')} WHERE org_id = $1",
            [str(org_id)],
        )
        return rows[0] if rows else None

    async def _members(self, org_id: str) -> list[dict[str, Any]]:
        rows = await self._rows(
            f"SELECT npub, data_enc FROM {self._t('taskbooth_org_members')} WHERE org_id = $1 ORDER BY npub",
            [org_id],
        )
        return [{"npub": r["npub"], **self._open(r["data_enc"])} for r in rows]

    async def _place(self, npub: str, org_id: str) -> tuple[dict[str, Any], str]:
        """The Organization's row and ``npub``'s place in it, or :class:`NotYours` for someone with none."""
        row = await self._org_row(org_id)
        if row is None:
            raise NotYours(f"No Organization {org_id!r} is yours to see.")
        members = {m["npub"] for m in await self._members(row["org_id"])}
        place = orgs.standing_in(row["owner_npub"], members, npub)
        if not orgs.may_see(place):
            raise NotYours(f"No Organization {org_id!r} is yours to see.")
        return row, place

    async def _managed(self, npub: str, org_id: str) -> dict[str, Any]:
        row, place = await self._place(npub, org_id)
        if not orgs.may_manage(place):
            raise ValueError("Only the Organization's owner may change it, its members or its Projects.")
        return row

    async def save_org(self, owner: str, name: str, about: str = "", org_id: str = "",
                       at: int | None = None) -> str:
        """Make an Organization, or change one ``owner`` owns."""
        at = int(time.time()) if at is None else at
        sealed = self._seal({"name": _text(name, "Organization's name", 80, required=True),
                             "about": _text(about, "Organization's description", 1000)})
        if org_id:
            await self._managed(owner, org_id)
            await self._neon._execute(
                f"UPDATE {self._t('taskbooth_orgs')} SET data_enc = $1 WHERE org_id = $2", [sealed, org_id])
            return org_id
        owner = checked_npub(owner, "owner")
        owned = await self._rows(
            f"SELECT COUNT(*) AS n FROM {self._t('taskbooth_orgs')} WHERE owner_npub = $1", [owner])
        if int(owned[0]["n"]) >= orgs.MAX_ORGS_PER_OWNER:
            raise ValueError(f"One npub may own at most {orgs.MAX_ORGS_PER_OWNER} Organizations.")
        org_id = new_id()
        await self._neon._execute(
            f"INSERT INTO {self._t('taskbooth_orgs')} (org_id, owner_npub, created_at, data_enc) "
            "VALUES ($1, $2, $3, $4)",
            [org_id, owner, at, sealed],
        )
        return org_id

    async def orgs_of(self, npub: str) -> list[dict[str, Any]]:
        """Every Organization ``npub`` owns or belongs to, with their place in each, oldest first."""
        ids = await self.orgs_in(npub)
        out = []
        for org_id in ids:
            row = await self._org_row(org_id)
            if row is not None:
                out.append({"org_id": org_id, "owner": row["owner_npub"], "created_at": int(row["created_at"]),
                            "your_place": orgs.OWNER if row["owner_npub"] == npub else orgs.MEMBER,
                            **self._open(row["data_enc"])})
        return sorted(out, key=lambda o: (o["created_at"], o["org_id"]))

    async def org(self, npub: str, org_id: str) -> dict[str, Any]:
        """An Organization whole: its members and its Projects, for its owner and members."""
        row, place = await self._place(npub, org_id)
        return {
            "org_id": row["org_id"], "owner": row["owner_npub"], "created_at": int(row["created_at"]),
            "your_place": place, **self._open(row["data_enc"]),
            "members": await self._members(row["org_id"]),
            "projects": [self._project_summary(p) for p in await self._projects_of(row["org_id"])],
        }

    async def save_member(self, owner: str, org_id: str, npub: str, label: str = "") -> None:
        """Add a member, or change their label. The owner is never a member of their own Organization."""
        row = await self._managed(owner, org_id)
        npub = checked_npub(npub, "member")
        if npub == row["owner_npub"]:
            raise ValueError("The owner already sees everything in the Organization.")
        members = await self._members(org_id)
        if npub not in {m["npub"] for m in members} and len(members) >= orgs.MAX_MEMBERS:
            raise ValueError(f"An Organization has at most {orgs.MAX_MEMBERS} members.")
        sealed = self._seal({"label": _text(label, "member's label", 80)})
        await self._neon._execute(
            f"INSERT INTO {self._t('taskbooth_org_members')} (org_id, npub, data_enc) VALUES ($1, $2, $3) "
            "ON CONFLICT (org_id, npub) DO UPDATE SET data_enc = excluded.data_enc",
            [org_id, npub, sealed],
        )

    async def remove_member(self, owner: str, org_id: str, npub: str) -> bool:
        """Take a member out. They stop seeing the Organization and its tasks at once."""
        await self._managed(owner, org_id)
        gone = await self._rows(
            f"DELETE FROM {self._t('taskbooth_org_members')} WHERE org_id = $1 AND npub = $2 RETURNING npub",
            [org_id, str(npub)],
        )
        return bool(gone)

    async def _projects_of(self, org_id: str) -> list[dict[str, Any]]:
        rows = await self._rows(
            f"SELECT project_id, org_id, created_at, archived, data_enc FROM {self._t('taskbooth_projects')} "
            "WHERE org_id = $1 ORDER BY created_at, project_id",
            [org_id],
        )
        return [self._project(r) for r in rows]

    def _project(self, row: dict[str, Any]) -> dict[str, Any]:
        return {"project_id": row["project_id"], "org_id": row["org_id"], "created_at": int(row["created_at"]),
                "archived": bool(row["archived"]), **self._open(row["data_enc"])}

    @staticmethod
    def _project_summary(project: dict[str, Any]) -> dict[str, Any]:
        keep = ("project_id", "name", "archived", "budget_sats", "created_at")
        return {k: project[k] for k in keep if k in project}

    async def _project_row(self, project_id: str) -> dict[str, Any] | None:
        rows = await self._rows(
            f"SELECT project_id, org_id, created_at, archived, data_enc FROM {self._t('taskbooth_projects')} "
            "WHERE project_id = $1",
            [str(project_id)],
        )
        return self._project(rows[0]) if rows else None

    async def save_project(self, owner: str, org_id: str, name: str, brief: str = "", budget_sats: int = 0,
                           circle: list[str] | None = None, defaults: dict[str, Any] | None = None,
                           project_id: str = "", at: int | None = None) -> str:
        """Make a Project in an Organization, or change one. Only the owner may."""
        at = int(time.time()) if at is None else at
        await self._managed(owner, org_id)
        people = circle or []
        if not isinstance(people, list) or len(people) > orgs.MAX_CIRCLE:
            raise ValueError(f"A Project's circle names at most {orgs.MAX_CIRCLE} npubs.")
        sealed = self._seal({
            "name": _text(name, "Project's name", 120, required=True),
            "brief": _text(brief, "Project's brief", MAX_TEXT),
            "budget_sats": _whole(budget_sats, "Project's budget in sats", most=orgs.MAX_BUDGET_SATS),
            "circle": sorted({checked_npub(n, "circle member") for n in people}),
            "defaults": orgs.checked_defaults(defaults, text=_text, whole=_whole, flag=_flag, npub=checked_npub,
                                              ip_options=IP_OPTIONS,
                                              max_days=MAX_DEEMED_DAYS, max_text=MAX_TEXT),
        })
        if project_id:
            changed = await self._rows(
                f"UPDATE {self._t('taskbooth_projects')} SET data_enc = $1 WHERE project_id = $2 AND org_id = $3 "
                "RETURNING project_id",
                [sealed, project_id, org_id],
            )
            if not changed:
                raise NotYours(f"No Project {project_id!r} is in that Organization.")
            return project_id
        if len(await self._projects_of(org_id)) >= orgs.MAX_PROJECTS:
            raise ValueError(f"An Organization has at most {orgs.MAX_PROJECTS} Projects.")
        project_id = new_id()
        await self._neon._execute(
            f"INSERT INTO {self._t('taskbooth_projects')} (project_id, org_id, created_at, archived, data_enc) "
            "VALUES ($1, $2, $3, 0, $4)",
            [project_id, org_id, at, sealed],
        )
        return project_id

    async def archive_project(self, owner: str, org_id: str, project_id: str, archived: bool = True) -> None:
        """Close a Project to new tasks, or open it again. Its tasks stay as they are."""
        await self._managed(owner, org_id)
        changed = await self._rows(
            f"UPDATE {self._t('taskbooth_projects')} SET archived = $1 WHERE project_id = $2 AND org_id = $3 "
            "RETURNING project_id",
            [1 if _flag(archived, "archived flag") else 0, project_id, org_id],
        )
        if not changed:
            raise NotYours(f"No Project {project_id!r} is in that Organization.")

    async def project(self, npub: str, project_id: str, at: int | None = None) -> dict[str, Any]:
        """A Project whole, with what its tasks have committed and paid, for the Organization's people."""
        at = int(time.time()) if at is None else at
        project = await self._project_row(project_id)
        if project is None:
            raise NotYours(f"No Project {project_id!r} is yours to see.")
        try:
            _, place = await self._place(npub, project["org_id"])
        except NotYours:
            raise NotYours(f"No Project {project_id!r} is yours to see.") from None
        rows = await self._rows(
            f"SELECT {_TASK_COLUMNS} FROM {self._t('taskbooth_tasks')} WHERE project_id = $1 AND org_id = $2",
            [project["project_id"], project["org_id"]],
        )
        viewed = [self._view(self._task(r), frozenset({Role.MEMBER}), at) for r in rows]
        return {**project, "your_place": place, "totals": orgs.project_totals(viewed)}

    async def _filing(self, npub: str, org_id: Any, project_id: Any) -> tuple[str, str]:
        """Where a new task is filed: no Organization, or a Project the poster may post in."""
        org_id = _text(org_id, "Organization id", 64)
        project_id = _text(project_id, "Project id", 64)
        if not org_id and not project_id:
            return "", ""
        if not org_id or not project_id:
            raise ValueError("A task is filed under both an Organization and one of its Projects, or neither.")
        try:
            _, place = await self._place(npub, org_id)
        except NotYours:
            raise ValueError("You may post only into the Projects of an Organization you are in.") from None
        if not orgs.may_post(place):
            raise ValueError("You may post only into the Projects of an Organization you are in.")
        project = await self._project_row(project_id)
        if project is None or project["org_id"] != org_id:
            raise ValueError("That Project is not in that Organization.")
        if project["archived"]:
            raise ValueError("That Project is archived and takes no new tasks.")
        return org_id, project_id

    # -- calendar feeds ------------------------------------------------------

    async def publish_feed(self, owner: str, name: str, zone: str, at: int | None = None,
                           site: str = "") -> dict[str, Any]:
        """Mint a calendar feed for ``owner``. The token is returned once here and kept sealed.

        Raises:
            ValueError: the name or zone is unusable, or the owner already keeps the most feeds allowed.
        """
        owner = checked_npub(owner, "owner")
        name = _text(name, "calendar name", 80, required=True)
        zone = feed.checked_zone(zone)
        site = checked_site(site)
        if len(await self.feeds_of(owner)) >= feed.MAX_FEEDS:
            raise ValueError(f"You keep {feed.MAX_FEEDS} calendar feeds already; revoke one first.")
        token = feed.new_token()
        at = int(time.time()) if at is None else at
        await self._neon._execute(
            f"INSERT INTO {self._t('taskbooth_feeds')} (token_key, owner_npub, created_at, fetches, last_fetch, data_enc) "
            "VALUES ($1, $2, $3, 0, NULL, $4)",
            [feed.token_key(token), owner, at, self._seal({"token": token, "name": name, "zone": zone, "site": site})],
        )
        return {"token": token, "name": name, "zone": zone, "site": site, "created_at": at, "fetches": 0,
                "last_fetch": None}

    async def feeds_of(self, owner: str) -> list[dict[str, Any]]:
        rows = await self._rows(
            f"SELECT created_at, fetches, last_fetch, data_enc FROM {self._t('taskbooth_feeds')} "
            "WHERE owner_npub = $1 ORDER BY created_at, token_key",
            [owner],
        )
        return [{**self._open(r["data_enc"]), "created_at": int(r["created_at"]), "fetches": int(r["fetches"]),
                 "last_fetch": int(r["last_fetch"]) if r["last_fetch"] is not None else None} for r in rows]

    async def feed_for(self, token: str, at: int | None = None) -> dict[str, Any] | None:
        """Whose feed a token opens, with its name and zone, counting the read. ``None`` when it opens nothing."""
        rows = await self._rows(
            f"UPDATE {self._t('taskbooth_feeds')} SET fetches = fetches + 1, last_fetch = $1 "
            "WHERE token_key = $2 RETURNING owner_npub, data_enc",
            [int(time.time()) if at is None else at, feed.token_key(feed.checked_token(token))],
        )
        if not rows:
            return None
        return {"owner": rows[0]["owner_npub"], **self._open(rows[0]["data_enc"])}

    async def revoke_feed(self, owner: str, token: str) -> bool:
        """Stop a feed of the owner's. True when one was stopped."""
        gone = await self._rows(
            f"DELETE FROM {self._t('taskbooth_feeds')} WHERE token_key = $1 AND owner_npub = $2 RETURNING token_key",
            [feed.token_key(feed.checked_token(token)), owner],
        )
        return bool(gone)
