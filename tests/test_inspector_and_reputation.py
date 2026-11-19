# ruff: noqa: F811 — the fixtures are imported, then named as test arguments
"""An inspector named on the cover page signs the offer and gives one signed opinion of the work;
anyone may read what an npub did on commissions, role by role, as counts."""

import pytest
from pynostr.key import PrivateKey

from taskbooth_mcp.agreement import inspection_hash, inspection_statement
from taskbooth_mcp.escrow import roles_in
from taskbooth_mcp.lifecycle import Action
from taskbooth_mcp.reputation import reputation_of
from taskbooth_mcp.store import NotYours, checked_cover
from tests.signing import OPERATOR_NPUB, agree, dm_signature
from tests.test_store import DAY, NOW, RUBRIC, _post, queue  # noqa: F401 — fixtures


@pytest.fixture
def keys():
    return {role: PrivateKey() for role in ("parent", "child", "sibling", "other_parent", "inspector", "arbiter")}


@pytest.fixture
def family(keys):
    return {role: key.public_key.bech32() for role, key in keys.items()}


def _cover(family, **over):
    return {"deliverables": ["a mural"], "inspector_npub": family["inspector"]} | over


async def _signed_job(db, family, keys, **cover):
    task = await _post(db, family, cover=_cover(family, **cover))
    await agree(db, keys["parent"], task)
    await agree(db, keys["inspector"], task)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    await agree(db, keys["child"], task)
    return task


async def _opinion(db, keys, task, verdict="concerns", reasons="Two panels are unpainted.", at=NOW + 30):
    npub = keys["inspector"].public_key.bech32()
    stored = await db._load(task)
    digest = inspection_hash(stored, verdict, reasons)
    return await db.inspect(npub, task, verdict, reasons, dm_signature(keys["inspector"], digest),
                            operator_npub=OPERATOR_NPUB, at=at)


# ---------------------------------------------------------------------------
# The cover page
# ---------------------------------------------------------------------------


def test_the_inspector_is_someone_other_than_the_client_and_the_arbiter(family):
    with pytest.raises(ValueError, match="other than the purchaser and the arbiter"):
        checked_cover(_cover(family, inspector_npub=family["parent"]), RUBRIC, purchaser=family["parent"])
    with pytest.raises(ValueError, match="other than the purchaser and the arbiter"):
        checked_cover(_cover(family, arbiter_npub=family["inspector"]), RUBRIC, purchaser=family["parent"])
    with pytest.raises(ValueError, match="inspector"):
        checked_cover(_cover(family, inspector_npub="npub1nope"), RUBRIC, purchaser=family["parent"])
    page = checked_cover(_cover(family), RUBRIC, purchaser=family["parent"])
    assert page["inspector_npub"] == family["inspector"]
    assert checked_cover({"deliverables": []}, RUBRIC, purchaser=family["parent"])["inspector_npub"] == ""


async def test_the_inspector_sees_the_job_signs_its_offer_and_cannot_claim_it(queue, family, keys):
    _, db = queue
    task = await _post(db, family, cover=_cover(family), audience=[family["child"], family["inspector"]])
    listed = await db.tasks_of(family["inspector"], at=NOW + 1)
    assert [t["task_id"] for t in listed] == [task] and "inspector" in listed[0]["your_roles"]
    assert f"Inspector: {family['inspector']}" in (await db.offer(family["inspector"], task))["statement"]
    await agree(db, keys["parent"], task)
    signed = await agree(db, keys["inspector"], task)
    assert signed["agreement"]["inspector_signed"] is True
    assert "inspector" in (await db.offer(family["parent"], task))["signatures"]
    with pytest.raises(ValueError, match="inspector cannot also do the work"):
        await db.act(family["inspector"], task, Action.CLAIM, at=NOW + 10)


async def test_the_work_is_not_done_until_the_inspector_has_signed(queue, family, keys):
    _, db = queue
    task = await _post(db, family, cover=_cover(family))
    await agree(db, keys["parent"], task)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    await agree(db, keys["child"], task)
    with pytest.raises(ValueError, match="inspector has not signed"):
        await db.act(family["child"], task, Action.MARK_DONE, at=NOW + 20)
    await agree(db, keys["inspector"], task)
    assert (await db.act(family["child"], task, Action.MARK_DONE, at=NOW + 20))["state"] == "done"


async def test_an_edit_that_changes_the_inspector_changes_who_finds_the_job(queue, family):
    _, db = queue
    task = await _post(db, family, cover=_cover(family))
    await db.edit(family["parent"], task, {"cover": _cover(family, inspector_npub=family["arbiter"])}, at=NOW + 5)
    assert await db.tasks_of(family["inspector"], at=NOW + 6) == []
    assert [t["task_id"] for t in await db.tasks_of(family["arbiter"], at=NOW + 6)] == [task]


# ---------------------------------------------------------------------------
# The opinion
# ---------------------------------------------------------------------------


async def test_the_opinion_waits_for_delivery_and_is_seen_by_everyone_on_the_job(queue, family, keys):
    _, db = queue
    task = await _signed_job(db, family, keys)
    with pytest.raises(ValueError, match="not been delivered"):
        await db.inspection_task(family["inspector"], task, "verified", "Fine.")
    await db.act(family["child"], task, Action.MARK_DONE, at=NOW + 20)
    given = await _opinion(db, keys, task)
    assert given["state"] == "done"  # advice moves nothing
    for who in ("parent", "child", "other_parent"):
        seen = (await db.get(family[who], task, NOW + 40))["inspection"]
        assert seen["verdict"] == "concerns" and seen["reasons"] == "Two panels are unpainted."
        assert seen["inspector"] == family["inspector"] and "record" not in seen
    events = await db.events(family["parent"], task)
    assert events[-1]["action"] == "inspect" and events[-1]["verdict"] == "concerns"


async def test_the_opinion_may_come_after_grading_and_payment(queue, family, keys):
    _, db = queue
    task = await _signed_job(db, family, keys)
    await db.act(family["child"], task, Action.MARK_DONE, at=NOW + 20)
    await db.act(family["parent"], task, Action.ASSESS, grade="Spotless", at=NOW + 25)
    await db.act(family["child"], task, Action.CONFIRM_PAID, at=NOW + 26)
    assert (await _opinion(db, keys, task, verdict="verified", at=NOW + 30))["inspection"]["verdict"] == "verified"


async def test_one_opinion_stands_as_signed(queue, family, keys):
    _, db = queue
    task = await _signed_job(db, family, keys)
    await db.act(family["child"], task, Action.MARK_DONE, at=NOW + 20)
    await _opinion(db, keys, task)
    with pytest.raises(ValueError, match="already has the inspector's opinion"):
        await _opinion(db, keys, task, verdict="verified")


async def test_only_the_named_inspector_gives_an_opinion_and_only_one_that_is_well_formed(queue, family, keys):
    _, db = queue
    task = await _signed_job(db, family, keys)
    await db.act(family["child"], task, Action.MARK_DONE, at=NOW + 20)
    with pytest.raises(NotYours):
        await db.inspection_task(family["parent"], task, "verified", "Fine.")
    with pytest.raises(ValueError, match="verdict is one of"):
        await db.inspection_task(family["inspector"], task, "great", "Fine.")
    with pytest.raises(ValueError, match="reasons"):
        await db.inspection_task(family["inspector"], task, "verified", "")
    with pytest.raises(ValueError, match="reasons"):
        await db.inspection_task(family["inspector"], task, "verified", "x" * 2001)


async def test_a_signature_over_another_opinion_does_not_stand(queue, family, keys):
    _, db = queue
    task = await _signed_job(db, family, keys)
    await db.act(family["child"], task, Action.MARK_DONE, at=NOW + 20)
    stored = await db._load(task)
    other = inspection_hash(stored, "verified", "All good.")
    with pytest.raises(ValueError, match="does not stand"):
        await db.inspect(family["inspector"], task, "not_verified", "All bad.",
                         dm_signature(keys["inspector"], other), operator_npub=OPERATOR_NPUB, at=NOW + 30)


async def test_the_opinion_names_the_offer_it_is_about(queue, family, keys):
    _, db = queue
    task = await _signed_job(db, family, keys)
    await db.act(family["child"], task, Action.MARK_DONE, at=NOW + 20)
    text = inspection_statement(await db._load(task), "verified", "Fine.")
    assert f"Task Booth inspection of {task}" in text and "Verdict: verified" in text
    assert (await db.offer(family["parent"], task))["statement_hash"] in text


def test_the_inspector_holds_no_escrow_key(family):
    record = {"parties": {"purchaser": family["parent"], "performer": family["child"], "arbiter": family["arbiter"]}}
    assert roles_in(record, family["inspector"]) == []


# ---------------------------------------------------------------------------
# Reputation
# ---------------------------------------------------------------------------


async def test_reputation_counts_each_role_on_commissions_only(queue, family, keys):
    _, db = queue
    late = await _signed_job(db, family, keys)
    await db.act(family["child"], late, Action.MARK_DONE, at=NOW + 3 * DAY)  # a day past needed-by
    await _opinion(db, keys, late, at=NOW + 3 * DAY + 60)
    await db.act(family["parent"], late, Action.ASSESS, grade="Tidy", at=NOW + 3 * DAY + 120)
    chore = await _post(db, family)  # no cover page: a chore, which says nothing about anyone
    await db.act(family["child"], chore, Action.CLAIM, at=NOW + 10)
    await db.act(family["child"], chore, Action.MARK_DONE, at=NOW + 20)

    child = await db.reputation(family["child"], at=NOW + 4 * DAY)
    assert child["artist"]["commissions"] == 1 and child["artist"]["delivered"] == 1
    assert child["artist"]["on_time"] == 0 and child["artist"]["late"] == 1
    assert child["artist"]["grade_pct"] == 40 and child["artist"]["verdicts"]["concerns"] == 1
    parent = await db.reputation(family["parent"], at=NOW + 4 * DAY)
    assert parent["client"] == {"commissions": 1, "graded": 1, "graded_in_time": 1, "owed": 0, "paid": 0,
                                "appeals_lost": 0}
    inspector = (await db.reputation(family["inspector"], at=NOW + 4 * DAY))["inspector"]
    assert inspector["named"] == inspector["signed"] == inspector["opinions"] == inspector["opinions_in_time"] == 1
    nobody = await db.reputation(family["sibling"], at=NOW + 4 * DAY)
    assert nobody["artist"]["commissions"] == 0 and nobody["arbiter"]["named"] == 0
    with pytest.raises(ValueError, match="npub"):
        await db.reputation("not-an-npub")


def test_an_arbiter_decision_counts_in_time_within_the_arbiters_week():
    def job(asked, decided):
        return {"task_id": "t", "purchaser": "c", "performer": "w", "state": "owed", "needed_by": 0,
                "cover": {"arbiter_npub": "a"}, "agreement": {"arbiter": {"x": 1}},
                "assessment": {"grade": "g", "sats": 5, "appeal": {"at": asked, "decided_at": decided,
                                                                   "first_sats": 1}}}
    arbiter = reputation_of([job(0, 7 * DAY), job(0, 8 * DAY)], "a", {})["arbiter"]
    assert arbiter == {"named": 2, "signed": 2, "decided": 2, "decided_in_time": 1}
    client = reputation_of([job(0, DAY)], "c", {})["client"]
    assert client["appeals_lost"] == 1
    artist = reputation_of([job(0, DAY)], "w", {})["artist"]
    assert artist["appeals_won"] == 1 and artist["grade_pct"] is None


def test_failures_count_against_the_artist_by_cause():
    jobs = [{"task_id": t, "purchaser": "c", "performer": "w", "state": "missed", "needed_by": 0, "cover": {}}
            for t in ("a", "b", "c")]
    jobs = [{**j, "cover": {"deliverables": []}} for j in jobs]
    failures = {"a": {"without_cause": True}, "b": {"without_cause": False}, "c": {"without_cause": None}}
    artist = reputation_of(jobs, "w", failures)["artist"]
    assert artist["failed_without_cause"] == 1 and artist["failed_with_cause"] == 1
