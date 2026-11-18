# ruff: noqa: F811 — the fixtures are imported, then named as test arguments
"""Escrow as a caller meets it: the mints the owner offers, and the five escrow tools."""

import time

from pynostr.key import PrivateKey

from taskbooth_mcp import server
from tests.fake_mint import MINT_URL, FakeMint
from tests.test_tools import PARENT, RUBRIC, billing, call, queue  # noqa: F401 — fixtures

ARBITER = PrivateKey().public_key.bech32()
DAY = 86_400


def _offer(monkeypatch, mints: str | None) -> FakeMint:
    async def operator_property(name):
        return mints if name == "escrow_mints" else None

    mint = FakeMint(now=int(time.time()))
    monkeypatch.setattr(server.runtime, "operator_property", operator_property)
    monkeypatch.setattr(server, "_mint_for", lambda url: mint)
    return mint


def _cover(**over):
    return {"arbiter_npub": ARBITER, "escrow": True, "escrow_mint": MINT_URL, "refund_days": 7} | over


async def _post(**over):
    args = {"title": "A portrait of Murray Rothbard", "rubric": RUBRIC, "audience": [PrivateKey().public_key.bech32()],
            "needed_by": int(time.time()) + 2 * DAY, "cover": _cover(), "npub": PARENT} | over
    return await call("post_task", **args)


async def test_the_owner_lists_the_mints_and_a_bad_address_is_skipped(monkeypatch):
    _offer(monkeypatch, f"{MINT_URL}  http://plain.example, https://10.0.0.9 https://second.example/")
    assert await server.escrow_mints() == (MINT_URL, "https://second.example")
    _offer(monkeypatch, None)
    assert await server.escrow_mints() == ()


async def test_a_job_in_escrow_posts_only_with_an_offered_mint(billing, queue, monkeypatch):
    _offer(monkeypatch, "")
    refused = await _post()
    assert not refused["success"] and "offers no escrow mint" in refused["error"]
    assert billing["rollback"] == 1  # a refusal costs nothing

    _offer(monkeypatch, MINT_URL)
    posted = await _post()
    task = posted["task"]
    assert task["cover"]["escrow"] and task["tie_breaker"] == ARBITER
    status = await call("escrow_status", task_id=task["task_id"], npub=PARENT)
    assert status["escrow"] is None and status["offered"] and status["sats"] == 2000  # no deposit, no milestones

    early = await call("escrow_invoice", task_id=task["task_id"], npub=PARENT)
    assert not early["success"] and "claimed" in early["error"]
    nothing = await call("escrow_sign", task_id=task["task_id"], signature="00" * 64, npub=PARENT)
    assert not nothing["success"] and "has no escrow" in nothing["error"]
    proposal = await call("escrow_propose", task_id=task["task_id"], to_performer=5, npub=PARENT)
    assert not proposal["success"] and "no funded escrow" in proposal["error"]


async def test_the_escrow_tools_are_registered_with_their_own_ids():
    names = {t.capability for t in server._DOMAIN_TOOLS}
    assert {"escrow_invoice", "escrow_status", "escrow_propose", "escrow_collect", "escrow_sign"} <= names
    ids = [t.tool_id for t in server._DOMAIN_TOOLS]
    assert len(ids) == len(set(ids))
