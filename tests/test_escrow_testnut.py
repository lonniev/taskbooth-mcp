# ruff: noqa: F811 — the fixtures are imported, then named as test arguments
"""The whole escrow against testnut.cashu.space, a public mint of pretend sats.

Run with ``pytest -m testnut``. The mint pays its own invoices, so each party's
"Lightning wallet" here is an invoice from the same mint, and a paid one turns
PAID there.
"""

import asyncio
import time

import pytest
from tollbooth import cashu

from taskbooth_mcp import escrow
from tests.fake_mint import sign_digest
from tests.test_escrow import _claimed, family, keys  # noqa: F401 — fixtures
from tests.test_store import queue  # noqa: F401 — fixture

pytestmark = pytest.mark.testnut
TESTNUT = "https://testnut.cashu.space"


async def _paid(client, quote, seconds=30):
    for _ in range(seconds):
        if (await client.mint_quote_state(quote))["state"] in ("PAID", "ISSUED"):
            return True
        await asyncio.sleep(1)
    return False


async def test_escrow_in_and_out_on_testnut(queue, family, keys, monkeypatch):
    _, db = queue
    client = cashu.MintClient(TESTNUT)
    monkeypatch.setattr("tests.test_escrow.MINT_URL", TESTNUT)
    monkeypatch.setattr("tests.test_escrow.MINTS", (TESTNUT,))
    task = await _claimed(db, keys, family)
    mint_for = lambda url: client
    now = int(time.time())

    bill = await escrow.open_invoice(db, mint_for, family["parent"], task, now)
    record = (await db._load(task))["escrow"]
    assert await _paid(client, record["funding"]["quote"])  # testnut pays its own invoice
    funded = await escrow.sign(db, mint_for, family["parent"], task, sign_digest(keys["parent"], bill["sign_digest"]),
                               now)
    assert funded["funded"] and funded["escrow"]["held_sats"] == bill["sats"]

    ruling = await escrow.propose(db, family["arbiter"], task, 700, now)
    await escrow.sign(db, mint_for, family["arbiter"], task, sign_digest(keys["arbiter"], ruling["sign_digest"]), now)
    split = await escrow.sign(db, mint_for, family["child"], task, sign_digest(keys["child"], ruling["sign_digest"]), now)
    assert split["split"] and split["escrow"]["to_collect"]["performer"] == 700

    wallets = {}

    async def invoice_for(address, sats):
        quote = await client.mint_quote(sats, escrow.lock_key(family["sibling"]))
        wallets[address] = quote["quote"]
        return quote["request"]

    for who, role_key in (("child", "child"), ("parent", "parent")):
        ready = await escrow.collect(db, mint_for, invoice_for, family[who], task, f"{who}@wallet.example", now)
        paid = await escrow.sign(db, mint_for, family[who], task, sign_digest(keys[role_key], ready["sign_digest"]),
                                 now)
        assert paid["paid"] == ready["sats"] > 0
        assert await _paid(client, wallets[f"{who}@wallet.example"], seconds=10)
