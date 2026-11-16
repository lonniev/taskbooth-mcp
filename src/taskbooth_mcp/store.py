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

from taskbooth_mcp import feed
from taskbooth_mcp.lifecycle import (
    EXPIRED,
    LATE_KINDS,
    Action,
    Facts,
    Role,
    Standing,
    State,
    days_late,
    late_sats,
    next_needed_by,
    standing,
    step,
)

MAX_TITLE = 200
MAX_TEXT = 4000
MAX_GRADES = 12
MAX_GRADE_SATS = 100_000_000
MAX_PEOPLE = 500
MAX_SIGNATURE = 4096
#: A purchaser may remind once a day and a half before the deadline, or after it.
NUDGE_DUE_WINDOW = 2 * 86_400
#: The least time between two reminders of one kind about one task.
NUDGE_GAP = 12 * 3_600
#: The task fields an edit may change, and nothing else.
EDITABLE = ("title", "description", "rubric", "audience", "needed_by", "every_days", "late")

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


def checked_late(value: Any) -> dict[str, Any]:
    """What happens when the task is done late: pay in full, a percentage off each day, or cancel."""
    if value is None or value == {}:
        return {"kind": "full"}
    if not isinstance(value, dict) or value.get("kind") not in LATE_KINDS:
        raise ValueError(f"The late rule is an object whose kind is one of: {', '.join(LATE_KINDS)}.")
    if value["kind"] == "discount":
        return {"kind": "discount", "pct_per_day": _whole(value.get("pct_per_day"), "percent off each day late",
                                                          least=1, most=100)}
    return {"kind": value["kind"]}


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
    def _roles(task: dict[str, Any], npub: str) -> frozenset[Role]:
        """Every role this npub holds on this task. One npub may hold several."""
        roles: set[Role] = set()
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
    def _wants_miss(task: dict[str, Any], at: int) -> bool:
        """A cancel-if-missed task whose deadline passed before it was done."""
        return bool(
            (task.get("late") or {}).get("kind") == "cancel"
            and task["state"] in (State.POSTED, State.CLAIMED)
            and task["needed_by"] < at
        )

    async def _miss(self, task: dict[str, Any], at: int) -> None:
        """Close a missed task, as the purchaser's own rule says. A lost race means someone else did it."""
        now = step(Standing(State(task["state"]), task["final"]), Action.MISS, {Role.OPERATOR})
        data = {k: task[k] for k in self._sealed_keys(task)}
        data.pop("nudges", None)
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
            task["state"], now.state.value, {},
        )

    async def get(self, npub: str, task_id: str, at: int | None = None) -> dict[str, Any]:
        """The task as ``npub`` may see it, or :class:`NotYours`."""
        at = int(time.time()) if at is None else at
        task = await self._load(task_id)
        roles = self._roles(task, npub) if task else frozenset()
        if task is None or not self._may_see(task, roles):
            raise NotYours(f"No task {task_id!r} is yours to see.")
        if self._wants_miss(task, at):
            await self._miss(task, at)
            task = await self._load(task_id) or task
            if task["every_days"] and not task["rolled"]:
                await self._recur(task, npub, at)
        return self._view(task, roles, at)

    async def tasks_of(self, npub: str, at: int | None = None) -> list[dict[str, Any]]:
        """Every task ``npub`` posted, performs, grades, or may claim; soonest needed first.

        A recurring task found expired is given its next occurrence here, by
        this read, since nothing runs in the background to do it.
        """
        at = int(time.time()) if at is None else at
        for _ in range(2):
            rows = await self._rows(
                f"SELECT {_TASK_COLUMNS} FROM {self._t('taskbooth_tasks')} WHERE purchaser_npub = $1 "
                f"OR performer_npub = $1 OR task_id IN "
                f"(SELECT task_id FROM {self._t('taskbooth_people')} WHERE npub = $1) "
                "ORDER BY needed_by, task_id",
                [npub],
            )
            tasks = [self._task(r) for r in rows]
            missed = [t for t in tasks if self._wants_miss(t, at)]
            stale = [t for t in tasks if self._wants_next(t, at)]
            if not missed and not stale:
                break
            for task in missed:
                await self._miss(task, at)
            for task in stale:
                await self._recur(task, npub, at)
        seen = [(t, self._roles(t, npub)) for t in tasks]
        return [self._view(t, roles, at) for t, roles in seen if self._may_see(t, roles)]

    async def events(self, npub: str, task_id: str) -> list[dict[str, Any]]:
        """The task's history, oldest first, for anyone with a part in it beyond the audience."""
        task = await self._load(task_id)
        roles = self._roles(task, npub) if task else frozenset()
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
        late: dict[str, Any] | None = None,
        at: int | None = None,
    ) -> str:
        """Post a task. The purchaser may name themselves in the audience."""
        at = int(time.time()) if at is None else at
        purchaser = checked_npub(purchaser, "purchaser")
        needed_by, data = self._terms(
            purchaser, title=title, rubric=rubric, audience=audience, needed_by=needed_by,
            description=description, earliest_start=earliest_start, every_days=every_days,
            tie_breaker=tie_breaker, assessors=assessors, signature=purchaser_signature, late=late, at=at,
        )
        task_id = new_id()
        await self._insert(
            task_id=task_id, purchaser=purchaser, needed_by=needed_by, series_id=task_id, occurrence=0,
            org_id=_text(org_id, "Organization id", 64), project_id=_text(project_id, "Project id", 64),
            data=data, actor=purchaser, action="post", at=at,
        )
        return task_id

    @staticmethod
    def _terms(
        purchaser: str, *, title: Any, rubric: Any, audience: Any, needed_by: Any, description: Any,
        earliest_start: Any, every_days: Any, tie_breaker: Any, assessors: Any, signature: Any,
        late: Any, at: int,
    ) -> tuple[int, dict[str, Any]]:
        """A task's terms checked as a post or an edit hands them in: its needed-by time and sealed data."""
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

        return needed_by, {
            "title": _text(title, "title", MAX_TITLE, required=True),
            "description": _text(description, "description", MAX_TEXT),
            "rubric": checked_rubric(rubric),
            "audience": sorted({checked_npub(n, "audience member") for n in audience}),
            # The purchaser always grades; anyone else named here grades on their behalf.
            "assessors": sorted({purchaser, *(checked_npub(n, "assessor") for n in delegates)}),
            "tie_breaker": checked_npub(tie_breaker, "tie-breaker") if tie_breaker else purchaser,
            "earliest_start": earliest_start,
            "every_days": _whole(every_days, "repeat interval in days", most=3660),
            "signatures": {"purchaser": _text(signature, "signature", MAX_SIGNATURE), "performer": ""},
            "late": checked_late(late),
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
        )
        for npub, role in people:
            await self._neon._execute(
                f"INSERT INTO {self._t('taskbooth_people')} (task_id, npub, role) VALUES ($1, $2, $3) "
                "ON CONFLICT (task_id, npub, role) DO NOTHING",
                [task_id, npub, role.value],
            )
        await self._record(task_id, 1, at, actor, action, "", State.POSTED.value, {})
        return True

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
        performer_signature: str = "",
        as_operator: bool = False,
        at: int | None = None,
    ) -> dict[str, Any]:
        """Make one move on a task, as ``npub``, and return the task as it now stands.

        ``as_operator`` is for this service recording a payment a wallet proved;
        no npub earns that role by being named anywhere.

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
            facts = Facts(sats=sats, top_grade=top)
            detail |= {"grade": chosen["name"], "sats": sats, **late_off}
        elif action is Action.ACCEPT:
            facts = Facts(sats=int((task.get("assessment") or {}).get("sats", 0)))

        now = step(was, action, roles, facts)  # raises ValueError on an illegal move

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
            case Action.RELEASE:
                performer = None
                data["signatures"] = {**data["signatures"], "performer": ""}
                data.pop("claimed_at", None)
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
                kept = {k: v for k, v in first.items() if k not in ("full_sats", "days_late", "pct_off")}
                data["assessment"] = {
                    **kept, "grade": detail["grade"], "sats": detail["sats"], **late_off,
                    "appeal": {
                        **first["appeal"], "first_grade": first["grade"], "first_sats": first["sats"],
                        "decided_by": npub, "decision_reasons": reasons, "decided_at": at,
                    },
                }
            case Action.CONFIRM_PAID | Action.RECORD_PAYMENT:
                data["paid_at"] = at

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

    async def edit(
        self, purchaser: str, task_id: str, changes: dict[str, Any], signature: str = "",
        at: int | None = None,
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
        needed_by, terms = self._terms(
            task["purchaser"], title=merged["title"], rubric=merged["rubric"], audience=merged["audience"],
            needed_by=merged["needed_by"], description=merged["description"],
            earliest_start=min(task["earliest_start"], merged["needed_by"]) if isinstance(merged["needed_by"], int)
            else task["earliest_start"],
            every_days=merged["every_days"], tie_breaker=task["tie_breaker"],
            assessors=[n for n in task["assessors"] if n != task["purchaser"]],
            signature=signature, late=merged["late"], at=at,
        )
        data = {k: task[k] for k in self._sealed_keys(task)} | terms
        data.pop("nudges", None)
        await self._bump(task, data, at, purchaser, Action.EDIT, {"changed": sorted(changes)}, needed_by=needed_by)
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
        data["earliest_start"] = task["earliest_start"] + shift if task["earliest_start"] else 0
        data["signatures"] = {"purchaser": task["signatures"]["purchaser"], "performer": ""}
        await self._insert(
            task_id=new_id(), purchaser=task["purchaser"], needed_by=due, series_id=task["series_id"],
            occurrence=task["occurrence"] + 1, org_id=task["org_id"], project_id=task["project_id"],
            data=data, actor=actor, action="recur", at=at,
        )
        await self._neon._execute(
            f"UPDATE {self._t('taskbooth_tasks')} SET rolled = 1 WHERE task_id = $1", [task["task_id"]]
        )

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

    # -- calendar feeds ------------------------------------------------------

    async def publish_feed(self, owner: str, name: str, zone: str, at: int | None = None) -> dict[str, Any]:
        """Mint a calendar feed for ``owner``. The token is returned once here and kept sealed.

        Raises:
            ValueError: the name or zone is unusable, or the owner already keeps the most feeds allowed.
        """
        owner = checked_npub(owner, "owner")
        name = _text(name, "calendar name", 80, required=True)
        zone = feed.checked_zone(zone)
        if len(await self.feeds_of(owner)) >= feed.MAX_FEEDS:
            raise ValueError(f"You keep {feed.MAX_FEEDS} calendar feeds already; revoke one first.")
        token = feed.new_token()
        at = int(time.time()) if at is None else at
        await self._neon._execute(
            f"INSERT INTO {self._t('taskbooth_feeds')} (token_key, owner_npub, created_at, fetches, last_fetch, data_enc) "
            "VALUES ($1, $2, $3, 0, NULL, $4)",
            [feed.token_key(token), owner, at, self._seal({"token": token, "name": name, "zone": zone})],
        )
        return {"token": token, "name": name, "zone": zone, "created_at": at, "fetches": 0, "last_fetch": None}

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
