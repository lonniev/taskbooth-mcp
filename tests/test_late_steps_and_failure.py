# ruff: noqa: F811 — the fixtures are imported, then named as test arguments
"""Late steps, the cover page's failure point, and a claim of cause (terms 4.10, 14.7 to 14.12)."""

import pytest

from taskbooth_mcp.lifecycle import Action, hours_late, step_sats
from taskbooth_mcp.store import checked_cover, checked_late
from tests.signing import agree
from tests.test_escrow import _funded, cover, family, keys, mint  # noqa: F401 — fixtures
from tests.test_store import DAY, NOW, RUBRIC, _post, queue  # noqa: F401 — fixtures

DUE = NOW + 2 * DAY
HOUR = 3_600
STEPS = {"kind": "steps", "steps": [{"after_hours": 0, "pct_off": 10}, {"after_hours": 72, "pct_off": 25}]}


# ---------------------------------------------------------------------------
# Late steps
# ---------------------------------------------------------------------------


def test_late_steps_rise_in_both_hours_and_percent():
    assert checked_late(STEPS) == STEPS
    for bad in ({"kind": "steps"}, {"kind": "steps", "steps": []},
                {"kind": "steps", "steps": [{"after_hours": 5, "pct_off": 10}, {"after_hours": 5, "pct_off": 20}]},
                {"kind": "steps", "steps": [{"after_hours": 1, "pct_off": 30}, {"after_hours": 9, "pct_off": 20}]},
                {"kind": "steps", "steps": [{"after_hours": -1, "pct_off": 10}]},
                {"kind": "steps", "steps": [{"after_hours": 1, "pct_off": 101}]},
                {"kind": "steps", "steps": [{"after_hours": 1, "pct_off": 10, "extra": 1}]},
                {"kind": "steps", "steps": [{"after_hours": h, "pct_off": h + 1} for h in range(7)]}):
        with pytest.raises(ValueError):
            checked_late(bad)


def test_the_last_step_passed_is_the_one_in_force():
    steps = STEPS["steps"]
    assert step_sats(1000, steps, 0) == (1000, 0)
    assert step_sats(1000, steps, hours_late(DUE, DUE + 1)) == (900, 10)
    assert step_sats(1000, steps, 72) == (900, 10)
    assert step_sats(1000, steps, 72.5) == (750, 25)


async def test_late_work_under_steps_earns_less_and_says_why(queue, family):
    _, db = queue
    task = await _post(db, family, late=STEPS)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    await db.act(family["child"], task, Action.MARK_DONE, at=DUE + 80 * HOUR)
    graded = await db.act(family["parent"], task, Action.ASSESS, grade="Tidy", at=DUE + 4 * DAY)
    assert graded["assessment"]["sats"] == 600
    assert graded["assessment"]["full_sats"] == 800 and graded["assessment"]["pct_off"] == 25


async def test_on_time_work_under_steps_earns_the_grade(queue, family):
    _, db = queue
    task = await _post(db, family, late=STEPS)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    await db.act(family["child"], task, Action.MARK_DONE, at=DUE)
    graded = await db.act(family["parent"], task, Action.ASSESS, grade="Tidy", at=DUE + DAY)
    assert graded["assessment"]["sats"] == 800 and "pct_off" not in graded["assessment"]


# ---------------------------------------------------------------------------
# The failure point
# ---------------------------------------------------------------------------


def _page(family, **over):
    return {"deemed_grade": "Tidy", "deemed_acceptance_days": 3, "arbiter_npub": family["arbiter"],
            "fail_after_hours": 24} | over


def test_the_failure_point_is_a_cover_term(family):
    assert checked_cover(_page(family), RUBRIC, purchaser=family["parent"])["fail_after_hours"] == 24
    assert checked_cover({"deliverables": []}, RUBRIC)["fail_after_hours"] == 0
    for bad in (-1, 365 * 24 + 1, True, "24"):
        with pytest.raises(ValueError):
            checked_cover(_page(family, fail_after_hours=bad), RUBRIC, purchaser=family["parent"])


async def test_the_failure_point_comes_after_the_last_step(queue, family):
    _, db = queue
    with pytest.raises(ValueError, match="after the last late step"):
        await _post(db, family, late=STEPS, cover=_page(family, fail_after_hours=72))
    assert await _post(db, family, late=STEPS, cover=_page(family, fail_after_hours=73))


def test_escrow_needs_a_failure_point(family):
    with pytest.raises(ValueError, match="failure point"):
        checked_cover(cover(family, fail_after_hours=0), RUBRIC, purchaser=family["parent"],
                      mints=(cover(family)["escrow_mint"],))


async def _claimed(db, family, keys, **page):
    task = await _post(db, family, cover=_page(family, **page))
    await agree(db, keys["parent"], task)
    await agree(db, keys["arbiter"], task)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    await agree(db, keys["child"], task)
    return task


async def test_undelivered_work_fails_at_the_point_and_not_before(queue, family, keys):
    _, db = queue
    task = await _claimed(db, family, keys)
    assert (await db.get(family["parent"], task, at=DUE + 24 * HOUR))["state"] == "claimed"
    failed = await db.get(family["parent"], task, at=DUE + 24 * HOUR + 1)
    assert failed["state"] == "missed"
    assert failed["failure"]["at"] == DUE + 24 * HOUR + 1 and failed["failure"]["without_cause"] is None
    assert (await db.events(family["parent"], task))[-1]["failure"] is True


async def test_delivered_work_never_fails(queue, family, keys):
    _, db = queue
    task = await _claimed(db, family, keys)
    await db.act(family["child"], task, Action.MARK_DONE, at=DUE + 23 * HOUR)
    assert (await db.get(family["parent"], task, at=DUE + 9 * DAY))["state"] != "missed"


async def test_an_unclaimed_job_expires_and_does_not_fail(queue, family, keys):
    _, db = queue
    task = await _post(db, family, cover=_page(family))
    seen = await db.get(family["parent"], task, at=DUE + 9 * DAY)
    assert seen["standing"] == "expired" and seen["failure"] is None


# ---------------------------------------------------------------------------
# A claim of cause
# ---------------------------------------------------------------------------


async def _failed(db, family, keys, **page):
    task = await _claimed(db, family, keys, **page)
    await db.get(family["parent"], task, at=DUE + 2 * DAY)
    return task, DUE + 2 * DAY


async def test_the_arbiter_upholds_or_rejects_a_claim_of_cause(queue, family, keys):
    _, db = queue
    task, failed_at = await _failed(db, family, keys)
    with pytest.raises(ValueError, match="Only the performer"):
        await db.act(family["parent"], task, Action.CLAIM_CAUSE, reasons="x", at=failed_at + 1)
    with pytest.raises(ValueError, match="what the cause was"):
        await db.act(family["child"], task, Action.CLAIM_CAUSE, at=failed_at + 1)
    claimed = await db.act(family["child"], task, Action.CLAIM_CAUSE, reasons="No reference photos came",
                           at=failed_at + DAY)
    assert claimed["failure"]["cause"]["reasons"] == "No reference photos came"
    assert claimed["failure"]["without_cause"] is None and claimed["state"] == "missed"
    with pytest.raises(ValueError, match="already been claimed"):
        await db.act(family["child"], task, Action.CLAIM_CAUSE, reasons="again", at=failed_at + DAY)
    with pytest.raises(ValueError, match="upheld"):
        await db.act(family["arbiter"], task, Action.DECIDE_CAUSE, ruling="maybe", at=failed_at + 2 * DAY)
    ruled = await db.act(family["arbiter"], task, Action.DECIDE_CAUSE, ruling="upheld",
                         reasons="The client never sent them", at=failed_at + 2 * DAY)
    assert ruled["failure"]["without_cause"] is False
    with pytest.raises(ValueError, match="already ruled"):
        await db.act(family["arbiter"], task, Action.DECIDE_CAUSE, ruling="rejected", at=failed_at + 3 * DAY)


async def test_a_rejected_claim_leaves_the_failure_without_cause(queue, family, keys):
    _, db = queue
    task, failed_at = await _failed(db, family, keys)
    await db.act(family["child"], task, Action.CLAIM_CAUSE, reasons="Busy", at=failed_at + DAY)
    ruled = await db.act(family["arbiter"], task, Action.DECIDE_CAUSE, ruling="rejected", at=failed_at + 2 * DAY)
    assert ruled["failure"]["without_cause"] is True


async def test_no_claim_within_seven_days_means_no_cause(queue, family, keys):
    _, db = queue
    task, failed_at = await _failed(db, family, keys)
    assert (await db.get(family["parent"], task, at=failed_at + 7 * DAY))["failure"]["without_cause"] is None
    assert (await db.get(family["parent"], task, at=failed_at + 7 * DAY + 1))["failure"]["without_cause"] is True
    with pytest.raises(ValueError, match="window to claim cause has closed"):
        await db.act(family["child"], task, Action.CLAIM_CAUSE, reasons="late", at=failed_at + 7 * DAY + 1)


async def test_without_an_arbiter_the_failure_stands(queue, family, keys):
    _, db = queue
    task = await _post(db, family, cover=_page(family, arbiter_npub=""))
    await agree(db, keys["parent"], task)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    await agree(db, keys["child"], task)
    failed = await db.get(family["parent"], task, at=DUE + 2 * DAY)
    assert failed["failure"]["without_cause"] is True and failed["failure"]["cause_ends"] is None
    with pytest.raises(ValueError, match="No arbiter"):
        await db.act(family["child"], task, Action.CLAIM_CAUSE, reasons="x", at=DUE + 2 * DAY + 1)


async def test_a_failure_without_cause_owes_the_escrow_to_the_client(queue, family, keys, mint):
    _, db = queue
    task = await _funded(db, keys, family, mint)
    failed_at = DUE + 168 * HOUR + 1
    assert (await db.get(family["parent"], task, at=failed_at))["state"] == "missed"
    waiting = await db.get(family["parent"], task, at=failed_at + DAY)
    assert "escrow_due_to" not in waiting
    due = await db.get(family["arbiter"], task, at=failed_at + 7 * DAY + 1)
    assert due["failure"]["without_cause"] is True and due["escrow_due_to"] == "client"


async def test_a_job_that_is_not_failed_has_no_cause_to_claim(queue, family, keys):
    _, db = queue
    task = await _post(db, family, late={"kind": "cancel"})  # no cover page: no Standard Terms
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    assert (await db.get(family["parent"], task, at=DUE + 1))["failure"] is None
    with pytest.raises(ValueError, match="did not fail to deliver"):
        await db.act(family["child"], task, Action.CLAIM_CAUSE, reasons="x", at=DUE + 2)
