"""Every move a task can make, and every move it cannot.

The legal moves are written out here by hand, from the design note, and are
never read back from ``lifecycle._ALLOWED``. Every other combination of state,
finality, action and role is then tried and must be refused.
"""

from itertools import product

import pytest

from taskqueue_mcp.lifecycle import (
    EXPIRED,
    Action,
    Facts,
    Role,
    Standing,
    State,
    is_finished,
    next_needed_by,
    standing,
    step,
)

DAY = 86_400

# (state, action, role) → where it leads when the grade earns sats and is not the top grade.
LEGAL: dict[tuple[State, Action, Role], Standing] = {
    (State.POSTED, Action.CLAIM, Role.AUDIENCE): Standing(State.CLAIMED),
    (State.POSTED, Action.WITHDRAW, Role.PURCHASER): Standing(State.WITHDRAWN),
    (State.CLAIMED, Action.RELEASE, Role.PERFORMER): Standing(State.POSTED),
    (State.CLAIMED, Action.RELEASE, Role.PURCHASER): Standing(State.POSTED),
    (State.CLAIMED, Action.MARK_DONE, Role.PERFORMER): Standing(State.DONE),
    (State.DONE, Action.ASSESS, Role.ASSESSOR): Standing(State.ASSESSED),
    (State.DONE, Action.REJECT, Role.ASSESSOR): Standing(State.REJECTED),
    (State.ASSESSED, Action.ACCEPT, Role.PERFORMER): Standing(State.OWED, final=True),
    (State.ASSESSED, Action.APPEAL, Role.PERFORMER): Standing(State.APPEALED),
    (State.APPEALED, Action.DECIDE_APPEAL, Role.TIE_BREAKER): Standing(State.OWED, final=True),
    (State.OWED, Action.CONFIRM_PAID, Role.PERFORMER): Standing(State.PAID, final=True),
    (State.OWED, Action.RECORD_PAYMENT, Role.OPERATOR): Standing(State.PAID, final=True),
}
SOME_SATS = Facts(sats=800)


@pytest.mark.parametrize(("move", "leads_to"), LEGAL.items(), ids=lambda v: str(v))
def test_each_legal_move_leads_where_the_design_says(move, leads_to):
    state, action, role = move
    assert step(Standing(state), action, {role}, SOME_SATS) == leads_to


def test_every_other_move_is_refused():
    tried = 0
    for state, action, role in product(State, Action, Role):
        if (state, action, role) in LEGAL:
            continue
        with pytest.raises(ValueError):
            step(Standing(state), action, {role}, SOME_SATS)
        tried += 1
    assert tried == len(State) * len(Action) * len(Role) - len(LEGAL)
    assert tried > 500


def test_a_final_assessment_accepts_nothing_more():
    settled = Standing(State.ASSESSED, final=True)
    for action, role in product(Action, Role):
        with pytest.raises(ValueError, match="assessed and final"):
            step(settled, action, {role}, SOME_SATS)


def test_holding_no_role_opens_nothing():
    for state, action in product(State, Action):
        with pytest.raises(ValueError):
            step(Standing(state), action, set(), SOME_SATS)


def test_the_refusal_names_who_may_act():
    with pytest.raises(ValueError, match="Only the tie-breaker may 'decide_appeal'"):
        step(Standing(State.APPEALED), Action.DECIDE_APPEAL, {Role.PURCHASER, Role.PERFORMER})
    with pytest.raises(ValueError, match="Only the performer or purchaser may 'release'"):
        step(Standing(State.CLAIMED), Action.RELEASE, {Role.AUDIENCE})


def test_one_npub_holding_several_roles_may_use_any_of_them():
    """The purchaser may also be the performer: every role they hold counts."""
    everyone = {Role.PURCHASER, Role.AUDIENCE, Role.PERFORMER, Role.ASSESSOR, Role.TIE_BREAKER}
    at = Standing(State.POSTED)
    for action in (Action.CLAIM, Action.MARK_DONE, Action.ASSESS, Action.APPEAL, Action.DECIDE_APPEAL,
                   Action.CONFIRM_PAID):
        at = step(at, action, everyone, SOME_SATS)
    assert at == Standing(State.PAID, final=True)


class TestAssessment:
    def test_the_top_grade_is_final_at_once_and_owed(self):
        got = step(Standing(State.DONE), Action.ASSESS, {Role.ASSESSOR}, Facts(sats=2000, top_grade=True))
        assert got == Standing(State.OWED, final=True)

    def test_a_lower_grade_waits_for_the_performer(self):
        got = step(Standing(State.DONE), Action.ASSESS, {Role.ASSESSOR}, Facts(sats=800))
        assert got == Standing(State.ASSESSED, final=False)

    @pytest.mark.parametrize(
        ("at", "action", "role"),
        [
            (State.ASSESSED, Action.ACCEPT, Role.PERFORMER),
            (State.APPEALED, Action.DECIDE_APPEAL, Role.TIE_BREAKER),
        ],
    )
    def test_a_final_grade_worth_nothing_ends_assessed_with_nothing_owed(self, at, action, role):
        got = step(Standing(at), action, {role}, Facts(sats=0))
        assert got == Standing(State.ASSESSED, final=True)
        assert is_finished(got)

    def test_a_zero_sat_top_grade_is_final_and_finished(self):
        got = step(Standing(State.DONE), Action.ASSESS, {Role.ASSESSOR}, Facts(sats=0, top_grade=True))
        assert got == Standing(State.ASSESSED, final=True)

    def test_an_appeal_can_be_made_only_once(self):
        decided = step(Standing(State.APPEALED), Action.DECIDE_APPEAL, {Role.TIE_BREAKER}, SOME_SATS)
        with pytest.raises(ValueError):
            step(decided, Action.APPEAL, {Role.PERFORMER})

    def test_a_grade_cannot_earn_negative_sats(self):
        with pytest.raises(ValueError, match="fewer than zero"):
            step(Standing(State.DONE), Action.ASSESS, {Role.ASSESSOR}, Facts(sats=-1))


class TestExpiry:
    def test_an_expired_task_cannot_be_claimed(self):
        with pytest.raises(ValueError, match="needed-by time has passed"):
            step(Standing(State.POSTED), Action.CLAIM, {Role.AUDIENCE}, Facts(expired=True))

    def test_an_expired_task_can_still_be_withdrawn(self):
        got = step(Standing(State.POSTED), Action.WITHDRAW, {Role.PURCHASER}, Facts(expired=True))
        assert got == Standing(State.WITHDRAWN)

    def test_a_posted_task_past_its_time_reads_as_expired(self):
        assert standing(Standing(State.POSTED), needed_by=100, at=101) == EXPIRED
        assert standing(Standing(State.POSTED), needed_by=100, at=100) == "posted"

    def test_a_claimed_task_past_its_time_is_still_claimed(self):
        assert standing(Standing(State.CLAIMED), needed_by=100, at=10_000) == "claimed"


class TestFinished:
    @pytest.mark.parametrize("state", [State.PAID, State.REJECTED, State.WITHDRAWN])
    def test_paid_rejected_and_withdrawn_are_the_end(self, state):
        assert is_finished(Standing(state))
        for action, role in product(Action, Role):
            with pytest.raises(ValueError):
                step(Standing(state), action, {role})

    @pytest.mark.parametrize("state", [State.POSTED, State.CLAIMED, State.DONE, State.ASSESSED,
                                       State.APPEALED, State.OWED])
    def test_everything_else_is_still_open(self, state):
        assert not is_finished(Standing(state))


class TestRecurrence:
    def test_a_weekly_chore_is_next_due_a_week_later(self):
        assert next_needed_by(1000, 7, at=900) == 1000 + 7 * DAY

    def test_occurrences_nobody_did_are_skipped(self):
        due = 1_000_000
        assert next_needed_by(due, 7, at=due + 22 * DAY) == due + 28 * DAY

    def test_finishing_exactly_on_the_due_time_gives_the_next_week(self):
        assert next_needed_by(1000, 7, at=1000) == 1000 + 7 * DAY

    def test_an_interval_shorter_than_a_day_is_refused(self):
        with pytest.raises(ValueError):
            next_needed_by(1000, 0, at=0)
