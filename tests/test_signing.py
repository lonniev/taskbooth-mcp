# ruff: noqa: F811 — the fixtures are imported, then named as test arguments
"""A job under the Standard Terms is a contract once both parties sign one offer."""

import pytest
from pynostr.key import PrivateKey

from taskbooth_mcp import server
from taskbooth_mcp.agreement import offer_hash, offer_statement
from taskbooth_mcp.lifecycle import Action
from tests.signing import OPERATOR, OPERATOR_NPUB, agree, dm_signature
from tests.test_store import DAY, NOW, _post, queue  # noqa: F401 — fixtures
from tests.test_tools import billing, call  # noqa: F401 — fixtures
from tests.test_tools import queue as tool_queue  # noqa: F401 — fixture under its own name

COVER = {"deliverables": ["fr.ts"], "spec_hash": "ab" * 32}


@pytest.fixture
def keys():
    return {role: PrivateKey() for role in ("parent", "child", "sibling", "other_parent")}


@pytest.fixture
def family(keys):
    return {role: key.public_key.bech32() for role, key in keys.items()}


async def _job(db, family, **over):
    return await _post(db, family, cover=COVER, deposit_pct=10,
                       milestones=[{"name": "Draft", "due_at": NOW + DAY, "pct": 20}], **over)


# ---------------------------------------------------------------------------
# The offer
# ---------------------------------------------------------------------------


async def test_the_offer_states_every_term_and_nothing_about_who_claims_it(queue, family, keys):
    _, db = queue
    task = await _job(db, family)
    read = await db.offer(family["child"], task)
    text = read["statement"]
    for line in (f"Task Booth offer {task}", f"Purchaser: {family['parent']}", "Grades: Spotless 2000 sats",
                 "Deposit: 10%", "Milestone: Draft, 20%", "Standard Terms: v1", "Spec sha256: " + "ab" * 32):
        assert line in text, line
    assert family["child"] not in text
    assert read["statement_hash"] == offer_hash(await db._load(task)) and read["signatures"] == {}
    # The same terms always read the same, before and after a claim.
    await agree(db, keys["parent"], task)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    assert offer_statement(await db._load(task)) == text


async def test_an_edit_changes_the_offer_and_clears_the_purchasers_signature(queue, family, keys):
    _, db = queue
    task = await _job(db, family)
    signed = await agree(db, keys["parent"], task)
    assert signed["agreement"] == {"purchaser_signed": True, "performer_signed": False, "arbiter_signed": False}
    before = (await db.offer(family["parent"], task))["statement_hash"]
    edited = await db.edit(family["parent"], task, {"deposit_pct": 5}, at=NOW + 5)
    assert edited["agreement"]["purchaser_signed"] is False
    assert (await db.offer(family["parent"], task))["statement_hash"] != before


# ---------------------------------------------------------------------------
# No contract, no work
# ---------------------------------------------------------------------------


async def test_nobody_claims_an_offer_its_purchaser_has_not_signed(queue, family, keys):
    _, db = queue
    task = await _job(db, family)
    with pytest.raises(ValueError, match="not signed this offer"):
        await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    await agree(db, keys["parent"], task)
    assert (await db.act(family["child"], task, Action.CLAIM, at=NOW + 10))["state"] == "claimed"


async def test_the_performer_signs_before_marking_the_work_done(queue, family, keys):
    _, db = queue
    task = await _job(db, family)
    await agree(db, keys["parent"], task)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    with pytest.raises(ValueError, match="Sign the offer first"):
        await db.act(family["child"], task, Action.MARK_DONE, at=NOW + 20)
    both = await agree(db, keys["child"], task)
    assert both["agreement"] == {"purchaser_signed": True, "performer_signed": True, "arbiter_signed": False}
    assert (await db.act(family["child"], task, Action.MARK_DONE, at=NOW + 20))["state"] == "done"
    kept = (await db.offer(family["parent"], task))["signatures"]
    assert kept["purchaser"]["signer"] == family["parent"] and kept["performer"]["signer"] == family["child"]
    assert kept["purchaser"]["statement_hash"] == kept["performer"]["statement_hash"]


async def test_a_released_performers_signature_goes_and_the_purchasers_stays(queue, family, keys):
    _, db = queue
    task = await _job(db, family)
    await agree(db, keys["parent"], task)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    await agree(db, keys["child"], task)
    back = await db.act(family["parent"], task, Action.RELEASE, at=NOW + 20)
    assert back["agreement"] == {"purchaser_signed": True, "performer_signed": False, "arbiter_signed": False}
    await db.act(family["sibling"], task, Action.CLAIM, at=NOW + 30)
    with pytest.raises(ValueError, match="Sign the offer first"):
        await db.act(family["sibling"], task, Action.MARK_DONE, at=NOW + 40)


async def test_a_task_without_a_cover_page_needs_no_signatures(queue, family):
    _, db = queue
    task = await _post(db, family)
    await db.act(family["child"], task, Action.CLAIM, at=NOW + 10)
    assert (await db.act(family["child"], task, Action.MARK_DONE, at=NOW + 20))["state"] == "done"


# ---------------------------------------------------------------------------
# A signature that does not stand
# ---------------------------------------------------------------------------


async def test_a_signature_must_be_the_signers_over_this_offer_witnessed_here(queue, family, keys):
    _, db = queue
    task = await _job(db, family)
    digest = (await db.offer(family["parent"], task))["statement_hash"]
    parent = family["parent"]
    with pytest.raises(ValueError, match="not this signer"):
        await db.sign(parent, task, dm_signature(keys["sibling"], digest), operator_npub=OPERATOR_NPUB)
    with pytest.raises(ValueError, match="not over this statement"):
        await db.sign(parent, task, dm_signature(keys["parent"], "00" * 32), operator_npub=OPERATOR_NPUB)
    with pytest.raises(ValueError, match="not this operator"):
        await db.sign(parent, task, dm_signature(keys["parent"], digest, operator=PrivateKey()),
                      operator_npub=OPERATOR_NPUB)
    # Only the purchaser signs an open offer.
    with pytest.raises(ValueError, match="purchaser"):
        await db.sign(family["child"], task, dm_signature(keys["child"], digest), operator_npub=OPERATOR_NPUB)
    assert (await db.offer(parent, task))["signatures"] == {}


# ---------------------------------------------------------------------------
# Through the tools
# ---------------------------------------------------------------------------


async def test_a_dm_signature_is_asked_for_then_collected(billing, tool_queue, monkeypatch, keys):
    parent = keys["parent"].public_key.bech32()
    billing["proven"].add(parent)
    asked = {}

    async def request_signature(signer, statement):
        asked.update(signer=signer, statement=statement)
        from tollbooth.tools.signature import statement_hash
        return {"success": True, "nonce": "bold-hawk-42", "statement_hash": statement_hash(statement)}

    async def receive_signature(signer, nonce, digest):
        return {"success": True, "signature": dm_signature(keys["parent"], digest, OPERATOR)}

    monkeypatch.setattr(server.runtime, "request_signature", request_signature)
    monkeypatch.setattr(server.runtime, "receive_signature", receive_signature)
    monkeypatch.setattr(server.runtime, "operator_npub", lambda: OPERATOR_NPUB)

    posted = await call("post_task", title="Translate Welcome", rubric={"grades": [{"name": "Done", "sats": 900}]},
                        audience=[keys["child"].public_key.bech32()], needed_by=2**40, cover=COVER, npub=parent)
    task = posted["task"]["task_id"]
    read = await call("offer_terms", task_id=task, npub=parent)
    sent = await call("request_offer_signature", task_id=task, npub=parent)
    assert asked == {"signer": parent, "statement": read["statement"]}
    assert sent["statement_hash"] == read["statement_hash"]
    got = await call("receive_offer_signature", task_id=task, nonce=sent["nonce"],
                     statement_hash=sent["statement_hash"], npub=parent)
    assert got["task"]["agreement"]["purchaser_signed"] is True


async def test_an_unsigned_call_cannot_stand_as_a_signature(billing, tool_queue, keys):
    parent = keys["parent"].public_key.bech32()
    billing["proven"].add(parent)
    posted = await call("post_task", title="x", rubric={"grades": [{"name": "Done", "sats": 9}]},
                        audience=[parent], needed_by=2**40, cover=COVER, npub=parent)
    read = await call("offer_terms", task_id=posted["task"]["task_id"], npub=parent)
    refused = await call("sign_offer", task_id=posted["task"]["task_id"], statement_hash=read["statement_hash"],
                         npub=parent)
    assert not refused["success"] and "request_offer_signature" in refused["error"]
