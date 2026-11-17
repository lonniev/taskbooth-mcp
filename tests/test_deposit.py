# ruff: noqa: F811 — the fixtures are imported, then named as test arguments
"""A deposit on claim: asked for, invoiced, confirmed, and netted from the award."""

import pytest
from tollbooth import bolt11

from tests.invoices import make_invoice
from tests.test_tools import (  # noqa: F401 — fixtures
    CHILD,
    OTHER_PARENT,
    PARENT,
    SIBLING,
    STRANGER,
    billing,
    call,
    post,
    queue,
    wallets,
)


async def claimed(**over):
    task = await post(deposit_pct=25, **over)
    assert (await call("claim_task", task_id=task, npub=CHILD))["ok"]
    return task


async def test_a_task_without_a_deposit_asks_for_none(billing, queue, wallets):
    task = await post()
    seen = (await call("claim_task", task_id=task, npub=CHILD))["task"]
    assert seen["deposit_pct"] == 0 and "deposit" not in seen
    refused = await call("request_deposit", task_id=task, npub=PARENT)
    assert not refused["success"] and "no deposit" in refused["error"]


async def test_a_claim_asks_for_its_share_of_the_top_grade(billing, queue, wallets):
    task = await claimed()
    seen = (await call("get_task", task_id=task, npub=PARENT))["task"]
    assert seen["deposit"] == {"pct": 25, "sats": 500}  # 25% of Spotless's 2000


async def test_the_deposit_invoice_is_exact_and_its_hash_is_kept_on_the_task(billing, queue, wallets):
    task = await claimed()
    made = (await call("request_deposit", task_id=task, npub=PARENT))["deposit"]
    assert made["sats"] == 500 and bolt11.decode(made["invoice"]).amount_msat == 500_000
    assert wallets["asked"] == [("kid@wallet.example", 500)]
    kept = (await call("get_task", task_id=task, npub=CHILD))["task"]["deposit"]
    assert kept["payment_hash"] == made["payment_hash"] and "address" not in kept
    again = (await call("request_deposit", task_id=task, npub=CHILD))["deposit"]
    assert again["invoice"] == made["invoice"] and len(wallets["asked"]) == 1
    history = (await call("task_history", task_id=task, npub=PARENT))["events"]
    assert history[-1]["action"] == "deposit_invoice" and history[-1]["payment_hash"] == made["payment_hash"]


async def test_a_performer_without_an_address_hands_in_their_own_invoice(billing, queue, wallets):
    wallets["addresses"].pop(CHILD)
    task = await claimed()
    told = await call("request_deposit", task_id=task, npub=PARENT)
    assert not told["success"] and "500 sats" in told["error"]
    wrong = await call("request_deposit", task_id=task, invoice=make_invoice(600), npub=CHILD)
    assert not wrong["success"] and "different amount" in wrong["error"]
    theirs = await call("request_deposit", task_id=task, invoice=make_invoice(500), npub=PARENT)
    assert not theirs["success"] and "Only the performer" in theirs["error"]
    mine = (await call("request_deposit", task_id=task, invoice=make_invoice(500), npub=CHILD))["deposit"]
    assert mine["sats"] == 500


@pytest.mark.parametrize("who", [SIBLING, STRANGER])
async def test_only_the_purchaser_and_performer_ask_for_the_deposit(billing, queue, wallets, who):
    task = await claimed()
    assert not (await call("request_deposit", task_id=task, npub=who))["success"]


async def test_only_the_performer_confirms_and_only_an_invoiced_deposit(billing, queue, wallets):
    task = await claimed()
    early = await call("confirm_deposit", task_id=task, npub=CHILD)
    assert not early["success"] and "No deposit invoice" in early["error"]
    await call("request_deposit", task_id=task, npub=PARENT)
    assert not (await call("confirm_deposit", task_id=task, npub=PARENT))["success"]
    paid = (await call("confirm_deposit", task_id=task, npub=CHILD))["task"]
    assert paid["deposit"]["paid_at"] and paid["state"] == "claimed"
    twice = await call("confirm_deposit", task_id=task, npub=CHILD)
    assert not twice["success"] and "already paid" in twice["error"]
    asked = await call("request_deposit", task_id=task, npub=PARENT)
    assert not asked["success"] and "already paid" in asked["error"]


async def test_the_award_pays_only_what_the_grade_earns_beyond_a_paid_deposit(billing, queue, wallets):
    task = await claimed()
    await call("request_deposit", task_id=task, npub=PARENT)
    await call("confirm_deposit", task_id=task, npub=CHILD)
    await call("mark_done", task_id=task, npub=CHILD)
    graded = (await call("assess_task", task_id=task, grade="Spotless", npub=PARENT))["task"]
    assert graded["state"] == "owed"
    assert graded["assessment"] | {"sats": 1500, "earned_sats": 2000, "deposit_sats": 500} == graded["assessment"]
    due = (await call("request_payout", task_id=task, npub=PARENT))["payout"]
    assert due["sats"] == 1500 and bolt11.decode(due["invoice"]).amount_msat == 1_500_000


async def test_a_grade_worth_less_than_the_deposit_leaves_nothing_owed(billing, queue, wallets):
    task = await claimed()
    await call("request_deposit", task_id=task, npub=PARENT)
    await call("confirm_deposit", task_id=task, npub=CHILD)
    await call("mark_done", task_id=task, npub=CHILD)
    await call("assess_task", task_id=task, grade="Not done", npub=PARENT)
    settled = (await call("accept_assessment", task_id=task, npub=CHILD))["task"]
    assert (settled["state"], settled["final"], settled["assessment"]["sats"]) == ("assessed", True, 0)


async def test_an_unpaid_deposit_leaves_the_award_whole(billing, queue, wallets):
    task = await claimed()
    await call("request_deposit", task_id=task, npub=PARENT)
    await call("mark_done", task_id=task, npub=CHILD)
    graded = (await call("assess_task", task_id=task, grade="Spotless", npub=PARENT))["task"]
    assert graded["assessment"]["sats"] == 2000 and "deposit_sats" not in graded["assessment"]


async def test_a_deposit_confirmed_after_the_work_is_done_still_counts(billing, queue, wallets):
    task = await claimed()
    await call("request_deposit", task_id=task, npub=PARENT)
    await call("mark_done", task_id=task, npub=CHILD)
    assert (await call("confirm_deposit", task_id=task, npub=CHILD))["ok"]
    graded = (await call("assess_task", task_id=task, grade="Tidy", npub=PARENT))["task"]
    assert graded["assessment"]["sats"] == 300  # Tidy's 800 less the 500 already paid


async def test_an_appeal_decision_nets_the_deposit_from_the_new_grade(billing, queue, wallets):
    task = await claimed()
    await call("request_deposit", task_id=task, npub=PARENT)
    await call("confirm_deposit", task_id=task, npub=CHILD)
    await call("mark_done", task_id=task, npub=CHILD)
    await call("assess_task", task_id=task, grade="Tidy", npub=PARENT)
    await call("appeal_assessment", task_id=task, reasons="It was spotless", npub=CHILD)
    decided = (await call("decide_appeal", task_id=task, grade="Spotless", npub=OTHER_PARENT))["task"]
    assert decided["assessment"]["sats"] == 1500 and decided["assessment"]["appeal"]["first_sats"] == 300


async def test_releasing_a_claim_clears_the_deposit_and_notes_one_already_paid(billing, queue, wallets):
    task = await claimed()
    await call("request_deposit", task_id=task, npub=PARENT)
    await call("confirm_deposit", task_id=task, npub=CHILD)
    back = (await call("release_claim", task_id=task, npub=PARENT))["task"]
    assert back["state"] == "posted" and "deposit" not in back
    last = (await call("task_history", task_id=task, npub=PARENT))["events"][-1]
    assert last["action"] == "release" and last["deposit_kept_by_performer"] == 500
    again = (await call("claim_task", task_id=task, npub=SIBLING))["task"]
    assert again["deposit"] == {"pct": 25, "sats": 500}


async def test_the_deposit_share_is_checked_and_editable_until_claimed(billing, queue, wallets):
    for bad in (-1, 91):
        refused = await call("post_task", title="x", rubric={"grades": [{"name": "A", "sats": 10}]},
                             audience=[CHILD], needed_by=2**40, deposit_pct=bad, npub=PARENT)
        assert not refused.get("ok"), bad
    task = await post()
    edited = (await call("edit_task", task_id=task, deposit_pct=40, npub=PARENT))["task"]
    assert edited["deposit_pct"] == 40
    await call("claim_task", task_id=task, npub=CHILD)
    assert not (await call("edit_task", task_id=task, deposit_pct=10, npub=PARENT))["success"]
