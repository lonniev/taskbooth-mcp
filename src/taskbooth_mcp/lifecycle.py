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
  Under the Standard Terms the performer has 7 days to appeal (4.6); when they
  pass unused, this service accepts the grade for them.
- **Missed**: a task whose purchaser chose "cancel if missed" closes itself when
  its needed-by time passes before it is done. Like expired, nothing in the
  background does it; the first read after the deadline records it. A job under
  the Standard Terms that is still undelivered at its cover page's failure point
  is missed the same way, as a failure to deliver (terms 14.7). Its performer
  may then claim cause once, and its arbiter rules on the claim (terms 14.8);
  neither moves the state.
- **Deposit**: a task may ask for part of its top grade when it is claimed.
  The purchaser pays it straight to the performer while the task is claimed,
  and the award later pays only what the grade earns beyond it. It moves no
  state; it is a record on the task.
- **Milestones**: dated part-payments of the top grade, payable from their
  date while the work is under way or done. Like the deposit, they move no
  state, and the award pays only what the grade earns beyond what was paid ahead.
- **Deemed acceptance**: when the purchaser does not grade in time, this
  service grades finished work at the cover page's deemed grade.
- A final assessment worth sats is **Owed** until it is paid. One worth zero
  sats stays Assessed, final, with nothing to pay. That is a finished task, and
  it is different from Rejected.

One npub may hold several roles on the same task. The purchaser may also be the
performer; nothing here asks who is working for whom.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "DEFAULT_WINDOW_HOURS",
    "EXPIRED",
    "LATE_KINDS",
    "MAX_DEPOSIT_PCT",
    "MAX_WINDOW_HOURS",
    "WINDOWS",
    "Action",
    "Facts",
    "Role",
    "Standing",
    "State",
    "days_late",
    "deposit_sats",
    "hours_late",
    "is_finished",
    "late_sats",
    "next_needed_by",
    "standing",
    "step",
    "step_sats",
    "window_hours",
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


#: The windows a cover page sets, in hours: to appeal a grade or claim cause (terms 4.6, 14.8), for the
#: arbiter to rule (14.8), and for the client to pay (7.2). A cover signed before they were set reads as 7 days.
WINDOWS = ("appeal_hours", "ruling_hours", "pay_hours")
DEFAULT_WINDOW_HOURS = 168
MAX_WINDOW_HOURS = 90 * 24


def window_hours(cover: dict[str, Any] | None, name: str) -> int:
    """One of a cover page's windows, in hours."""
    return int((cover or {}).get(name) or DEFAULT_WINDOW_HOURS)


class Role(StrEnum):
    PURCHASER = "purchaser"      # posted the task
    AUDIENCE = "audience"        # may see it and claim it
    PERFORMER = "performer"      # claimed it
    ASSESSOR = "assessor"        # the purchaser, or someone they delegated grading to
    TIE_BREAKER = "tie_breaker"  # named in the rubric; decides an appeal
    OPERATOR = "operator"        # this service: recording a proved payment, closing a missed task
    MEMBER = "member"            # in the Organization the task is filed under: sees it, moves nothing
    ARBITER = "arbiter"          # named on the cover page: signs the offer, decides appeals, holds an escrow key
    INSPECTOR = "inspector"      # named on the cover page: signs the offer, gives one signed opinion of the work


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
    CLAIM_CAUSE = "claim_cause"
    DECIDE_CAUSE = "decide_cause"
    # The inspector's opinion changes no state; it is checked where it is kept.
    INSPECT = "inspect"
    EDIT = "edit"
    NUDGE_PAY = "nudge_pay"
    NUDGE_DUE = "nudge_due"
    DEPOSIT_INVOICE = "deposit_invoice"
    CONFIRM_DEPOSIT = "confirm_deposit"
    MILESTONE_INVOICE = "milestone_invoice"
    CONFIRM_MILESTONE = "confirm_milestone"
    SIGN = "sign"
    # Escrow moves change no state; they are checked by the escrow itself and kept as events.
    ESCROW_INVOICE = "escrow_invoice"
    ESCROW_FUND = "escrow_fund"
    ESCROW_PROPOSE = "escrow_propose"
    ESCROW_SIGN = "escrow_sign"
    ESCROW_SPLIT = "escrow_split"
    ESCROW_COLLECT = "escrow_collect"
    ESCROW_PAID_OUT = "escrow_paid_out"


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
    # The operator grades only at the deemed grade, once the purchaser let the time run out.
    (State.DONE, Action.ASSESS): frozenset({Role.ASSESSOR, Role.OPERATOR}),
    (State.DONE, Action.REJECT): frozenset({Role.ASSESSOR}),
    # The operator accepts only for a performer who let the days to appeal run out.
    (State.ASSESSED, Action.ACCEPT): frozenset({Role.PERFORMER, Role.OPERATOR}),
    (State.ASSESSED, Action.APPEAL): frozenset({Role.PERFORMER}),
    (State.APPEALED, Action.DECIDE_APPEAL): frozenset({Role.TIE_BREAKER}),
    (State.OWED, Action.CONFIRM_PAID): frozenset({Role.PERFORMER}),
    (State.OWED, Action.RECORD_PAYMENT): frozenset({Role.OPERATOR}),
    (State.POSTED, Action.MISS): frozenset({Role.OPERATOR}),
    (State.CLAIMED, Action.MISS): frozenset({Role.OPERATOR}),
    # After a failure to deliver, the performer may claim cause, and the arbiter rules on it.
    (State.MISSED, Action.CLAIM_CAUSE): frozenset({Role.PERFORMER}),
    (State.MISSED, Action.DECIDE_CAUSE): frozenset({Role.ARBITER}),
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
    # A milestone falls due on its date, whether the work is still under way or already done.
    (State.CLAIMED, Action.MILESTONE_INVOICE): frozenset({Role.PURCHASER, Role.PERFORMER}),
    (State.DONE, Action.MILESTONE_INVOICE): frozenset({Role.PURCHASER, Role.PERFORMER}),
    (State.CLAIMED, Action.CONFIRM_MILESTONE): frozenset({Role.PERFORMER}),
    (State.DONE, Action.CONFIRM_MILESTONE): frozenset({Role.PERFORMER}),
    # The purchaser signs the offer while it is open; the performer signs the same offer once claimed.
    # A named arbiter or inspector signs it too, before or after the claim.
    (State.POSTED, Action.SIGN): frozenset({Role.PURCHASER, Role.ARBITER, Role.INSPECTOR}),
    (State.CLAIMED, Action.SIGN): frozenset({Role.PERFORMER, Role.ARBITER, Role.INSPECTOR}),
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
        case (Action.EDIT | Action.NUDGE_PAY | Action.NUDGE_DUE | Action.DEPOSIT_INVOICE | Action.CONFIRM_DEPOSIT
              | Action.MILESTONE_INVOICE | Action.CONFIRM_MILESTONE | Action.SIGN | Action.CLAIM_CAUSE
              | Action.DECIDE_CAUSE):
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
#: a percentage off for each day late, take off a rising percentage in steps,
#: or cancel it once the deadline passes.
LATE_KINDS = ("full", "discount", "steps", "cancel")

_DAY = 86_400


def days_late(needed_by: int, at: int) -> int:
    """Whole days past the needed-by time, counting any part of a day as one.

    A needed-by time is the end of the chosen day in the purchaser's own zone,
    so one second past it is the next day, a day late, wherever they live.
    """
    over = at - needed_by
    return 0 if over <= 0 else -(-over // _DAY)


def hours_late(needed_by: int, at: int) -> float:
    """Hours past the needed-by time, as a fraction; 0 when on time."""
    return max(0, at - needed_by) / 3600


def step_sats(sats: int, steps: list[dict[str, int]], late_by_hours: float) -> tuple[int, int]:
    """What a grade earns under late steps, and the percentage that came off.

    A step applies once the work is more than its ``after_hours`` late; the last
    step that applies is the one in force. On time, nothing comes off.
    """
    pct = 0
    if late_by_hours > 0:
        for s in steps:
            if late_by_hours > s["after_hours"]:
                pct = s["pct_off"]
    return sats * (100 - pct) // 100, pct


def late_sats(sats: int, pct_per_day: int, days: int) -> int:
    """What a grade earns after ``pct_per_day`` comes off for each of ``days`` late; never below zero."""
    if days <= 0 or pct_per_day <= 0:
        return sats
    kept = max(0, 100 - pct_per_day * days)
    return sats * kept // 100


#: The most of a task's top grade a deposit and its milestones together may ask for,
#: so something is always left for the grade to decide.
MAX_DEPOSIT_PCT = 90


def deposit_sats(top_sats: int, pct: int) -> int:
    """The deposit a claim asks for: ``pct`` of the top grade, rounded down."""
    if pct <= 0 or top_sats <= 0:
        return 0
    return top_sats * min(pct, MAX_DEPOSIT_PCT) // 100
