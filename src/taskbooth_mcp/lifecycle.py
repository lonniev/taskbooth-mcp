"""What may happen to a task next, and who may make it happen.

    Posted → Claimed → Done → Assessed → Owed → Paid
                          └→ Rejected

Everything here is a pure function: no clock, no database, no network. A caller
says where the task stands, what someone wants to do, and which roles that
someone holds; :func:`step` answers with where the task stands afterwards, or
raises ``ValueError`` with a sentence a person can read.

Four things the arrows above leave out:

- **Expired** is never stored. A posted task whose needed-by time has passed
  reads as expired (:func:`standing`), and nothing runs in the background to
  make it so.
- An assessment starts **provisional**: the performer accepts it or appeals it
  once. It becomes **final** on acceptance, on the tie-breaker's decision, or at
  once when the grade is the rubric's top grade, which leaves nothing to appeal.
- **Missed**: a task whose purchaser chose "cancel if missed" closes itself when
  its needed-by time passes before it is done. Like expired, nothing in the
  background does it; the first read after the deadline records it.
- **Deposit**: a task may ask for part of its top grade when it is claimed.
  The purchaser pays it straight to the performer while the task is claimed,
  and the award later pays only what the grade earns beyond it. It moves no
  state; it is a record on the task.
- A final assessment worth sats is **Owed** until it is paid. One worth zero
  sats stays Assessed, final, with nothing to pay. That is a finished task, and
  it is different from Rejected.

One npub may hold several roles on the same task. The purchaser may also be the
performer; nothing here asks who is working for whom.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "EXPIRED",
    "LATE_KINDS",
    "MAX_DEPOSIT_PCT",
    "Action",
    "Facts",
    "Role",
    "Standing",
    "State",
    "days_late",
    "deposit_sats",
    "is_finished",
    "late_sats",
    "next_needed_by",
    "standing",
    "step",
]


class State(StrEnum):
    POSTED = "posted"
    CLAIMED = "claimed"
    DONE = "done"
    ASSESSED = "assessed"
    APPEALED = "appealed"
    OWED = "owed"
    PAID = "paid"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    MISSED = "missed"


#: How a posted task reads once its needed-by time has passed. Never stored.
EXPIRED = "expired"


class Role(StrEnum):
    PURCHASER = "purchaser"      # posted the task
    AUDIENCE = "audience"        # may see it and claim it
    PERFORMER = "performer"      # claimed it
    ASSESSOR = "assessor"        # the purchaser, or someone they delegated grading to
    TIE_BREAKER = "tie_breaker"  # named in the rubric; decides an appeal
    OPERATOR = "operator"        # this service: recording a proved payment, closing a missed task


class Action(StrEnum):
    CLAIM = "claim"
    RELEASE = "release"
    WITHDRAW = "withdraw"
    MARK_DONE = "mark_done"
    ASSESS = "assess"
    REJECT = "reject"
    ACCEPT = "accept"
    APPEAL = "appeal"
    DECIDE_APPEAL = "decide_appeal"
    CONFIRM_PAID = "confirm_paid"
    RECORD_PAYMENT = "record_payment"
    MISS = "miss"
    EDIT = "edit"
    NUDGE_PAY = "nudge_pay"
    NUDGE_DUE = "nudge_due"
    DEPOSIT_INVOICE = "deposit_invoice"
    CONFIRM_DEPOSIT = "confirm_deposit"


@dataclass(frozen=True, slots=True)
class Standing:
    """Where a task stands: its state, and whether its assessment is final."""

    state: State
    final: bool = False


@dataclass(frozen=True, slots=True)
class Facts:
    """What a step needs to know beyond the state.

    ``expired``: the needed-by time has passed (matters only to a claim).
    ``sats``: what the grade in question earns (assess, accept, decide_appeal).
    ``top_grade``: the grade being given is the rubric's highest (assess).
    """

    expired: bool = False
    sats: int = 0
    top_grade: bool = False


def _settled(sats: int) -> Standing:
    """Where a final assessment leaves the task."""
    return Standing(State.OWED if sats > 0 else State.ASSESSED, final=True)


#: (state, action) → the roles that may do it. An assessment that is already
#: final accepts no further action; :func:`step` checks that separately.
_ALLOWED: dict[tuple[State, Action], frozenset[Role]] = {
    (State.POSTED, Action.CLAIM): frozenset({Role.AUDIENCE}),
    (State.POSTED, Action.WITHDRAW): frozenset({Role.PURCHASER}),
    (State.CLAIMED, Action.RELEASE): frozenset({Role.PERFORMER, Role.PURCHASER}),
    (State.CLAIMED, Action.MARK_DONE): frozenset({Role.PERFORMER}),
    (State.DONE, Action.ASSESS): frozenset({Role.ASSESSOR}),
    (State.DONE, Action.REJECT): frozenset({Role.ASSESSOR}),
    (State.ASSESSED, Action.ACCEPT): frozenset({Role.PERFORMER}),
    (State.ASSESSED, Action.APPEAL): frozenset({Role.PERFORMER}),
    (State.APPEALED, Action.DECIDE_APPEAL): frozenset({Role.TIE_BREAKER}),
    (State.OWED, Action.CONFIRM_PAID): frozenset({Role.PERFORMER}),
    (State.OWED, Action.RECORD_PAYMENT): frozenset({Role.OPERATOR}),
    (State.POSTED, Action.MISS): frozenset({Role.OPERATOR}),
    (State.CLAIMED, Action.MISS): frozenset({Role.OPERATOR}),
    # The terms may change only while nobody has agreed to them.
    (State.POSTED, Action.EDIT): frozenset({Role.PURCHASER}),
    # Reminders change nothing: the performer asks to be paid, the purchaser
    # says a deadline is near.
    (State.OWED, Action.NUDGE_PAY): frozenset({Role.PERFORMER}),
    (State.CLAIMED, Action.NUDGE_DUE): frozenset({Role.PURCHASER}),
    # A deposit is asked for while the work is under way, and its arrival may
    # be confirmed until the work is graded, after which the award accounts for it.
    (State.CLAIMED, Action.DEPOSIT_INVOICE): frozenset({Role.PURCHASER, Role.PERFORMER}),
    (State.CLAIMED, Action.CONFIRM_DEPOSIT): frozenset({Role.PERFORMER}),
    (State.DONE, Action.CONFIRM_DEPOSIT): frozenset({Role.PERFORMER}),
}


def step(
    now: Standing,
    action: Action,
    roles: frozenset[Role] | set[Role],
    facts: Facts | None = None,
) -> Standing:
    """Where the task stands after ``action``, taken by someone holding ``roles``.

    Raises:
        ValueError: the action is not open from here, or not to these roles.
    """
    facts = facts if facts is not None else Facts()
    allowed = _ALLOWED.get((now.state, action))
    if allowed is None or (now.state is State.ASSESSED and now.final):
        raise ValueError(f"A task that is {_reads_as(now)} cannot be given '{action.value}'.")
    if not allowed & frozenset(roles):
        who = " or ".join(sorted(r.value.replace("_", "-") for r in allowed))
        raise ValueError(f"Only the {who} may '{action.value}' this task.")
    if facts.sats < 0:
        raise ValueError("A grade cannot earn fewer than zero sats.")

    match action:
        case Action.CLAIM:
            if facts.expired:
                raise ValueError("This task's needed-by time has passed, so it can no longer be claimed.")
            return Standing(State.CLAIMED)
        case Action.RELEASE:
            return Standing(State.POSTED)
        case Action.WITHDRAW:
            return Standing(State.WITHDRAWN)
        case Action.MARK_DONE:
            return Standing(State.DONE)
        case Action.ASSESS:
            # The top grade is the most the purchaser can owe: nothing to appeal.
            return _settled(facts.sats) if facts.top_grade else Standing(State.ASSESSED)
        case Action.REJECT:
            return Standing(State.REJECTED)
        case Action.ACCEPT | Action.DECIDE_APPEAL:
            return _settled(facts.sats)
        case Action.APPEAL:
            return Standing(State.APPEALED)
        case Action.CONFIRM_PAID | Action.RECORD_PAYMENT:
            return Standing(State.PAID, final=True)
        case Action.MISS:
            return Standing(State.MISSED, final=True)
        case Action.EDIT | Action.NUDGE_PAY | Action.NUDGE_DUE | Action.DEPOSIT_INVOICE | Action.CONFIRM_DEPOSIT:
            return now
    raise ValueError(f"Unknown action '{action}'.")  # pragma: no cover — the enum is closed


def _reads_as(now: Standing) -> str:
    if now.state is State.ASSESSED and now.final:
        return "assessed and final"
    return now.state.value


def is_finished(now: Standing) -> bool:
    """True when nothing more can happen to the task."""
    if now.state in (State.PAID, State.REJECTED, State.WITHDRAWN, State.MISSED):
        return True
    return now.state is State.ASSESSED and now.final


def standing(now: Standing, needed_by: int, at: int) -> str:
    """How the task reads at time ``at``: its state, or ``expired``."""
    if now.state is State.POSTED and needed_by < at:
        return EXPIRED
    return now.state.value


def next_needed_by(needed_by: int, every_days: int, at: int) -> int:
    """The needed-by time of a recurring task's next occurrence.

    Steps forward by whole intervals and skips any that have already passed, so
    a chore nobody did for three weeks comes back once, due next.
    """
    if every_days < 1:
        raise ValueError("A recurring task repeats every 1 day or more.")
    interval = every_days * 86_400
    missed = max(0, (at - needed_by) // interval)
    return needed_by + (missed + 1) * interval


#: What a purchaser may say happens when a task is done late: pay in full, take
#: a percentage off for each day late, or cancel it once the deadline passes.
LATE_KINDS = ("full", "discount", "cancel")

_DAY = 86_400


def days_late(needed_by: int, at: int) -> int:
    """Whole days past the needed-by time, counting any part of a day as one.

    A needed-by time is the end of the chosen day in the purchaser's own zone,
    so one second past it is the next day, a day late, wherever they live.
    """
    over = at - needed_by
    return 0 if over <= 0 else -(-over // _DAY)


def late_sats(sats: int, pct_per_day: int, days: int) -> int:
    """What a grade earns after ``pct_per_day`` comes off for each of ``days`` late; never below zero."""
    if days <= 0 or pct_per_day <= 0:
        return sats
    kept = max(0, 100 - pct_per_day * days)
    return sats * kept // 100


#: The most of a task's top grade a deposit may ask for.
MAX_DEPOSIT_PCT = 90


def deposit_sats(top_sats: int, pct: int) -> int:
    """The deposit a claim asks for: ``pct`` of the top grade, rounded down."""
    if pct <= 0 or top_sats <= 0:
        return 0
    return top_sats * min(pct, MAX_DEPOSIT_PCT) // 100
