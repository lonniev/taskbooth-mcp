# ruff: noqa: F811 — the fixtures are imported, then named as test arguments
"""A cover page sets how long to appeal, for the arbiter to rule and for the client to pay; a
commission is filed in a Project; the operator sets where reputation ranks begin."""

import pytest

from taskbooth_mcp.agreement import offer_statement
from taskbooth_mcp.lifecycle import Action, window_hours
from taskbooth_mcp.reputation import reputation_of
from taskbooth_mcp.store import checked_cover
from tests.test_escrow import family, keys  # noqa: F401 — fixtures
from tests.test_late_steps_and_failure import HOUR, _claimed, _failed, _page
from tests.test_store import NOW, RUBRIC, _post, queue  # noqa: F401 — fixtures
from tests.test_tools import billing, call  # noqa: F401 — fixtures
from tests.test_tools import queue as tool_queue  # noqa: F401 — fixture under its own name

SHORT = {"appeal_hours": 2, "ruling_hours": 4, "pay_hours": 1}


def test_each_window_is_a_cover_term_of_whole_hours(family):
    page = checked_cover(_page(family, **SHORT), RUBRIC, purchaser=family["parent"])
    assert (page["appeal_hours"], page["ruling_hours"], page["pay_hours"]) == (2, 4, 1)
    left_out = checked_cover({"deliverables": []}, RUBRIC, purchaser=family["parent"])
    assert (left_out["appeal_hours"], left_out["ruling_hours"], left_out["pay_hours"]) == (168, 168, 168)
    for name in SHORT:
        for bad in (0, 90 * 24 + 1, True, "2"):
            with pytest.raises(ValueError, match="hours"):
                checked_cover(_page(family, **{name: bad}), RUBRIC, purchaser=family["parent"])


def test_a_cover_signed_before_the_windows_reads_as_seven_days():
    assert window_hours({"deliverables": []}, "appeal_hours") == 168
    assert window_hours(None, "pay_hours") == 168


async def test_the_offer_states_each_window(queue, family):
    _, db = queue
    task = await _post(db, family, cover=_page(family, **SHORT))
    text = offer_statement(await db._load(task))
    for line in ("Appeal window: 2 hours", "Arbiter's ruling time: 4 hours", "Payment window: 1 hours"):
        assert line in text, line


async def test_a_grade_is_appealed_only_within_the_appeal_window(queue, family, keys):
    _, db = queue
    task = await _claimed(db, family, keys, **SHORT)
    await db.act(family["child"], task, Action.MARK_DONE, at=NOW + 20)
    await db.act(family["parent"], task, Action.ASSESS, grade="Tidy", at=NOW + 30)
    with pytest.raises(ValueError, match="appeal window for this grade has closed"):
        await db.act(family["child"], task, Action.APPEAL, reasons="It was spotless", at=NOW + 30 + 2 * HOUR + 1)
    other = await _claimed(db, family, keys, **SHORT)
    await db.act(family["child"], other, Action.MARK_DONE, at=NOW + 20)
    await db.act(family["parent"], other, Action.ASSESS, grade="Tidy", at=NOW + 30)
    appealed = await db.act(family["child"], other, Action.APPEAL, reasons="It was spotless", at=NOW + 30 + 2 * HOUR)
    assert appealed["state"] == "appealed"


async def test_cause_is_claimed_only_within_the_appeal_window(queue, family, keys):
    _, db = queue
    task, failed_at = await _failed(db, family, keys, **SHORT)
    seen = await db.get(family["parent"], task, at=failed_at + 1)
    assert seen["failure"]["cause_ends"] == failed_at + 2 * HOUR
    with pytest.raises(ValueError, match="window to claim cause has closed"):
        await db.act(family["child"], task, Action.CLAIM_CAUSE, reasons="No photos", at=failed_at + 2 * HOUR + 1)


def test_an_arbiter_is_in_time_within_the_contracts_ruling_time():
    def job(decided):
        return {"task_id": "t", "purchaser": "c", "performer": "w", "state": "owed", "needed_by": 0,
                "cover": {"arbiter_npub": "a", "ruling_hours": 4}, "agreement": {},
                "assessment": {"grade": "g", "sats": 1, "appeal": {"at": 0, "decided_at": decided, "first_sats": 1}}}
    arbiter = reputation_of([job(4 * HOUR), job(4 * HOUR + 1)], "a", {})["arbiter"]
    assert arbiter["decided"] == 2 and arbiter["decided_in_time"] == 1


async def test_a_commission_is_filed_in_a_project_and_a_chore_need_not_be(queue, family):
    _, db = queue
    with pytest.raises(ValueError, match="filed in a Project"):
        await _post(db, family, cover=_page(family), org_id="", project_id="")
    assert await _post(db, family)  # a chore, with no cover page


async def test_reputation_carries_the_operators_rank_thresholds(billing, tool_queue, family):
    billing["proven"].add("")  # a free tool: the stand-in debit lets an anonymous caller through
    read = await call("reputation", of_npub=family["child"])
    assert read["ok"] and read["ranks"] == {
        "rank_min_finished": 3, "rank_proven_pct": 75, "rank_trusted_pct": 90, "rank_trusted_finished": 10,
        "rank_exemplary_pct": 95, "rank_exemplary_finished": 20,
    }
