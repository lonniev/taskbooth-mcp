"""The Task Booth as a caller meets it, through FastMCP's in-memory client."""

import json
import time

import pytest
from fastmcp import Client
from pynostr.key import PrivateKey
from tollbooth import bolt11
from tollbooth.lnurl import LnurlResolutionError
from tollbooth.vault_encryption import VaultCipher

from taskbooth_mcp import payout, server
from taskbooth_mcp.store import TaskStore
from tests.invoices import make_invoice, payment_hash_of
from tests.sqlite_neon import SqliteNeon

PARENT, CHILD, SIBLING, OTHER_PARENT, STRANGER = (PrivateKey().public_key.bech32() for _ in range(5))
DAY = 86_400
RUBRIC = {
    "grades": [
        {"name": "Spotless", "sats": 2000, "criteria": "Nothing left on the floor"},
        {"name": "Tidy", "sats": 800},
        {"name": "Not done", "sats": 0},
    ],
}


@pytest.fixture
def billing(monkeypatch):
    """Stand in for the wheel's debit and rollback; the debit proves the caller."""
    calls = {"debit": 0, "rollback": 0, "proven": {PARENT, CHILD, SIBLING, OTHER_PARENT, STRANGER}}

    async def debit_or_deny(tool_id, npub, **kw):
        if npub not in calls["proven"]:
            return {"success": False, "error_code": "proof_required", "error": "prove it"}
        calls["debit"] += 1
        return 1

    async def rollback_debit(tool_id, npub, **kw):
        calls["rollback"] += 1

    async def same(result, npub):
        return result

    monkeypatch.setattr(server.runtime, "debit_or_deny", debit_or_deny)
    monkeypatch.setattr(server.runtime, "rollback_debit", rollback_debit)
    monkeypatch.setattr(server.runtime, "inject_low_balance_warning", same)
    for name in ("fire_and_forget_demand_increment", "fire_and_forget_supply_increment",
                 "fire_and_forget_notarize_if_stale"):
        monkeypatch.setattr(server.runtime, name, lambda *a, **k: None)
    return calls


@pytest.fixture
async def queue(monkeypatch):
    db = TaskStore(SqliteNeon(cipher=VaultCipher(nsec_hex=PrivateKey().hex())))
    await db.ensure_schema()

    async def store():
        return db
    monkeypatch.setattr(server, "store", store)
    return db


@pytest.fixture
def wallets(monkeypatch):
    """Each npub's payout_address, and the invoices their wallets hand out."""
    state = {"addresses": {CHILD: "kid@wallet.example", SIBLING: "sib@wallet.example"},
             "asked": [], "invoice": None, "fails": None}

    async def payout_address_of(npub):
        return state["addresses"].get(npub, "")

    async def resolve(address, sats, **kw):
        state["asked"].append((address, sats))
        if state["fails"]:
            raise LnurlResolutionError(state["fails"])
        return state["invoice"] or make_invoice(sats, preimage=f"{len(state['asked']):02x}" * 32)

    monkeypatch.setattr(server, "payout_address_of", payout_address_of)
    monkeypatch.setattr(payout, "resolve_lightning_address", resolve)
    return state


async def call(tool, **args):
    async with Client(server.mcp) as c:
        result = await c.call_tool(f"taskbooth_{tool}", args, raise_on_error=False)
    return json.loads(result.content[0].text)


async def post(**over):
    args = {"title": "Clean your room", "rubric": RUBRIC, "audience": [CHILD, SIBLING],
            "needed_by": int(time.time()) + 2 * DAY, "tie_breaker": OTHER_PARENT, "npub": PARENT} | over
    return (await call("post_task", **args))["task"]["task_id"]


async def done(task):
    assert (await call("claim_task", task_id=task, npub=CHILD))["ok"]
    assert (await call("mark_done", task_id=task, npub=CHILD))["ok"]


async def owed(task, grade="Tidy"):
    await done(task)
    await call("assess_task", task_id=task, grade=grade, npub=PARENT)
    if grade != "Spotless":
        await call("accept_assessment", task_id=task, npub=CHILD)


# ---------------------------------------------------------------------------
# A task, start to finish
# ---------------------------------------------------------------------------


async def test_a_chore_goes_from_posted_to_paid(billing, queue, wallets):
    task = await post(description="Before Grandma visits")
    assert [t["task_id"] for t in (await call("list_tasks", npub=CHILD))["tasks"]] == [task]

    claimed = await call("claim_task", task_id=task, npub=CHILD)
    assert claimed["task"]["state"] == "claimed" and claimed["task"]["performer"] == CHILD
    assert (await call("mark_done", task_id=task, npub=CHILD))["task"]["state"] == "done"

    graded = await call("assess_task", task_id=task, grade="tidy", reasons="Socks under the bed", npub=PARENT)
    assert graded["task"]["state"] == "assessed" and graded["task"]["final"] is False
    assert graded["task"]["assessment"] == graded["task"]["assessment"] | {
        "grade": "Tidy", "sats": 800, "assessor": PARENT, "reasons": "Socks under the bed"}

    assert (await call("accept_assessment", task_id=task, npub=CHILD))["task"]["state"] == "owed"

    due = (await call("request_payout", task_id=task, npub=PARENT))["payout"]
    assert due["sats"] == 800 and due["lightning_uri"] == f"lightning:{due['invoice']}"
    assert bolt11.decode(due["invoice"]).amount_msat == 800_000
    assert wallets["asked"] == [("kid@wallet.example", 800)]

    paid = await call("confirm_paid", task_id=task, npub=CHILD)
    assert paid["task"]["state"] == "paid" and paid["task"]["paid_at"]
    seen = await call("get_task", task_id=task, npub=PARENT)
    assert seen["payout"]["paid_at"] == paid["task"]["paid_at"]
    assert [e["action"] for e in (await call("task_history", task_id=task, npub=PARENT))["events"]] == [
        "post", "claim", "mark_done", "assess", "accept", "confirm_paid"]


async def test_one_npub_posts_claims_grades_and_pays_itself(billing, queue, wallets):
    """The owner tests with real money this way."""
    wallets["addresses"][PARENT] = "me@wallet.example"
    task = await post(audience=[PARENT], tie_breaker="")
    for tool, extra in (("claim_task", {}), ("mark_done", {}), ("assess_task", {"grade": "Tidy"}),
                        ("appeal_assessment", {"reasons": "I did better"}),
                        ("decide_appeal", {"grade": "Spotless"})):
        assert (await call(tool, task_id=task, npub=PARENT, **extra))["ok"], tool
    due = (await call("request_payout", task_id=task, npub=PARENT))["payout"]
    assert due["sats"] == 2000 and wallets["asked"] == [("me@wallet.example", 2000)]
    assert (await call("confirm_paid", task_id=task, npub=PARENT))["task"]["state"] == "paid"


# ---------------------------------------------------------------------------
# Assessment
# ---------------------------------------------------------------------------


async def test_the_top_grade_is_owed_without_waiting(billing, queue, wallets):
    task = await post()
    await done(task)
    graded = await call("assess_task", task_id=task, grade="Spotless", npub=PARENT)
    assert graded["task"]["state"] == "owed" and graded["task"]["final"] is True


async def test_exactly_one_appeal_and_its_outcome_is_final(billing, queue, wallets):
    task = await post()
    await done(task)
    await call("assess_task", task_id=task, grade="Not done", npub=PARENT)
    assert (await call("appeal_assessment", task_id=task, reasons="I did do it", npub=CHILD))["task"]["state"] == "appealed"
    assert not (await call("decide_appeal", task_id=task, grade="Tidy", npub=PARENT))["success"]
    decided = await call("decide_appeal", task_id=task, grade="Tidy", reasons="Half done", npub=OTHER_PARENT)
    assert decided["task"]["state"] == "owed" and decided["task"]["assessment"]["sats"] == 800
    assert decided["task"]["assessment"]["appeal"]["first_grade"] == "Not done"
    again = await call("appeal_assessment", task_id=task, npub=CHILD)
    assert not again["success"] and "cannot be given 'appeal'" in again["error"]


async def test_nothing_is_owed_before_the_assessment_is_final(billing, queue, wallets):
    task = await post()
    await done(task)
    await call("assess_task", task_id=task, grade="Tidy", npub=PARENT)
    early = await call("request_payout", task_id=task, npub=PARENT)
    assert not early["success"] and "only for a task that is owed" in early["error"]
    assert wallets["asked"] == []


@pytest.mark.parametrize(
    ("tool", "extra"),
    [("assess_task", {"grade": "Tidy"}), ("reject_task", {"reasons": "no"})],
)
async def test_only_an_assessor_may_grade_or_reject(billing, queue, wallets, tool, extra):
    task = await post()
    await done(task)
    for who in (CHILD, OTHER_PARENT, SIBLING, STRANGER):
        refused = await call(tool, task_id=task, npub=who, **extra)
        assert not refused["success"], who
    assert (await call("get_task", task_id=task, npub=PARENT))["task"]["state"] == "done"
    assert billing["rollback"] == 4


async def test_only_the_named_tie_breaker_decides_an_appeal(billing, queue, wallets):
    task = await post()
    await done(task)
    await call("assess_task", task_id=task, grade="Tidy", npub=PARENT)
    await call("appeal_assessment", task_id=task, npub=CHILD)
    for who in (PARENT, CHILD, SIBLING, STRANGER):
        assert not (await call("decide_appeal", task_id=task, grade="Spotless", npub=who))["success"], who
    assert (await call("decide_appeal", task_id=task, grade="Spotless", npub=OTHER_PARENT))["ok"]


async def test_a_delegate_may_grade_until_the_purchaser_takes_it_back(billing, queue, wallets):
    task = await post(tie_breaker="")
    await done(task)
    assert not (await call("delegate_assessor", task_id=task, assessor_npub=OTHER_PARENT, npub=CHILD))["success"]
    on = await call("delegate_assessor", task_id=task, assessor_npub=OTHER_PARENT, npub=PARENT)
    assert OTHER_PARENT in on["task"]["assessors"]
    off = await call("delegate_assessor", task_id=task, assessor_npub=OTHER_PARENT, allowed=False, npub=PARENT)
    assert OTHER_PARENT not in off["task"]["assessors"]
    assert not (await call("assess_task", task_id=task, grade="Tidy", npub=OTHER_PARENT))["success"]
    await call("delegate_assessor", task_id=task, assessor_npub=OTHER_PARENT, npub=PARENT)
    graded = await call("assess_task", task_id=task, grade="Tidy", npub=OTHER_PARENT)
    assert graded["task"]["assessment"]["assessor"] == OTHER_PARENT
    keep = await call("delegate_assessor", task_id=task, assessor_npub=PARENT, allowed=False, npub=PARENT)
    assert not keep["success"] and "always grades" in keep["error"]


async def test_a_rejected_task_owes_nothing(billing, queue, wallets):
    task = await post()
    await done(task)
    rejected = await call("reject_task", task_id=task, reasons="Not started", npub=PARENT)
    assert rejected["task"]["state"] == "rejected"
    assert not (await call("request_payout", task_id=task, npub=PARENT))["success"]


# ---------------------------------------------------------------------------
# Payout by invoice
# ---------------------------------------------------------------------------


async def test_the_payment_hash_is_on_record_before_the_invoice_is_handed_over(billing, queue, wallets, monkeypatch):
    task = await post()
    await owed(task)
    order = []
    save = queue.save_payout

    async def save_and_note(task_id, data):
        order.append(("saved", data["payment_hash"]))
        await save(task_id, data)
    monkeypatch.setattr(queue, "save_payout", save_and_note)

    due = (await call("request_payout", task_id=task, npub=PARENT))["payout"]
    assert order == [("saved", due["payment_hash"])]
    assert due["payment_hash"] == payment_hash_of("01" * 32)
    kept = await queue.payout(task)
    assert kept["invoice"] == due["invoice"] and kept["method"] == "invoice"
    assert kept["requested_by"] == PARENT and kept["address"] == "kid@wallet.example"


async def test_asking_again_returns_the_same_invoice_until_it_expires(billing, queue, wallets):
    task = await post()
    await owed(task)
    first = (await call("request_payout", task_id=task, npub=PARENT))["payout"]
    again = (await call("request_payout", task_id=task, npub=CHILD))["payout"]
    assert again == first and len(wallets["asked"]) == 1


async def test_an_expired_invoice_is_replaced_by_a_fresh_one(billing, queue, wallets):
    task = await post()
    await owed(task)
    first = (await call("request_payout", task_id=task, npub=PARENT))["payout"]
    kept = await queue.payout(task)
    await queue.save_payout(task, {k: v for k, v in kept.items() if k != "paid_at"} | {"expires_at": int(time.time()) + 60})
    fresh = (await call("request_payout", task_id=task, npub=PARENT))["payout"]
    assert fresh["payment_hash"] != first["payment_hash"] and len(wallets["asked"]) == 2
    assert (await queue.payout(task))["payment_hash"] == fresh["payment_hash"]


async def test_an_invoice_about_to_expire_is_refused(billing, queue, wallets):
    task = await post()
    await owed(task)
    wallets["invoice"] = make_invoice(800, created_at=int(time.time()) - 500, expiry=600)
    refused = await call("request_payout", task_id=task, npub=PARENT)
    assert not refused["success"] and "already expired" in refused["error"]
    assert await queue.payout(task) is None


@pytest.mark.parametrize("sats", [801, 799, 8000, None])
async def test_an_invoice_for_any_other_amount_is_refused_and_never_shown(billing, queue, wallets, sats):
    task = await post()
    await owed(task)
    wallets["invoice"] = make_invoice(sats)
    refused = await call("request_payout", task_id=task, npub=PARENT)
    assert not refused["success"] and "different amount" in refused["error"]
    assert wallets["invoice"] not in json.dumps(refused)
    assert await queue.payout(task) is None
    assert (await call("get_task", task_id=task, npub=PARENT))["task"]["state"] == "owed"


async def test_a_wallet_that_gives_no_invoice_leaves_the_task_owed(billing, queue, wallets):
    task = await post()
    await owed(task)
    wallets["fails"] = "LNURL endpoint returned HTTP 503"
    refused = await call("request_payout", task_id=task, npub=PARENT)
    assert not refused["success"] and "gave no usable invoice" in refused["error"]
    wallets["fails"] = None
    assert (await call("request_payout", task_id=task, npub=PARENT))["ok"]


async def test_an_invoice_that_is_not_one_is_refused(billing, queue, wallets):
    task = await post()
    await owed(task)
    wallets["invoice"] = "lnbc-not-really"
    refused = await call("request_payout", task_id=task, npub=PARENT)
    assert not refused["success"] and "gave no usable invoice" in refused["error"]


async def test_only_the_purchaser_or_performer_may_ask_for_the_invoice(billing, queue, wallets):
    task = await post()
    await owed(task)
    for who in (OTHER_PARENT, SIBLING, STRANGER):
        assert not (await call("request_payout", task_id=task, npub=who))["success"], who
    assert wallets["asked"] == []


async def test_only_the_performer_confirms_the_sats_arrived(billing, queue, wallets):
    task = await post()
    await owed(task)
    await call("request_payout", task_id=task, npub=PARENT)
    for who in (PARENT, OTHER_PARENT, STRANGER):
        assert not (await call("confirm_paid", task_id=task, npub=who))["success"], who
    assert (await call("confirm_paid", task_id=task, npub=CHILD))["task"]["state"] == "paid"


async def test_a_performer_whose_address_has_gone_is_told_so(billing, queue, wallets):
    task = await post()
    await owed(task)
    del wallets["addresses"][CHILD]
    refused = await call("request_payout", task_id=task, npub=PARENT)
    assert not refused["success"] and "has not set a payout_address" in refused["error"]


# ---------------------------------------------------------------------------
# Payout by the performer's own invoice
# ---------------------------------------------------------------------------


async def test_a_performer_with_no_address_hands_in_their_own_invoice(billing, queue, wallets):
    wallets["addresses"][CHILD] = ""
    task = await post()
    await owed(task)
    told = await call("request_payout", task_id=task, npub=PARENT)
    assert not told["success"] and "exactly 800 sats" in told["error"]

    mine = make_invoice(800, preimage="ab" * 32)
    handed = (await call("request_payout", task_id=task, invoice=mine, npub=CHILD))["payout"]
    assert handed["invoice"] == mine and handed["payment_hash"] == payment_hash_of("ab" * 32)
    assert (await call("request_payout", task_id=task, npub=PARENT))["payout"]["invoice"] == mine
    assert wallets["asked"] == []
    assert (await call("confirm_paid", task_id=task, npub=CHILD))["task"]["state"] == "paid"


@pytest.mark.parametrize("shown", ["lightning:{}", "LIGHTNING:{}", "  {}\n"])
async def test_an_invoice_is_taken_as_a_wallet_shows_it(billing, queue, wallets, shown):
    task = await post()
    await owed(task)
    mine = make_invoice(800)
    handed = await call("request_payout", task_id=task, invoice=shown.format(mine), npub=CHILD)
    assert handed["payout"]["invoice"] == mine
    upper = await call("request_payout", task_id=task, invoice=mine.upper(), npub=CHILD)
    assert upper["payout"]["invoice"] == mine


async def test_the_performers_invoice_replaces_one_fetched_from_their_address(billing, queue, wallets):
    task = await post()
    await owed(task)
    fetched = (await call("request_payout", task_id=task, npub=PARENT))["payout"]["invoice"]
    mine = make_invoice(800, preimage="cd" * 32)
    assert mine != fetched
    await call("request_payout", task_id=task, invoice=mine, npub=CHILD)
    assert (await call("request_payout", task_id=task, npub=PARENT))["payout"]["invoice"] == mine


@pytest.mark.parametrize("sats", [801, 8000, None])
async def test_a_performers_invoice_for_any_other_amount_is_refused(billing, queue, wallets, sats):
    wallets["addresses"][CHILD] = ""
    task = await post()
    await owed(task)
    refused = await call("request_payout", task_id=task, invoice=make_invoice(sats), npub=CHILD)
    assert not refused["success"] and "different amount" in refused["error"]
    assert await queue.payout(task) is None


async def test_a_performers_invoice_that_is_not_one_or_has_expired_is_refused(billing, queue, wallets):
    task = await post()
    await owed(task)
    for bad, why in (("hello", "not a Lightning invoice"), ("lnbc-not-really", "no usable invoice"),
                     ("lno1qcp4256ypq", "no usable invoice"),
                     (make_invoice(800, created_at=int(time.time()) - 500, expiry=600), "already expired")):
        refused = await call("request_payout", task_id=task, invoice=bad, npub=CHILD)
        assert not refused["success"] and why in refused["error"], bad
    assert await queue.payout(task) is None


async def test_only_the_performer_hands_in_an_invoice(billing, queue, wallets):
    task = await post()
    await owed(task)
    for who in (PARENT, OTHER_PARENT, SIBLING, STRANGER):
        refused = await call("request_payout", task_id=task, invoice=make_invoice(800), npub=who)
        assert not refused["success"], who
    assert await queue.payout(task) is None


async def test_an_invoice_is_not_taken_before_the_task_is_owed(billing, queue, wallets):
    task = await post()
    await done(task)
    refused = await call("request_payout", task_id=task, invoice=make_invoice(800), npub=CHILD)
    assert not refused["success"] and "owed" in refused["error"]


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------


async def test_a_performer_with_no_payout_address_may_still_claim(billing, queue, wallets):
    task = await post()
    wallets["addresses"][CHILD] = ""
    assert (await call("claim_task", task_id=task, npub=CHILD))["task"]["state"] == "claimed"


async def test_someone_outside_the_audience_can_neither_see_nor_claim(billing, queue, wallets):
    task = await post()
    wallets["addresses"][STRANGER] = "x@wallet.example"
    assert (await call("list_tasks", npub=STRANGER))["tasks"] == []
    for tool in ("get_task", "claim_task", "task_history"):
        refused = await call(tool, task_id=task, npub=STRANGER)
        assert not refused["success"] and "yours to see" in refused["error"], tool


async def test_the_purchaser_takes_back_a_stale_claim_and_withdraws(billing, queue, wallets):
    task = await post()
    await call("claim_task", task_id=task, npub=CHILD)
    assert (await call("release_claim", task_id=task, npub=PARENT))["task"]["state"] == "posted"
    assert (await call("withdraw_task", task_id=task, npub=PARENT))["task"]["standing"] == "withdrawn"


# ---------------------------------------------------------------------------
# Callers and inputs
# ---------------------------------------------------------------------------


async def test_an_unproven_npub_is_denied_before_anything_runs(billing, queue, wallets):
    billing["proven"].discard(PARENT)
    denied = await call("post_task", title="x", rubric=RUBRIC, audience=[CHILD],
                        needed_by=int(time.time()) + DAY, npub=PARENT)
    assert denied["error_code"] == "proof_required"
    assert await queue.tasks_of(PARENT) == []


async def test_a_malformed_task_is_refused_and_the_fare_refunded(billing, queue, wallets):
    refused = await call("post_task", title="x", rubric={"grades": []}, audience=[CHILD],
                         needed_by=int(time.time()) + DAY, npub=PARENT)
    assert not refused["success"] and "from 1 to 12 grades" in refused["error"]
    assert billing["rollback"] == 1


async def test_rubric_templates_are_kept_per_owner(billing, queue, wallets):
    kept = (await call("save_rubric_template", name="Bedroom chores", rubric=RUBRIC, npub=PARENT))["template_id"]
    mine = (await call("list_rubric_templates", npub=PARENT))["templates"]
    assert [(t["template_id"], t["name"]) for t in mine] == [(kept, "Bedroom chores")]
    assert (await call("list_rubric_templates", npub=CHILD))["templates"] == []
    stolen = await call("save_rubric_template", name="Mine", rubric=RUBRIC, template_id=kept, npub=CHILD)
    assert not stolen["success"]


def test_a_payout_address_is_a_lightning_address():
    assert payout.checked_address("  Kid@Wallet.Example ") == "kid@wallet.example"
    for bad in ("", "kid", "kid@", "@wallet.example", "kid@wallet", "a b@wallet.example", "a@b@c.example", None, 7):
        with pytest.raises(ValueError):
            payout.checked_address(bad)


# ---------------------------------------------------------------------------
# Calendar feeds
# ---------------------------------------------------------------------------

ZONE = "America/New_York"


@pytest.fixture
def anyone(billing):
    """A calendar app is nobody: it carries a token and no npub."""
    billing["proven"].add("")
    return billing


async def feed_of(who, **over):
    args = {"name": "Sam's chores", "time_zone": ZONE, "npub": who} | over
    return await call("calendar_publish", **args)


async def test_a_feed_holds_the_tasks_of_whoever_published_it(anyone, queue, wallets):
    await post(title="🧹 Sweep the kitchen floor", description="After supper, under the table")
    token = (await feed_of(CHILD))["feed"]["token"]
    got = await call("calendar_fetch", token=token)
    assert got["ok"] and got["count"] == 1 and got["name"] == "Sam's chores"
    ics = got["ics"]
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert "SUMMARY:🧹 Sweep the kitchen floor" in ics
    assert "After supper\\, under the table\\nEarns up to 2000 sats." in ics.replace("\r\n ", "")
    assert "X-WR-CALNAME:Sam's chores" in ics


async def test_a_feed_names_nobody(anyone, queue, wallets):
    task = await post()
    await done(task)
    token = (await feed_of(CHILD))["feed"]["token"]
    ics = (await call("calendar_fetch", token=token))["ics"].replace("\r\n ", "")
    for npub in (PARENT, CHILD, SIBLING, OTHER_PARENT):
        assert npub not in ics
    assert "npub1" not in ics and token not in ics


async def test_a_task_falls_on_its_due_day_where_its_people_live(anyone, queue, wallets):
    # 23:59:59 on 20 November in New York is already the 21st in UTC.
    from datetime import datetime
    from zoneinfo import ZoneInfo
    due = int(datetime(2030, 11, 20, 23, 59, 59, tzinfo=ZoneInfo(ZONE)).timestamp())
    await post(needed_by=due)
    here = (await feed_of(CHILD))["feed"]["token"]
    assert "DTSTART;VALUE=DATE:20301120" in (await call("calendar_fetch", token=here))["ics"]
    utc = (await feed_of(CHILD, time_zone="UTC"))["feed"]["token"]
    assert "DTSTART;VALUE=DATE:20301121" in (await call("calendar_fetch", token=utc))["ics"]


async def test_a_feed_follows_the_tasks_as_they_change(anyone, queue, wallets):
    task = await post()
    token = (await feed_of(CHILD))["feed"]["token"]
    assert "SUMMARY:Clean your room" in (await call("calendar_fetch", token=token))["ics"]
    await owed(task)
    ics = (await call("calendar_fetch", token=token))["ics"]
    assert "SUMMARY:✓ Clean your room" in ics and "Earned 800 sats." in ics
    gone = await post(title="Rake the leaves")
    await call("withdraw_task", task_id=gone, npub=PARENT)
    assert "Rake the leaves" not in (await call("calendar_fetch", token=(await feed_of(PARENT))["feed"]["token"]))["ics"]


async def test_an_events_uid_is_the_same_on_every_read(anyone, queue, wallets):
    await post()
    token = (await feed_of(CHILD))["feed"]["token"]
    uid = lambda ics: [line for line in ics.split("\r\n") if line.startswith("UID:")]
    first, again = (await call("calendar_fetch", token=token))["ics"], (await call("calendar_fetch", token=token))["ics"]
    assert uid(first) == uid(again) and len(uid(first)) == 1


@pytest.mark.parametrize("token", ["", "nope", "g" * 32, "ab" * 15, "ab" * 17, "0" * 32])
async def test_a_token_that_opens_nothing_is_refused(anyone, queue, wallets, token):
    await post()
    await feed_of(CHILD)
    refused = await call("calendar_fetch", token=token)
    assert not refused["success"] and "not a calendar feed" in refused["error"]


async def test_the_owner_sees_their_feeds_and_how_often_each_is_read(anyone, queue, wallets):
    token = (await feed_of(CHILD))["feed"]["token"]
    await feed_of(SIBLING, name="Mine")
    await call("calendar_fetch", token=token)
    await call("calendar_fetch", token=token)
    feeds = (await call("calendar_list", npub=CHILD))["feeds"]
    assert [(f["token"], f["name"], f["zone"], f["fetches"]) for f in feeds] == [(token, "Sam's chores", ZONE, 2)]
    assert feeds[0]["last_fetch"] is not None


async def test_a_revoked_feed_opens_nothing_and_only_its_owner_revokes_it(anyone, queue, wallets):
    await post()
    token = (await feed_of(CHILD))["feed"]["token"]
    assert not (await call("calendar_revoke", token=token, npub=SIBLING))["success"]
    assert (await call("calendar_fetch", token=token))["ok"]
    assert (await call("calendar_revoke", token=token, npub=CHILD))["ok"]
    assert not (await call("calendar_fetch", token=token))["success"]
    assert (await call("calendar_list", npub=CHILD))["feeds"] == []
    assert not (await call("calendar_revoke", token=token, npub=CHILD))["success"]


async def test_the_token_is_stored_sealed_and_looked_up_by_its_hash(anyone, queue, wallets):
    token = (await feed_of(CHILD))["feed"]["token"]
    rows = await queue._rows(f"SELECT token_key, data_enc FROM {queue._t('taskbooth_feeds')}", [])
    assert len(rows) == 1 and token not in rows[0]["token_key"] and token not in rows[0]["data_enc"]


@pytest.mark.parametrize("over", [{"time_zone": "Mars/Olympus"}, {"time_zone": ""}, {"name": ""}, {"name": "x" * 81}])
async def test_a_feed_needs_a_name_and_a_real_time_zone(anyone, queue, wallets, over):
    assert not (await feed_of(CHILD, **over))["success"]
    assert (await call("calendar_list", npub=CHILD))["feeds"] == []


async def test_a_person_keeps_a_handful_of_feeds_at_most(anyone, queue, wallets):
    for i in range(5):
        assert (await feed_of(CHILD, name=f"Feed {i}"))["ok"]
    refused = await feed_of(CHILD, name="One more")
    assert not refused["success"] and "revoke one first" in refused["error"]
