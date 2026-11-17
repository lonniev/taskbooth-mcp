# ruff: noqa: F811 — the fixtures are imported, then named as test arguments
"""Milestones: dated part-payments of the top grade, invoiced exactly and netted from the award."""

import pytest
from tollbooth import bolt11

from taskbooth_mcp import payout
from taskbooth_mcp.lifecycle import Action
from taskbooth_mcp.store import checked_milestones
from tests.invoices import make_invoice
from tests.test_store import DAY, NOW, _npub, family, queue  # noqa: F401 — fixtures

YEAR = 365 * DAY
HALF = NOW + YEAR // 2
#: A portrait of Murray Rothbard for a collector's estate: 1.1M at the top grade,
#: a 100k deposit (9%), and 500k (45%) at six months.
COMMISSION = {"grades": [{"name": "Accepted", "sats": 1_100_000},
                         {"name": "Abomination", "sats": 550_000, "criteria": "As the Mises Institute judges"}]}


async def _commission(db, family, **over):
    args = {"title": "Portrait of Murray Rothbard", "rubric": COMMISSION, "audience": [family["child"]],
            "needed_by": NOW + YEAR, "tie_breaker": family["other_parent"], "deposit_pct": 9,
            "milestones": [{"name": "Six months", "due_at": HALF, "pct": 45}], "at": NOW} | over
    task = await db.post(family["parent"], **args)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    return task


async def _invoice(db, family, task, which, at):
    async def resolve(address, sats):
        return make_invoice(sats, created_at=at)
    claimed = await db.advance_task(family["parent"], task, which, at=at)
    return await payout.advance_invoice(db, claimed, which, address="artist@wallet.example",
                                        requested_by=family["parent"], at=at, resolve=resolve)


async def _pay(db, family, task, which, at):
    await _invoice(db, family, task, which, at)
    return await db.confirm_advance(family["child"], task, which, at=at + 60)


# ---------------------------------------------------------------------------
# What a milestone may ask for
# ---------------------------------------------------------------------------


def test_milestones_are_checked_and_sorted():
    two = checked_milestones([{"name": "B", "due_at": NOW + 20, "pct": 10}, {"name": "A", "due_at": NOW + 10, "pct": 5}],
                             deposit_pct=0, needed_by=NOW + 30, at=NOW)
    assert [m["name"] for m in two] == ["A", "B"]
    assert checked_milestones(None, deposit_pct=0, needed_by=NOW + 30, at=NOW) == []


@pytest.mark.parametrize(("milestones", "deposit", "says"), [
    ([{"name": "x", "due_at": NOW, "pct": 10}], 0, "after now"),
    ([{"name": "x", "due_at": NOW + YEAR + 1, "pct": 10}], 0, "needed-by"),
    ([{"name": "x", "due_at": NOW + 5, "pct": 0}], 0, "percentage"),
    ([{"name": "x", "due_at": NOW + 5, "pct": 91}], 0, "percentage"),
    ([{"name": "x", "due_at": NOW + 5, "pct": 50}], 41, "at most 90%"),
    ([{"name": "", "due_at": NOW + 5, "pct": 5}], 0, "name"),
    ([{"name": "x", "due_at": True, "pct": 5}], 0, "due_at"),
    ([{"name": "x", "due_at": NOW + 5, "pct": 1}] * 7, 0, "at most 6"),
    ("halfway", 0, "list"),
])
def test_a_milestone_that_breaks_a_rule_is_refused(milestones, deposit, says):
    with pytest.raises(ValueError, match=says):
        checked_milestones(milestones, deposit_pct=deposit, needed_by=NOW + YEAR, at=NOW)


async def test_a_claim_fixes_what_each_milestone_pays(queue, family):
    _, db = queue
    task = await _commission(db, family)
    seen = await db.get(family["parent"], task, at=NOW + 20)
    assert seen["deposit"] == {"pct": 9, "sats": 99_000}
    assert seen["milestones"] == [{"name": "Six months", "due_at": HALF, "pct": 45, "sats": 495_000}]


# ---------------------------------------------------------------------------
# Paying one
# ---------------------------------------------------------------------------


async def test_a_milestone_is_not_payable_before_its_date(queue, family):
    _, db = queue
    task = await _commission(db, family)
    with pytest.raises(ValueError, match="falls due"):
        await db.advance_task(family["parent"], task, 0, at=HALF - 1)
    with pytest.raises(ValueError, match="numbered from 0"):
        await db.advance_task(family["parent"], task, 1, at=HALF)


async def test_a_milestone_invoice_is_exact_and_its_hash_is_kept(queue, family):
    _, db = queue
    task = await _commission(db, family)
    made = await _invoice(db, family, task, 0, HALF)
    assert made["sats"] == 495_000 and bolt11.decode(made["invoice"]).amount_msat == 495_000_000
    kept = (await db.get(family["child"], task, at=HALF + 1))["milestones"][0]
    assert kept["payment_hash"] == made["payment_hash"]
    events = await db.events(family["parent"], task)
    assert events[-1]["action"] == "milestone_invoice" and events[-1]["milestone"] == 0


async def test_only_the_performer_confirms_a_milestone_and_only_once(queue, family):
    _, db = queue
    task = await _commission(db, family)
    with pytest.raises(ValueError, match="No milestone 0 invoice"):
        await db.confirm_advance(family["child"], task, 0, at=HALF)
    await _invoice(db, family, task, 0, HALF)
    with pytest.raises(ValueError, match="performer"):
        await db.confirm_advance(family["parent"], task, 0, at=HALF + 5)
    paid = await db.confirm_advance(family["child"], task, 0, at=HALF + 5)
    assert paid["milestones"][0]["paid_at"] == HALF + 5 and paid["state"] == "claimed"
    with pytest.raises(ValueError, match="already paid"):
        await db.confirm_advance(family["child"], task, 0, at=HALF + 6)


async def test_a_stranger_cannot_ask_for_a_milestone(queue, family):
    _, db = queue
    task = await _commission(db, family)
    with pytest.raises(LookupError):
        await db.advance_task(_npub(), task, 0, at=HALF)


# ---------------------------------------------------------------------------
# The award pays what is left
# ---------------------------------------------------------------------------


async def test_the_award_pays_what_the_grade_earns_beyond_the_deposit_and_milestones(queue, family):
    _, db = queue
    task = await _commission(db, family)
    await _pay(db, family, task, None, NOW + 20)
    await _pay(db, family, task, 0, HALF)
    await db.act(family["child"], task, Action.MARK_DONE, at=NOW + YEAR - DAY)
    graded = await db.act(family["parent"], task, Action.ASSESS, grade="Accepted", at=NOW + YEAR)
    assert graded["state"] == "owed"
    assert graded["assessment"] | {"sats": 506_000, "earned_sats": 1_100_000,
                                   "paid_ahead_sats": 594_000} == graded["assessment"]


async def test_an_abomination_leaves_nothing_owed_and_nothing_clawed_back(queue, family):
    _, db = queue
    task = await _commission(db, family)
    await _pay(db, family, task, None, NOW + 20)
    await _pay(db, family, task, 0, HALF)
    await db.act(family["child"], task, Action.MARK_DONE, at=NOW + YEAR - DAY)
    await db.act(family["parent"], task, Action.ASSESS, grade="Abomination", at=NOW + YEAR)
    settled = await db.act(family["child"], task, Action.ACCEPT, at=NOW + YEAR + 5)
    # 550k earned, 594k already paid ahead: the artist keeps it all and is owed nothing more.
    assert (settled["state"], settled["final"], settled["assessment"]["sats"]) == ("assessed", True, 0)


async def test_an_unpaid_milestone_is_not_netted(queue, family):
    _, db = queue
    task = await _commission(db, family)
    await _invoice(db, family, task, 0, HALF)
    await db.act(family["child"], task, Action.MARK_DONE, at=NOW + YEAR - DAY)
    graded = await db.act(family["parent"], task, Action.ASSESS, grade="Accepted", at=NOW + YEAR)
    assert graded["assessment"]["sats"] == 1_100_000


async def test_a_milestone_may_be_paid_after_the_work_is_done(queue, family):
    _, db = queue
    task = await _commission(db, family)
    await db.act(family["child"], task, Action.MARK_DONE, at=HALF - DAY)
    await _pay(db, family, task, 0, HALF)
    graded = await db.act(family["parent"], task, Action.ASSESS, grade="Accepted", at=HALF + DAY)
    assert graded["assessment"]["sats"] == 605_000


# ---------------------------------------------------------------------------
# When the artist cannot finish
# ---------------------------------------------------------------------------


async def test_a_released_artist_keeps_what_was_paid_and_the_job_can_be_reposted(queue, family):
    _, db = queue
    task = await _commission(db, family)
    await _pay(db, family, task, None, NOW + 20)
    await _pay(db, family, task, 0, HALF)
    back = await db.act(family["parent"], task, Action.RELEASE, at=HALF + DAY)
    assert back["state"] == "posted" and "deposit" not in back
    assert back["milestones"] == [{"name": "Six months", "due_at": HALF, "pct": 45}]
    last = (await db.events(family["parent"], task))[-1]
    assert last["deposit_kept_by_performer"] == 99_000 and last["milestones_kept_by_performer"] == 495_000
    # The collector reposts on changed terms: a new artist, a later date, a smaller share.
    successor = _npub()
    edited = await db.edit(family["parent"], task, {
        "audience": [successor], "needed_by": NOW + 2 * YEAR,
        "milestones": [{"name": "Finish the face", "due_at": NOW + YEAR + 90 * DAY, "pct": 20}],
    }, at=HALF + 2 * DAY)
    assert edited["milestones"][0]["pct"] == 20
    again = await db.act(successor, task, Action.CLAIM, at=HALF + 3 * DAY)
    assert again["milestones"][0]["sats"] == 220_000 and not again["milestones"][0].get("paid_at")


async def test_a_repeating_task_moves_its_milestones_with_it(queue, family):
    _, db = queue
    task = await db.post(family["parent"], title="Weekly report", rubric=COMMISSION, audience=[family["child"]],
                         needed_by=NOW + 7 * DAY, every_days=7, at=NOW,
                         milestones=[{"name": "Midweek", "due_at": NOW + 3 * DAY, "pct": 10}])
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    await db.act(family["child"], task, Action.MARK_DONE, at=NOW + 20)
    nxt = next(t for t in await db.tasks_of(family["parent"], at=NOW + 30) if t["task_id"] != task)
    assert nxt["milestones"] == [{"name": "Midweek", "due_at": NOW + 10 * DAY, "pct": 10}]
