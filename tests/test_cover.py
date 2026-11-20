# ruff: noqa: F811 — the fixtures are imported, then named as test arguments
"""A job's cover page, the Standard Terms it names, and grading at the deemed grade when nobody grades in time."""

import hashlib

import pytest
from pynostr.key import PrivateKey

from taskbooth_mcp import terms
from taskbooth_mcp.lifecycle import Action
from taskbooth_mcp.store import checked_cover
from tests.signing import agree
from tests.test_store import DAY, NOW, RUBRIC, _post, queue  # noqa: F401 — fixtures
from tests.test_tools import billing, call  # noqa: F401 — fixtures

DUE = NOW + 2 * DAY


@pytest.fixture
def keys():
    return {role: PrivateKey() for role in ("parent", "child", "sibling", "other_parent")}


@pytest.fixture
def family(keys):
    return {role: key.public_key.bech32() for role, key in keys.items()}
SPEC_HASH = hashlib.sha256(b"Translate the Welcome page into French, tu throughout.").hexdigest()
COVER = {"spec_url": "https://example.org/spec.md", "spec_hash": SPEC_HASH, "deliverables": ["fr.ts"],
         "deemed_grade": "Tidy", "deemed_acceptance_days": 3, "ip_option": "assign"}


# ---------------------------------------------------------------------------
# The Standard Terms
# ---------------------------------------------------------------------------


def test_each_version_of_the_terms_is_pinned_by_its_hash():
    # Never edit a version a real contract signed; add the next one. v1 is a draft until then.
    # This pin fails on any edit, so each edit is deliberate.
    assert terms.terms_hash("v0") == hashlib.sha256(terms.text("v0").encode()).hexdigest()
    assert terms.terms_hash("v0") == "39f1a595bfe991b897f76b51d877a5cb8e864c942ee6f181ec48c835881534dc"
    assert terms.terms_hash("v1") == "207905d2fee013a9a3545b0e514e6c45f9d7fa9909164314baeb7ebef3afa239"
    assert terms.text() == terms.text(terms.CURRENT)
    with pytest.raises(ValueError, match="no version"):
        terms.text("v9")


def test_v1_offers_cashu_escrow_with_a_named_arbiter():
    v1 = terms.text("v1")
    assert "MakerMarket" not in v1
    assert "Any two of the three may release the escrow" in v1
    assert "From the refund date on the Cover Page, the Client alone may take back the whole escrow." in v1
    assert "holds no key that can move them" in v1


def test_the_terms_put_the_refund_date_before_the_deemed_acceptance_date():
    assert "refund time-lock date" in terms.text("v0")
    assert "always fall before the deemed-acceptance time-lock date" in terms.text("v0")


async def test_anyone_may_read_the_terms(billing):
    billing["proven"].add("")  # a free tool asks nobody to prove anything
    read = await call("standard_terms")
    assert read["version"] == terms.CURRENT and read["hash"] == terms.terms_hash()
    assert read["text"].startswith("Task Booth Standard Terms") and read["versions"] == ["v0", "v1"]
    refused = await call("standard_terms", version="v9")
    assert not refused["success"] and "no version" in refused["error"]


# ---------------------------------------------------------------------------
# The cover page
# ---------------------------------------------------------------------------


def test_a_cover_page_is_stamped_with_the_terms_in_force():
    page = checked_cover(COVER, RUBRIC)
    assert page["terms_version"] == terms.CURRENT and page["terms_hash"] == terms.terms_hash()
    assert page["deemed_grade"] == "Tidy" and page["ip_option"] == "assign" and not page["confidential"]
    # The caller cannot name the terms; the server stamps them.
    with pytest.raises(ValueError, match="no field"):
        checked_cover(COVER | {"terms_hash": "0" * 64}, RUBRIC)
    assert checked_cover(None, RUBRIC) is None and checked_cover({}, RUBRIC) is None


@pytest.mark.parametrize(("change", "says"), [
    ({"spec_url": "http://example.org/spec"}, "https"),
    ({"spec_url": "javascript:alert(1)"}, "https"),
    ({"spec_hash": "ABC"}, "64 lowercase hex"),
    ({"spec_hash": "G" * 64}, "64 lowercase hex"),
    ({"deliverables": "fr.ts"}, "list"),
    ({"deliverables": ["x"] * 21}, "list"),
    ({"deemed_grade": "Perfect"}, "no grade named"),
    ({"deemed_acceptance_days": 0}, "deemed-acceptance"),
    ({"deemed_acceptance_days": 61}, "deemed-acceptance"),
    ({"deemed_grade": "", "deemed_acceptance_days": 5}, "needs a deemed grade"),
    ({"ip_option": "mine"}, "IP option"),
    ({"ip_option": "custom"}, "custom IP terms"),
    ({"confidential": "yes"}, "true or false"),
    ({"escrow_enabled": True}, "no field"),
    ({"arbiter_npub": "npub1x"}, "arbiter is not a valid npub"),
    ({"escrow": "yes"}, "true or false"),
    ({"escrow": True}, "needs a named arbiter"),
    ({"escrow_mint": "https://testnut.cashu.space"}, "belong only to a job held in escrow"),
    ({"refund_days": 7}, "belong only to a job held in escrow"),
    ({"price_usd": 100}, "no field"),
])
def test_every_cover_page_field_is_checked(change, says):
    with pytest.raises(ValueError, match=says):
        checked_cover(COVER | change, RUBRIC)


async def test_a_task_keeps_its_cover_page_and_an_edit_restamps_it(queue, family):
    _, db = queue
    task = await _post(db, family, cover=COVER)
    seen = await db.get(family["parent"], task, at=NOW)
    assert seen["cover"]["spec_hash"] == SPEC_HASH and seen["cover"]["terms_hash"] == terms.terms_hash()
    edited = await db.edit(family["parent"], task, {"title": "Translate Welcome"}, at=NOW + 5)
    assert edited["cover"] == seen["cover"]
    edited = await db.edit(family["parent"], task, {"cover": COVER | {"confidential": True}}, at=NOW + 6)
    assert edited["cover"]["confidential"] is True
    # A rubric that drops the deemed grade is refused while the cover page still names it.
    with pytest.raises(ValueError, match="no grade named"):
        await db.edit(family["parent"], task, {"rubric": {"grades": [{"name": "Done", "sats": 10}]}}, at=NOW + 7)


async def test_a_task_without_a_cover_page_reads_as_before(queue, family):
    _, db = queue
    seen = await db.get(family["parent"], await _post(db, family), at=NOW)
    assert seen["cover"] is None and seen["milestones"] == []


async def test_a_repeating_task_carries_its_cover_page_forward(queue, family, keys):
    _, db = queue
    task = await _post(db, family, cover=COVER, every_days=7)
    await agree(db, keys["parent"], task)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    await agree(db, keys["child"], task)
    await db.act(family["child"], task, Action.MARK_DONE, at=NOW + 20)
    tasks = await db.tasks_of(family["parent"], at=NOW + 30)
    nxt = next(t for t in tasks if t["task_id"] != task)
    assert nxt["cover"] == (await db.get(family["parent"], task, at=NOW + 30))["cover"]


# ---------------------------------------------------------------------------
# Deemed acceptance
# ---------------------------------------------------------------------------


async def _finished(db, family, keys, cover=COVER, **over):
    task = await _post(db, family, cover=cover, **over)
    await agree(db, keys["parent"], task)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    await agree(db, keys["child"], task)
    await db.act(family["child"], task, Action.MARK_DONE, at=NOW + DAY)
    return task


async def test_ungraded_work_is_graded_at_the_deemed_grade_once_the_period_runs_out(queue, family, keys):
    _, db = queue
    task = await _finished(db, family, keys)
    waiting = await db.get(family["child"], task, at=NOW + DAY + 3 * DAY)
    assert waiting["state"] == "done"
    deemed = await db.get(family["child"], task, at=NOW + DAY + 3 * DAY + 1)
    assert deemed["state"] == "assessed" and deemed["assessment"]["grade"] == "Tidy"
    assert deemed["assessment"]["deemed"] is True and deemed["assessment"]["sats"] == 800
    events = await db.events(family["parent"], task)
    assert events[-1]["action"] == "assess" and events[-1]["deemed"] is True
    # The performer may still appeal it once.
    appealed = await db.act(family["child"], task, Action.APPEAL, reasons="It was spotless", at=NOW + 6 * DAY)
    assert appealed["state"] == "appealed"


async def test_a_purchaser_who_grades_in_time_is_not_overruled(queue, family, keys):
    _, db = queue
    task = await _finished(db, family, keys)
    await db.act(family["parent"], task, Action.ASSESS, grade="Spotless", at=NOW + 2 * DAY)
    later = await db.get(family["parent"], task, at=NOW + 30 * DAY)
    assert later["assessment"]["grade"] == "Spotless" and "deemed" not in later["assessment"]


async def test_a_late_grade_after_the_period_is_refused_because_it_was_already_deemed(queue, family, keys):
    _, db = queue
    task = await _finished(db, family, keys)
    with pytest.raises(ValueError, match="cannot be given 'assess'"):
        await db.act(family["parent"], task, Action.ASSESS, grade="Not done", at=NOW + 10 * DAY)
    assert (await db.get(family["parent"], task, at=NOW + 10 * DAY))["assessment"]["deemed"] is True


async def test_a_list_read_deems_too(queue, family, keys):
    _, db = queue
    task = await _finished(db, family, keys)
    listed = {t["task_id"]: t for t in await db.tasks_of(family["parent"], at=NOW + 10 * DAY)}
    assert listed[task]["assessment"]["deemed"] is True


async def test_a_deemed_grade_takes_the_late_discount_like_any_other(queue, family, keys):
    _, db = queue
    task = await _post(db, family, cover=COVER, late={"kind": "discount", "pct_per_day": 10})
    await agree(db, keys["parent"], task)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    await agree(db, keys["child"], task)
    await db.act(family["child"], task, Action.MARK_DONE, at=DUE + DAY)  # one day late
    deemed = await db.get(family["parent"], task, at=DUE + 5 * DAY)
    assert deemed["assessment"]["sats"] == 720 and deemed["assessment"]["deemed"] is True


async def test_a_cover_page_without_a_deemed_grade_waits_for_the_purchaser(queue, family, keys):
    _, db = queue
    task = await _finished(db, family, keys, cover={"deliverables": ["fr.ts"]})
    assert (await db.get(family["parent"], task, at=NOW + 90 * DAY))["state"] == "done"


# ---------------------------------------------------------------------------
# Giving the job back
# ---------------------------------------------------------------------------


async def _with_paid_deposit(db, family):
    task = await _post(db, family, deposit_pct=25)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    claimed = await db._load(task)
    await db.save_advance(family["parent"], claimed, None, {"sats": 500, "payment_hash": "ab" * 32}, NOW + 20)
    await db.confirm_advance(family["child"], task, at=NOW + 30)
    return task


async def test_a_performer_who_gives_the_job_back_keeps_the_deposit_because_payments_are_final(queue, family):
    _, db = queue
    task = await _with_paid_deposit(db, family)
    await db.act(family["child"], task, Action.RELEASE, at=NOW + 40)
    last = (await db.events(family["parent"], task))[-1]
    assert last["deposit_kept_by_performer"] == 500 and "deposit_owed_back" not in last


async def test_a_performer_who_is_released_keeps_the_deposit(queue, family):
    _, db = queue
    task = await _with_paid_deposit(db, family)
    await db.act(family["parent"], task, Action.RELEASE, at=NOW + 40)
    last = (await db.events(family["parent"], task))[-1]
    assert last["deposit_kept_by_performer"] == 500 and "deposit_owed_back" not in last
