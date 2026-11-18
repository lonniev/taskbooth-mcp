# ruff: noqa: F811 — the fixtures are imported, then named as test arguments
"""A job's Balance in escrow at a Cashu mint, and the arbiter who holds the third key."""

import hashlib

import pytest
from pynostr.key import PrivateKey

from taskbooth_mcp import escrow
from taskbooth_mcp.lifecycle import Action, Role
from taskbooth_mcp.store import NotYours, checked_cover
from tests.fake_mint import MINT_URL, FakeMint, sign_digest
from tests.invoices import make_invoice
from tests.signing import agree
from tests.test_store import DAY, NOW, RUBRIC, _post, queue  # noqa: F401 — fixtures

SPEC_HASH = hashlib.sha256(b"A portrait of Murray Rothbard for the new Library").hexdigest()
MINTS = (MINT_URL,)
#: Top grade 2000; deposit 10% and one 20% milestone leave 1400 to hold.
BALANCE = 1400


@pytest.fixture
def keys():
    return {role: PrivateKey() for role in ("parent", "child", "sibling", "other_parent", "arbiter", "stranger")}


@pytest.fixture
def family(keys):
    return {role: key.public_key.bech32() for role, key in keys.items()}


@pytest.fixture
def mint():
    return FakeMint(now=NOW)


def cover(family, **over):
    return {"spec_hash": SPEC_HASH, "deemed_grade": "Tidy", "deemed_acceptance_days": 3,
            "arbiter_npub": family["arbiter"], "escrow": True, "escrow_mint": MINT_URL, "refund_days": 7} | over


async def _job(db, family, **over):
    return await _post(db, family, cover=cover(family), deposit_pct=10, escrow_mints=MINTS,
                       milestones=[{"name": "Sketch", "due_at": NOW + DAY, "pct": 20}], **over)


async def _claimed(db, keys, family):
    """Posted, signed by the purchaser and arbiter, claimed and signed by the performer."""
    task = await _job(db, family)
    await agree(db, keys["parent"], task)
    await agree(db, keys["arbiter"], task)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    await agree(db, keys["child"], task)
    return task


async def _funded(db, keys, family, mint):
    task = await _claimed(db, keys, family)
    bill = await escrow.open_invoice(db, lambda url: mint, family["parent"], task, NOW + 20)
    mint.pay(list(mint.quotes)[-1])
    await escrow.sign(db, lambda url: mint, family["parent"], task, sign_digest(keys["parent"], bill["sign_digest"]),
                      NOW + 30)
    return task


async def _invoice_for(address, sats):
    return make_invoice(sats, created_at=NOW)


def _sign_pending(keys, role_key, view):
    return sign_digest(keys[role_key], view["sign_digest"])


# ---------------------------------------------------------------------------
# The cover page and the arbiter
# ---------------------------------------------------------------------------


def test_escrow_needs_an_arbiter_and_an_offered_mint(family):
    page = checked_cover(cover(family), RUBRIC, purchaser=family["parent"], mints=MINTS)
    assert page["escrow"] and page["escrow_mint"] == MINT_URL and page["refund_days"] == 7
    with pytest.raises(ValueError, match="needs a named arbiter"):
        checked_cover(cover(family, arbiter_npub=""), RUBRIC, purchaser=family["parent"], mints=MINTS)
    with pytest.raises(ValueError, match="offers no escrow mint"):
        checked_cover(cover(family), RUBRIC, purchaser=family["parent"], mints=())
    with pytest.raises(ValueError, match="one this Operator offers"):
        checked_cover(cover(family, escrow_mint="https://other.example"), RUBRIC, purchaser=family["parent"],
                      mints=MINTS)
    with pytest.raises(ValueError, match="https"):
        checked_cover(cover(family, escrow_mint="http://mint.example"), RUBRIC, purchaser=family["parent"],
                      mints=MINTS)
    with pytest.raises(ValueError, match="other than the purchaser"):
        checked_cover(cover(family, arbiter_npub=family["parent"]), RUBRIC, purchaser=family["parent"], mints=MINTS)
    with pytest.raises(ValueError, match="refund period"):
        checked_cover(cover(family, refund_days=0), RUBRIC, purchaser=family["parent"], mints=MINTS)
    # An arbiter alone, with no escrow, is a job paid by Lightning with someone to decide appeals.
    plain = checked_cover(cover(family, escrow=False, escrow_mint="", refund_days=0), RUBRIC,
                          purchaser=family["parent"], mints=())
    assert plain["arbiter_npub"] == family["arbiter"] and not plain["escrow"]


async def test_the_arbiter_decides_appeals_signs_the_offer_and_sees_the_job(queue, family, keys):
    _, db = queue
    task = await _job(db, family)
    seen = await db.get(family["arbiter"], task)
    assert seen["tie_breaker"] == family["arbiter"] and "arbiter" in seen["your_roles"]
    assert "named_tie_breaker" not in seen
    offer = await db.offer(family["arbiter"], task)
    assert f"Arbiter: {family['arbiter']}" in offer["statement"]
    assert f"Escrow: the Balance held at {MINT_URL}" in offer["statement"]
    signed = await agree(db, keys["arbiter"], task)
    assert signed["agreement"]["arbiter_signed"]
    await agree(db, keys["parent"], task)
    with pytest.raises(ValueError, match="Only the audience"):
        await db.act(family["arbiter"], task, Action.CLAIM, at=NOW + 10)
    # Even named in the audience, the arbiter cannot take the work on.
    both = await _job(db, family, audience=[family["child"], family["arbiter"]])
    await agree(db, keys["parent"], both)
    with pytest.raises(ValueError, match="arbiter cannot also do the work"):
        await db.act(family["arbiter"], both, Action.CLAIM, at=NOW + 10)


async def test_done_waits_for_the_arbiters_signature_and_the_funded_escrow(queue, family, keys, mint):
    _, db = queue
    task = await _job(db, family)
    await agree(db, keys["parent"], task)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    await agree(db, keys["child"], task)
    with pytest.raises(ValueError, match="arbiter has not signed"):
        await db.act(family["child"], task, Action.MARK_DONE, at=NOW + 20)
    await agree(db, keys["arbiter"], task)
    with pytest.raises(ValueError, match="not in escrow yet"):
        await db.act(family["child"], task, Action.MARK_DONE, at=NOW + 20)


# ---------------------------------------------------------------------------
# Lightning in
# ---------------------------------------------------------------------------


async def test_the_purchaser_funds_the_balance_by_invoice_and_signature(queue, family, keys, mint):
    _, db = queue
    posted = await _job(db, family)
    assert (await escrow.status(db, family["parent"], posted, NOW))["sats"] == BALANCE  # known before the claim
    task = await _claimed(db, keys, family)
    with pytest.raises(ValueError, match="Only the purchaser"):
        await escrow.open_invoice(db, lambda url: mint, family["child"], task, NOW + 20)
    bill = await escrow.open_invoice(db, lambda url: mint, family["parent"], task, NOW + 20)
    assert bill["sats"] == BALANCE and bill["mint"] == MINT_URL and bill["refund_at"] == NOW + 2 * DAY + 7 * DAY
    again = await escrow.open_invoice(db, lambda url: mint, family["parent"], task, NOW + 25)
    assert again["invoice"] == bill["invoice"]  # the same invoice until it expires
    quote = next(iter(mint.quotes))

    with pytest.raises(ValueError, match="not paid yet"):
        await escrow.sign(db, lambda url: mint, family["parent"], task,
                          sign_digest(keys["parent"], bill["sign_digest"]), NOW + 30)
    mint.pay(quote)
    with pytest.raises(ValueError, match="not over this sign_digest by your Nostr key"):
        await escrow.sign(db, lambda url: mint, family["parent"], task,
                          sign_digest(keys["child"], bill["sign_digest"]), NOW + 30)
    with pytest.raises(ValueError, match="Nothing in this job's escrow"):
        await escrow.sign(db, lambda url: mint, family["child"], task,
                          sign_digest(keys["child"], bill["sign_digest"]), NOW + 30)
    done = await escrow.sign(db, lambda url: mint, family["parent"], task,
                             sign_digest(keys["parent"], bill["sign_digest"]), NOW + 30)
    assert done["funded"] and done["escrow"]["held_sats"] == BALANCE
    view = await db.get(family["arbiter"], task)
    assert view["escrow"]["state"] == "funded"
    assert "proofs" not in view["escrow"] and "funding" not in view["escrow"]  # nothing secret leaves
    await db.act(family["child"], task, Action.MARK_DONE, at=NOW + 40)


async def test_escrow_waits_for_every_signature_on_the_offer(queue, family, keys, mint):
    _, db = queue
    task = await _job(db, family)
    await agree(db, keys["parent"], task)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    with pytest.raises(ValueError, match="still to sign: performer, arbiter"):
        await escrow.open_invoice(db, lambda url: mint, family["parent"], task, NOW + 20)


async def test_a_mint_without_the_escrow_nuts_is_refused(queue, family, keys, mint):
    _, db = queue
    task = await _claimed(db, keys, family)
    mint.info_nuts.pop("11")
    with pytest.raises(ValueError, match="NUT 11"):
        await escrow.open_invoice(db, lambda url: mint, family["parent"], task, NOW + 20)


# ---------------------------------------------------------------------------
# A split, and Lightning out
# ---------------------------------------------------------------------------


async def test_the_award_is_split_by_two_and_each_collects_to_their_address(queue, family, keys, mint):
    _, db = queue
    task = await _funded(db, keys, family, mint)
    await db.act(family["child"], task, Action.MARK_DONE, at=NOW + 40)
    await db.act(family["parent"], task, Action.ASSESS, grade="Spotless", at=NOW + 50)  # top grade: final
    owed = await db.get(family["child"], task)
    # The deposit and milestone were never confirmed paid, so the award is the whole top grade;
    # the escrow pays what it holds, and the rest is owed by Lightning (terms 6.16, 7).
    assert owed["state"] == "owed" and owed["assessment"]["sats"] == 2000

    mint_for = lambda url: mint
    proposal = await escrow.propose(db, family["parent"], task, None, NOW + 60)
    net = BALANCE - 1  # the mint's fee to move the escrow's proofs at 100 ppk
    assert proposal["proposal"] | {"at": 0} == {"to_performer": net, "to_purchaser": 0, "by": "purchaser", "at": 0,
                                                 "signed_by": []}
    first = await escrow.sign(db, mint_for, family["parent"], task, _sign_pending(keys, "parent", proposal), NOW + 70)
    assert not first["split"] and first["escrow"]["proposal"]["signed_by"] == ["purchaser"]
    second = await escrow.sign(db, mint_for, family["child"], task, _sign_pending(keys, "child", proposal), NOW + 80)
    assert second["split"] and second["escrow"]["state"] == "split"
    to_collect = second["escrow"]["to_collect"]
    assert sum(to_collect.values()) == net

    with pytest.raises(ValueError, match="payout_address"):
        await escrow.collect(db, mint_for, _invoice_for, family["child"], task, "", NOW + 90)
    ready = await escrow.collect(db, mint_for, _invoice_for, family["child"], task, "kid@wallet.example", NOW + 90)
    assert ready["sats"] + ready["fee_reserve"] <= to_collect["performer"] - 1
    with pytest.raises(ValueError, match="not over this sign_digest"):
        await escrow.sign(db, mint_for, family["child"], task, _sign_pending(keys, "parent", ready), NOW + 95)
    paid = await escrow.sign(db, mint_for, family["child"], task, _sign_pending(keys, "child", ready), NOW + 100)
    assert paid["paid"] == ready["sats"] and paid["to"] == "kid@wallet.example"
    assert mint.paid_invoices[-1][1] == ready["sats"]
    assert (await db.get(family["child"], task))["state"] == "paid"
    # The unused fee reserve came back, locked to the performer, to collect later.
    assert paid["escrow"]["to_collect"]["performer"] == ready["fee_reserve"]


async def test_one_signature_moves_nothing_and_a_new_proposal_starts_over(queue, family, keys, mint):
    _, db = queue
    task = await _funded(db, keys, family, mint)
    mint_for = lambda url: mint
    with pytest.raises(ValueError, match="no final grade"):
        await escrow.propose(db, family["child"], task, None, NOW + 60)
    one = await escrow.propose(db, family["child"], task, 900, NOW + 60)
    await escrow.sign(db, mint_for, family["child"], task, _sign_pending(keys, "child", one), NOW + 61)
    other = await escrow.propose(db, family["parent"], task, 300, NOW + 62)
    status = await escrow.status(db, family["child"], task, NOW + 63)
    assert status["escrow"]["proposal"]["signed_by"] == [] and status["escrow"]["proposal"]["to_performer"] == 300
    assert [p["what"] for p in status["for_you"]] == ["proposal"]
    # The old proposal's signature is no good on the new one.
    with pytest.raises(ValueError, match="not over this sign_digest"):
        await escrow.sign(db, mint_for, family["child"], task, _sign_pending(keys, "child", one), NOW + 64)
    assert not mint.spent - {p for p in mint.spent if not p.startswith("[")}  # nothing locked was spent
    await escrow.sign(db, mint_for, family["parent"], task, _sign_pending(keys, "parent", other), NOW + 65)
    with pytest.raises(NotYours):
        await escrow.propose(db, family["other_parent"], task, 1, NOW + 66)


async def test_the_arbiter_rules_with_either_party(queue, family, keys, mint):
    _, db = queue
    task = await _funded(db, keys, family, mint)
    mint_for = lambda url: mint
    ruling = await escrow.propose(db, family["arbiter"], task, 500, NOW + 60)
    await escrow.sign(db, mint_for, family["arbiter"], task, _sign_pending(keys, "arbiter", ruling), NOW + 61)
    done = await escrow.sign(db, mint_for, family["parent"], task, _sign_pending(keys, "parent", ruling), NOW + 62)
    assert done["split"] and done["escrow"]["to_collect"]["performer"] == 500
    assert done["escrow"]["proposal"]["signed_by"] == ["arbiter", "purchaser"]


async def test_after_the_refund_date_the_purchaser_alone_takes_it_back(queue, family, keys, mint):
    _, db = queue
    task = await _funded(db, keys, family, mint)
    mint_for = lambda url: mint
    refund_at = NOW + 2 * DAY + 7 * DAY
    back = await escrow.propose(db, family["parent"], task, 0, NOW + 60)
    early = await escrow.sign(db, mint_for, family["parent"], task, _sign_pending(keys, "parent", back), NOW + 61)
    assert not early["split"]  # before the date one signature is not enough
    # After the date, a fresh proposal signed by the purchaser alone goes through, and the mint agrees.
    mint.now = refund_at
    back = await escrow.propose(db, family["parent"], task, 0, refund_at)
    done = await escrow.sign(db, mint_for, family["parent"], task, _sign_pending(keys, "parent", back), refund_at)
    assert done["split"] and set(done["escrow"]["to_collect"]) == {"purchaser"}
    # A share for the performer still needs two.
    task2 = await _funded(db, keys, family, mint)
    half = await escrow.propose(db, family["parent"], task2, 100, refund_at)
    alone = await escrow.sign(db, mint_for, family["parent"], task2, _sign_pending(keys, "parent", half), refund_at)
    assert not alone["split"]


async def test_a_funded_escrow_must_be_settled_before_the_claim_is_released(queue, family, keys, mint):
    _, db = queue
    task = await _funded(db, keys, family, mint)
    with pytest.raises(ValueError, match="Settle it first"):
        await db.act(family["parent"], task, Action.RELEASE, at=NOW + 60)


async def test_an_unpaid_invoice_goes_with_a_released_claim(queue, family, keys, mint):
    _, db = queue
    task = await _claimed(db, keys, family)
    await escrow.open_invoice(db, lambda url: mint, family["parent"], task, NOW + 20)
    released = await db.act(family["parent"], task, Action.RELEASE, at=NOW + 30)
    assert "escrow" not in released


async def test_only_the_people_on_the_job_reach_its_escrow(queue, family, keys, mint):
    _, db = queue
    task = await _funded(db, keys, family, mint)
    with pytest.raises(NotYours):
        await escrow.status(db, family["stranger"], task, NOW + 60)
    seen = await escrow.status(db, family["arbiter"], task, NOW + 60)
    assert seen["escrow"]["state"] == "funded" and seen["for_you"] == []
    assert Role.ARBITER in db._roles(await db._load(task), family["arbiter"])
