# ruff: noqa: F811 — the store fixtures are imported, then named as test arguments
"""Late work, missed deadlines, editing an open task, and reminders, on SQLite running the store's real SQL."""

import pytest

from taskbooth_mcp.lifecycle import Action
from taskbooth_mcp.store import NUDGE_GAP, checked_late
from tests.test_store import DAY, NOW, _npub, _post, family, queue  # noqa: F401 — fixtures

DUE = NOW + 2 * DAY


async def _claim_and_finish(db, family, task_id, done_at):
    await db.act(family["child"], task_id, Action.CLAIM, at=NOW + 10)
    await db.act(family["child"], task_id, Action.MARK_DONE, at=done_at)


# ---------------------------------------------------------------------------
# The late rule
# ---------------------------------------------------------------------------


def test_the_late_rule_is_checked_like_any_other_input():
    assert checked_late(None) == {"kind": "full"}
    assert checked_late({"kind": "cancel", "pct_per_day": 50}) == {"kind": "cancel"}
    assert checked_late({"kind": "discount", "pct_per_day": 10}) == {"kind": "discount", "pct_per_day": 10}
    for bad in ({"kind": "discount"}, {"kind": "discount", "pct_per_day": 0},
                {"kind": "discount", "pct_per_day": 101}, {"kind": "discount", "pct_per_day": True},
                {"kind": "later"}, "cancel", ["discount"]):
        with pytest.raises(ValueError):
            checked_late(bad)


async def test_a_task_says_how_late_its_work_is(queue, family):
    _, db = queue
    task = await _post(db, family)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    assert (await db.get(family["parent"], task, at=DUE))["days_late"] == 0
    assert (await db.get(family["parent"], task, at=DUE + 1))["days_late"] == 1
    await db.act(family["child"], task, Action.MARK_DONE, at=DUE + DAY + 5)
    # Done is done: the count stops when the work was marked done.
    assert (await db.get(family["parent"], task, at=DUE + 9 * DAY))["days_late"] == 2


async def test_late_work_under_a_discount_earns_less_and_says_why(queue, family):
    _, db = queue
    task = await _post(db, family, late={"kind": "discount", "pct_per_day": 10})
    await _claim_and_finish(db, family, task, DUE + DAY + 5)  # two days late
    graded = await db.act(family["parent"], task, Action.ASSESS, grade="Tidy", at=DUE + 3 * DAY)
    assert graded["assessment"]["sats"] == 640
    assert graded["assessment"]["full_sats"] == 800
    assert graded["assessment"]["days_late"] == 2
    assert graded["assessment"]["pct_off"] == 20


async def test_on_time_work_under_a_discount_earns_the_grade(queue, family):
    _, db = queue
    task = await _post(db, family, late={"kind": "discount", "pct_per_day": 10})
    await _claim_and_finish(db, family, task, DUE - 5)
    graded = await db.act(family["parent"], task, Action.ASSESS, grade="Tidy", at=DUE + 3 * DAY)
    assert graded["assessment"]["sats"] == 800
    assert "full_sats" not in graded["assessment"]


async def test_a_top_grade_discounted_to_nothing_finishes_with_nothing_owed(queue, family):
    _, db = queue
    task = await _post(db, family, late={"kind": "discount", "pct_per_day": 50})
    await _claim_and_finish(db, family, task, DUE + 2 * DAY)
    graded = await db.act(family["parent"], task, Action.ASSESS, grade="Spotless", at=DUE + 3 * DAY)
    assert graded["assessment"]["sats"] == 0
    assert (graded["state"], graded["final"]) == ("assessed", True)


async def test_a_full_pay_task_ignores_lateness_when_graded(queue, family):
    _, db = queue
    task = await _post(db, family)
    await _claim_and_finish(db, family, task, DUE + 3 * DAY)
    graded = await db.act(family["parent"], task, Action.ASSESS, grade="Tidy", at=DUE + 4 * DAY)
    assert graded["assessment"]["sats"] == 800


# ---------------------------------------------------------------------------
# Missed
# ---------------------------------------------------------------------------


async def test_a_cancel_if_missed_task_closes_on_the_first_read_after_its_deadline(queue, family):
    _, db = queue
    task = await _post(db, family, late={"kind": "cancel"})
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    assert (await db.get(family["parent"], task, at=DUE))["state"] == "claimed"
    seen = await db.get(family["parent"], task, at=DUE + 1)
    assert (seen["state"], seen["final"]) == ("missed", True)
    events = await db.events(family["parent"], task)
    assert (events[-1]["action"], events[-1]["actor"]) == ("miss", family["parent"])


async def test_a_missed_task_cannot_then_be_marked_done(queue, family):
    _, db = queue
    task = await _post(db, family, late={"kind": "cancel"})
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    with pytest.raises(ValueError, match="missed"):
        await db.act(family["child"], task, Action.MARK_DONE, at=DUE + 60)
    assert (await db.get(family["parent"], task, at=DUE + 120))["state"] == "missed"


async def test_work_marked_done_in_time_is_never_missed(queue, family):
    _, db = queue
    task = await _post(db, family, late={"kind": "cancel"})
    await _claim_and_finish(db, family, task, DUE - 60)
    assert (await db.get(family["parent"], task, at=DUE + 5 * DAY))["state"] == "done"


async def test_a_missed_repeating_task_comes_back_once_with_the_same_rule(queue, family):
    _, db = queue
    task = await _post(db, family, every_days=7, late={"kind": "cancel"})
    tasks = await db.tasks_of(family["parent"], at=DUE + 1)
    assert [t["state"] for t in tasks] == ["missed", "posted"]
    assert tasks[1]["late"] == {"kind": "cancel"}
    assert tasks[1]["needed_by"] == DUE + 7 * DAY
    assert len(await db.tasks_of(family["parent"], at=DUE + 2)) == 2
    assert tasks[0]["task_id"] == task


async def test_without_the_cancel_rule_late_work_stays_open(queue, family):
    _, db = queue
    task = await _post(db, family, late={"kind": "discount", "pct_per_day": 5})
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    seen = await db.get(family["parent"], task, at=DUE + 3 * DAY)
    assert (seen["state"], seen["days_late"]) == ("claimed", 3)


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------


async def test_the_purchaser_edits_an_open_task_and_the_change_is_recorded(queue, family):
    _, db = queue
    task = await _post(db, family, purchaser_signature="old-sig")
    edited = await db.edit(family["parent"], task, {
        "title": "Clean your room and the hall", "needed_by": DUE + DAY,
        "late": {"kind": "discount", "pct_per_day": 25},
    }, at=NOW + 50)
    assert edited["title"] == "Clean your room and the hall"
    assert edited["needed_by"] == DUE + DAY
    assert edited["late"] == {"kind": "discount", "pct_per_day": 25}
    assert edited["description"] == "Before Grandma visits"  # untouched
    assert edited["signatures"]["purchaser"] == ""  # the old signature was on the old terms
    events = await db.events(family["parent"], task)
    assert (events[-1]["action"], events[-1]["changed"]) == ("edit", ["late", "needed_by", "title"])


async def test_an_edited_audience_is_who_may_then_claim(queue, family):
    _, db = queue
    task = await _post(db, family)
    await db.edit(family["parent"], task, {"audience": [family["sibling"]]}, at=NOW + 50)
    with pytest.raises(LookupError):
        await db.get(family["child"], task, at=NOW + 60)
    claimed = await db.act(family["sibling"], task, Action.CLAIM, at=NOW + 70)
    assert claimed["performer"] == family["sibling"]


async def test_once_claimed_the_terms_are_fixed(queue, family):
    _, db = queue
    task = await _post(db, family)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    with pytest.raises(ValueError, match="claimed"):
        await db.edit(family["parent"], task, {"title": "Something else"}, at=NOW + 20)


async def test_only_the_purchaser_edits_and_only_what_is_editable(queue, family):
    _, db = queue
    task = await _post(db, family)
    with pytest.raises(LookupError):
        await db.edit(family["child"], task, {"title": "Mine now"}, at=NOW + 20)
    for bad in ({"purchaser": family["child"]}, {"state": "paid"}, {}, {"needed_by": NOW - 1},
                {"rubric": {"grades": []}}):
        with pytest.raises(ValueError):
            await db.edit(family["parent"], task, bad, at=NOW + 20)


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------


async def test_the_performer_reminds_the_purchaser_to_pay_an_owed_task(queue, family):
    _, db = queue
    task = await _post(db, family)
    await _claim_and_finish(db, family, task, NOW + 20)
    await db.act(family["parent"], task, Action.ASSESS, grade="Spotless", at=NOW + 30)
    seen = await db.nudge(family["child"], task, "pay", at=NOW + 40)
    assert seen["nudges"]["pay"] == {"by": family["child"], "at": NOW + 40}
    assert seen["state"] == "owed"
    with pytest.raises(ValueError, match="12 hours"):
        await db.nudge(family["child"], task, "pay", at=NOW + 40 + NUDGE_GAP - 1)
    again = await db.nudge(family["child"], task, "pay", at=NOW + 40 + NUDGE_GAP)
    assert again["nudges"]["pay"]["at"] == NOW + 40 + NUDGE_GAP


async def test_paying_clears_the_reminder(queue, family):
    _, db = queue
    task = await _post(db, family)
    await _claim_and_finish(db, family, task, NOW + 20)
    await db.act(family["parent"], task, Action.ASSESS, grade="Spotless", at=NOW + 30)
    await db.nudge(family["child"], task, "pay", at=NOW + 40)
    paid = await db.act(family["child"], task, Action.CONFIRM_PAID, at=NOW + 50)
    assert paid["nudges"] == {}


async def test_the_purchaser_says_the_deadline_is_near_only_when_it_is(queue, family):
    _, db = queue
    task = await _post(db, family, needed_by=NOW + 5 * DAY)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    with pytest.raises(ValueError, match="two days"):
        await db.nudge(family["parent"], task, "due", at=NOW + 20)
    near = await db.nudge(family["parent"], task, "due", at=NOW + 3 * DAY + 1)
    assert near["nudges"]["due"]["by"] == family["parent"]


async def test_each_side_sends_only_its_own_reminder(queue, family):
    _, db = queue
    task = await _post(db, family, needed_by=NOW + DAY)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    with pytest.raises(ValueError):
        await db.nudge(family["child"], task, "due", at=NOW + 20)
    with pytest.raises(ValueError):
        await db.nudge(family["parent"], task, "pay", at=NOW + 20)
    with pytest.raises(ValueError):
        await db.nudge(family["parent"], task, "hurry", at=NOW + 20)
    with pytest.raises(LookupError):
        await db.nudge(_npub(), task, "due", at=NOW + 20)
