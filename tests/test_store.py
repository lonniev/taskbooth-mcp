"""Tasks as the database is left holding them, on SQLite running the store's real SQL."""

import pytest
from pynostr.key import PrivateKey
from tollbooth.vault_encryption import VaultCipher

from taskqueue_mcp.lifecycle import Action
from taskqueue_mcp.store import NotYours, StoreUnavailable, TaskStore, checked_rubric
from tests.sqlite_neon import SqliteNeon

NOW = 1_800_000_000
DAY = 86_400
RUBRIC = {
    "grades": [
        {"name": "Spotless", "sats": 2000, "criteria": "Nothing left on the floor"},
        {"name": "Tidy", "sats": 800, "criteria": "Bed made, clothes away"},
        {"name": "Not done", "sats": 0, "criteria": ""},
    ],
    "appeal": "One appeal, decided by the other parent",
}


def _npub() -> str:
    return PrivateKey().public_key.bech32()


@pytest.fixture
async def queue():
    neon = SqliteNeon(cipher=VaultCipher(nsec_hex=PrivateKey().hex()))
    db = TaskStore(neon)
    await db.ensure_schema()
    return neon, db


@pytest.fixture
def family():
    return {"parent": _npub(), "child": _npub(), "sibling": _npub(), "other_parent": _npub()}


async def _post(db, family, **over):
    args = {
        "title": "Clean your room", "description": "Before Grandma visits", "rubric": RUBRIC,
        "audience": [family["child"], family["sibling"]], "needed_by": NOW + 2 * DAY,
        "tie_breaker": family["other_parent"], "at": NOW,
    } | over
    return await db.post(family["parent"], **args)


async def _to_done(db, family, task_id):
    await db.act(family["child"], task_id, Action.CLAIM, at=NOW + 10)
    await db.act(family["child"], task_id, Action.MARK_DONE, at=NOW + 20)


# ---------------------------------------------------------------------------
# What is stored
# ---------------------------------------------------------------------------


async def test_a_store_without_a_cipher_refuses_to_start():
    with pytest.raises(StoreUnavailable):
        TaskStore(SqliteNeon())


async def test_the_schema_has_the_four_tables_the_design_names(queue):
    neon, _ = queue
    assert {"taskqueue_tasks", "taskqueue_events", "taskqueue_payouts",
            "taskqueue_rubric_templates"} <= neon.tables()


async def test_words_and_amounts_are_never_stored_in_the_clear(queue, family):
    neon, db = queue
    task = await _post(db, family, purchaser_signature="sig-of-the-offer")
    await _to_done(db, family, task)
    await db.act(family["parent"], task, Action.ASSESS, grade="Tidy", reasons="Socks under the bed", at=NOW + 30)
    await db.save_payout(task, {"invoice": "lnbc8u1fake", "payment_hash": "ab" * 32, "sats": 800})
    await db.save_template(family["parent"], "Bedroom chores", RUBRIC)
    dump = repr([neon.rows(t) for t in neon.tables()])
    for secret in ("Clean your room", "Grandma", "Spotless", "2000", "Socks", "lnbc8u1fake", "ab" * 32,
                   "Bedroom chores", "sig-of-the-offer"):
        assert secret not in dump, secret


async def test_every_field_of_the_record_comes_back(queue, family):
    _, db = queue
    task = await _post(db, family, earliest_start=NOW + 100, org_id="org1", project_id="proj1",
                       purchaser_signature="sig-p")
    await db.act(family["child"], task, Action.CLAIM, performer_signature="sig-c", at=NOW + 200)
    await db.act(family["child"], task, Action.MARK_DONE, at=NOW + 300)
    await db.act(family["parent"], task, Action.ASSESS, grade="spotless", reasons="Lovely", at=NOW + 400)
    await db.save_payout(task, {"payment_hash": "cd" * 32, "sats": 2000, "method": "invoice"})
    seen = await db.act(family["child"], task, Action.CONFIRM_PAID, at=NOW + 500)

    assert seen["title"] == "Clean your room"
    assert seen["purchaser"] == family["parent"] and seen["performer"] == family["child"]
    assert seen["audience"] == sorted([family["child"], family["sibling"]])
    assert seen["needed_by"] == NOW + 2 * DAY and seen["earliest_start"] == NOW + 100
    assert seen["rubric"] == checked_rubric(RUBRIC)
    assert seen["signatures"] == {"purchaser": "sig-p", "performer": "sig-c"}
    assert seen["accomplished_at"] == NOW + 300
    assert seen["assessment"] == {"grade": "Spotless", "sats": 2000, "assessor": family["parent"],
                                  "reasons": "Lovely", "at": NOW + 400}
    assert seen["paid_at"] == NOW + 500
    assert (seen["org_id"], seen["project_id"]) == ("org1", "proj1")
    assert seen["state"] == "paid" and seen["standing"] == "paid" and seen["final"] is True
    assert await db.payout(task) == {"paid_at": NOW + 500, "payment_hash": "cd" * 32, "sats": 2000,
                                     "method": "invoice"}


# ---------------------------------------------------------------------------
# Moves
# ---------------------------------------------------------------------------


async def test_each_move_is_stamped_with_who_made_it_and_when(queue, family):
    _, db = queue
    task = await _post(db, family)
    await _to_done(db, family, task)
    await db.act(family["parent"], task, Action.ASSESS, grade="Tidy", reasons="Socks", at=NOW + 30)
    await db.act(family["child"], task, Action.APPEAL, reasons="They were clean socks", at=NOW + 40)
    await db.act(family["other_parent"], task, Action.DECIDE_APPEAL, grade="Spotless", at=NOW + 50)
    await db.act(family["child"], task, Action.CONFIRM_PAID, at=NOW + 60)

    trail = await db.events(family["parent"], task)
    assert [(e["seq"], e["action"], e["from"], e["to"], e["actor"], e["at"]) for e in trail] == [
        (1, "post", "", "posted", family["parent"], NOW),
        (2, "claim", "posted", "claimed", family["child"], NOW + 10),
        (3, "mark_done", "claimed", "done", family["child"], NOW + 20),
        (4, "assess", "done", "assessed", family["parent"], NOW + 30),
        (5, "appeal", "assessed", "appealed", family["child"], NOW + 40),
        (6, "decide_appeal", "appealed", "owed", family["other_parent"], NOW + 50),
        (7, "confirm_paid", "owed", "paid", family["child"], NOW + 60),
    ]
    assert trail[3]["grade"] == "Tidy" and trail[3]["sats"] == 800 and trail[3]["reasons"] == "Socks"

    seen = await db.get(family["child"], task, at=NOW + 70)
    assert seen["assessment"]["grade"] == "Spotless" and seen["assessment"]["sats"] == 2000
    assert seen["assessment"]["appeal"] == {
        "reasons": "They were clean socks", "at": NOW + 40, "first_grade": "Tidy", "first_sats": 800,
        "decided_by": family["other_parent"], "decision_reasons": "", "decided_at": NOW + 50,
    }


async def test_events_are_only_ever_inserted(queue, family):
    neon, db = queue
    task = await _post(db, family)
    await _to_done(db, family, task)
    await db.act(family["parent"], task, Action.REJECT, reasons="Not done at all", at=NOW + 30)
    touching = [s for s in neon.sql if "taskqueue_events" in s and not s.startswith(("CREATE", "SELECT"))]
    assert touching and all(s.startswith("INSERT INTO") for s in touching)


async def test_an_illegal_move_is_refused_and_changes_nothing(queue, family):
    neon, db = queue
    task = await _post(db, family)
    before = neon.rows("taskqueue_tasks"), neon.rows("taskqueue_events")
    with pytest.raises(ValueError, match="Only the audience may 'claim'"):
        await db.act(family["parent"], task, Action.CLAIM, at=NOW + 5)
    with pytest.raises(ValueError, match="cannot be given 'assess'"):
        await db.act(family["parent"], task, Action.ASSESS, grade="Tidy", at=NOW + 5)
    assert (neon.rows("taskqueue_tasks"), neon.rows("taskqueue_events")) == before


async def test_of_two_who_claim_at_once_only_one_wins(queue, family):
    """The second writer read the task before the first one moved it."""
    _, db = queue
    task = await _post(db, family)
    stale = await db._load(task)

    async def read_before_the_other_claim(_task_id):
        return dict(stale)

    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    db._load = read_before_the_other_claim
    with pytest.raises(ValueError, match="Someone else changed this task first"):
        await db.act(family["sibling"], task, Action.CLAIM, at=NOW + 11)
    del db._load
    assert (await db.get(family["parent"], task, at=NOW + 12))["performer"] == family["child"]
    assert len(await db.events(family["parent"], task)) == 2


async def test_the_purchaser_may_release_a_stale_claim(queue, family):
    _, db = queue
    task = await _post(db, family)
    await db.act(family["child"], task, Action.CLAIM, performer_signature="sig-c", at=NOW + 10)
    seen = await db.act(family["parent"], task, Action.RELEASE, at=NOW + 20)
    assert seen["state"] == "posted" and seen["performer"] is None
    assert seen["signatures"]["performer"] == ""
    await db.act(family["sibling"], task, Action.CLAIM, at=NOW + 30)


async def test_a_grade_not_in_the_rubric_is_refused_and_names_the_grades(queue, family):
    _, db = queue
    task = await _post(db, family)
    await _to_done(db, family, task)
    with pytest.raises(ValueError, match="Spotless, Tidy, Not done"):
        await db.act(family["parent"], task, Action.ASSESS, grade="Perfect", at=NOW + 30)


async def test_the_top_grade_is_owed_at_once(queue, family):
    _, db = queue
    task = await _post(db, family)
    await _to_done(db, family, task)
    seen = await db.act(family["parent"], task, Action.ASSESS, grade="Spotless", at=NOW + 30)
    assert seen["state"] == "owed" and seen["final"] is True


async def test_an_accepted_zero_sat_grade_is_finished_with_nothing_owed(queue, family):
    _, db = queue
    task = await _post(db, family)
    await _to_done(db, family, task)
    await db.act(family["parent"], task, Action.ASSESS, grade="Not done", at=NOW + 30)
    seen = await db.act(family["child"], task, Action.ACCEPT, at=NOW + 40)
    assert seen["state"] == "assessed" and seen["final"] is True
    with pytest.raises(ValueError, match="assessed and final"):
        await db.act(family["child"], task, Action.APPEAL, at=NOW + 50)


async def test_a_delegated_assessor_may_grade(queue, family):
    _, db = queue
    task = await _post(db, family, assessors=[family["other_parent"]])
    await _to_done(db, family, task)
    seen = await db.act(family["other_parent"], task, Action.ASSESS, grade="Tidy", at=NOW + 30)
    assert seen["assessment"]["assessor"] == family["other_parent"]


async def test_only_the_service_may_record_a_proven_payment(queue, family):
    _, db = queue
    task = await _post(db, family)
    await _to_done(db, family, task)
    await db.act(family["parent"], task, Action.ASSESS, grade="Spotless", at=NOW + 30)
    with pytest.raises(ValueError, match="Only the operator"):
        await db.act(family["parent"], task, Action.RECORD_PAYMENT, at=NOW + 40)
    seen = await db.act(family["parent"], task, Action.RECORD_PAYMENT, as_operator=True, at=NOW + 50)
    assert seen["state"] == "paid" and seen["paid_at"] == NOW + 50


async def test_one_npub_may_post_claim_grade_and_pay_itself(queue):
    """The owner tests with real money this way; nothing asks who works for whom."""
    _, db = queue
    me = _npub()
    task = await db.post(me, title="Try the whole flow", rubric=RUBRIC, audience=[me],
                         needed_by=NOW + DAY, at=NOW)
    for action, extra in (
        (Action.CLAIM, {}), (Action.MARK_DONE, {}), (Action.ASSESS, {"grade": "Tidy"}),
        (Action.APPEAL, {}), (Action.DECIDE_APPEAL, {"grade": "Spotless"}), (Action.CONFIRM_PAID, {}),
    ):
        seen = await db.act(me, task, action, at=NOW + 60, **extra)
    assert seen["state"] == "paid"
    assert seen["your_roles"] == ["assessor", "audience", "performer", "purchaser", "tie_breaker"]


# ---------------------------------------------------------------------------
# Who may see what
# ---------------------------------------------------------------------------


async def test_a_stranger_sees_nothing_and_can_do_nothing(queue, family):
    _, db = queue
    task = await _post(db, family)
    stranger = _npub()
    with pytest.raises(NotYours):
        await db.get(stranger, task)
    with pytest.raises(NotYours):
        await db.act(stranger, task, Action.CLAIM, at=NOW + 5)
    with pytest.raises(NotYours):
        await db.events(stranger, task)
    with pytest.raises(NotYours):
        await db.get(stranger, "no-such-task")
    assert await db.tasks_of(stranger, at=NOW + 5) == []


async def test_the_audience_sees_a_task_only_while_it_is_open(queue, family):
    _, db = queue
    task = await _post(db, family)
    assert [t["task_id"] for t in await db.tasks_of(family["sibling"], at=NOW + 5)] == [task]
    with pytest.raises(NotYours):
        await db.events(family["sibling"], task)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    assert await db.tasks_of(family["sibling"], at=NOW + 15) == []
    with pytest.raises(NotYours):
        await db.get(family["sibling"], task, at=NOW + 15)


async def test_each_person_lists_the_tasks_they_have_a_part_in(queue, family):
    _, db = queue
    soon = await _post(db, family, needed_by=NOW + DAY)
    later = await _post(db, family, title="Wash the car", needed_by=NOW + 3 * DAY)
    await db.act(family["child"], later, Action.CLAIM, at=NOW + 10)
    assert [t["task_id"] for t in await db.tasks_of(family["parent"], at=NOW + 20)] == [soon, later]
    assert [t["task_id"] for t in await db.tasks_of(family["child"], at=NOW + 20)] == [soon, later]
    assert [t["task_id"] for t in await db.tasks_of(family["other_parent"], at=NOW + 20)] == [soon, later]


# ---------------------------------------------------------------------------
# No clock
# ---------------------------------------------------------------------------


async def test_an_unclaimed_task_past_its_time_reads_as_expired_and_stays_posted(queue, family):
    neon, db = queue
    task = await _post(db, family)
    seen = await db.get(family["parent"], task, at=NOW + 3 * DAY)
    assert seen["standing"] == "expired" and seen["state"] == "posted"
    assert neon.rows("taskqueue_tasks")[0]["state"] == "posted"
    with pytest.raises(ValueError, match="needed-by time has passed"):
        await db.act(family["child"], task, Action.CLAIM, at=NOW + 3 * DAY)
    withdrawn = await db.act(family["parent"], task, Action.WITHDRAW, at=NOW + 3 * DAY)
    assert withdrawn["standing"] == "withdrawn"


async def test_finishing_a_recurring_task_posts_its_next_occurrence(queue, family):
    _, db = queue
    first = await _post(db, family, every_days=7, earliest_start=NOW + DAY, purchaser_signature="sig-p")
    await db.act(family["child"], first, Action.CLAIM, performer_signature="sig-c", at=NOW + 10)
    await db.act(family["child"], first, Action.MARK_DONE, at=NOW + 20)

    tasks = await db.tasks_of(family["parent"], at=NOW + 30)
    assert len(tasks) == 2
    nxt = next(t for t in tasks if t["task_id"] != first)
    assert nxt["state"] == "posted" and nxt["performer"] is None
    assert nxt["needed_by"] == NOW + 9 * DAY and nxt["earliest_start"] == NOW + 8 * DAY
    assert (nxt["series_id"], nxt["occurrence"]) == (first, 1)
    assert nxt["title"] == "Clean your room" and nxt["rubric"] == checked_rubric(RUBRIC)
    assert nxt["signatures"] == {"purchaser": "sig-p", "performer": ""}
    assert [e["action"] for e in await db.events(family["parent"], nxt["task_id"])] == ["recur"]
    # The sibling may claim next week's even though the child did this week's.
    await db.act(family["sibling"], nxt["task_id"], Action.CLAIM, at=NOW + 40)


async def test_a_one_off_task_posts_nothing_when_it_is_done(queue, family):
    _, db = queue
    task = await _post(db, family)
    await _to_done(db, family, task)
    assert len(await db.tasks_of(family["parent"], at=NOW + 30)) == 1


async def test_a_recurring_task_nobody_did_comes_back_once_on_the_next_read(queue, family):
    neon, db = queue
    first = await _post(db, family, every_days=7)
    at = NOW + 2 * DAY + 15 * DAY  # two whole weeks missed
    tasks = await db.tasks_of(family["child"], at=at)
    writes = [s for s in neon.sql if not s.startswith("SELECT")]
    for _ in range(2):
        tasks = await db.tasks_of(family["child"], at=at)
    assert [s for s in neon.sql if not s.startswith("SELECT")] == writes, "a later read writes nothing"
    assert len(neon.rows("taskqueue_tasks")) == 2
    assert [t["standing"] for t in tasks] == ["expired", "posted"]
    assert tasks[1]["needed_by"] == NOW + 2 * DAY + 21 * DAY
    assert tasks[0]["task_id"] == first


async def test_a_withdrawn_recurring_task_does_not_come_back(queue, family):
    neon, db = queue
    task = await _post(db, family, every_days=7)
    await db.act(family["parent"], task, Action.WITHDRAW, at=NOW + 10)
    await db.tasks_of(family["parent"], at=NOW + 30 * DAY)
    assert len(neon.rows("taskqueue_tasks")) == 1


# ---------------------------------------------------------------------------
# What a caller hands in
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("over", "says"),
    [
        ({"title": "   "}, "title is required"),
        ({"title": "x" * 201}, "longer than 200"),
        ({"audience": []}, "from 1 to 500 npubs"),
        ({"audience": ["npub1notreal"]}, "not a valid npub"),
        ({"audience": ["alice@example.com"]}, "must be an npub"),
        ({"needed_by": NOW - 1}, "must be in the future"),
        ({"needed_by": "tomorrow"}, "whole number"),
        ({"earliest_start": NOW + 9 * DAY}, "cannot be after"),
        ({"every_days": -1}, "whole number"),
        ({"tie_breaker": "nobody"}, "must be an npub"),
        ({"rubric": {"grades": []}}, "from 1 to 12 grades"),
        ({"rubric": {"grades": [{"name": "A", "sats": -5}]}}, "whole number"),
        ({"rubric": {"grades": [{"name": "A", "sats": True}]}}, "whole number"),
        ({"rubric": {"grades": [{"name": "A", "sats": 1.5}]}}, "whole number"),
        ({"rubric": {"grades": [{"name": "A", "sats": 1}, {"name": "a", "sats": 2}]}}, "share a name"),
        ({"rubric": "be good"}, "list of grades"),
    ],
)
async def test_a_malformed_task_is_refused_and_nothing_is_stored(queue, family, over, says):
    neon, db = queue
    with pytest.raises(ValueError, match=says):
        await _post(db, family, **over)
    assert neon.rows("taskqueue_tasks") == []


async def test_a_task_with_no_named_tie_breaker_falls_to_the_purchaser(queue, family):
    _, db = queue
    task = await _post(db, family, tie_breaker="")
    assert (await db.get(family["parent"], task, at=NOW + 1))["tie_breaker"] == family["parent"]


# ---------------------------------------------------------------------------
# Rubric templates
# ---------------------------------------------------------------------------


async def test_a_rubric_template_is_kept_replaced_and_private_to_its_owner(queue, family):
    _, db = queue
    kept = await db.save_template(family["parent"], "Bedroom chores", RUBRIC)
    await db.save_template(family["parent"], "Bedrooms", {"grades": [{"name": "Done", "sats": 500}]}, kept)
    mine = await db.templates_of(family["parent"])
    assert [(t["template_id"], t["name"]) for t in mine] == [(kept, "Bedrooms")]
    assert mine[0]["rubric"]["grades"] == [{"name": "Done", "sats": 500, "criteria": ""}]
    assert await db.templates_of(family["child"]) == []
    with pytest.raises(NotYours):
        await db.save_template(family["child"], "Mine now", RUBRIC, kept)
