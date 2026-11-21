# ruff: noqa: F811 — the fixtures are imported, then named as test arguments
"""Drafting a job from an idea: what the model said is checked, and nothing invented passes as said."""

import datetime as dt
import json

import pytest

from taskbooth_mcp import draft, price, server
from tests.test_tools import PARENT, billing, call  # noqa: F401 — fixtures

TODAY = dt.date(2026, 11, 21)


def answer(obj, *, stop="end_turn"):
    return {"model": "anthropic/claude-haiku-5.5", "stop_reason": stop,
            "content": [{"type": "text", "text": f"<draft>{json.dumps(obj)}</draft>"}]}


GOOD = {
    "said": {"title": "Walnut dining table for eight", "brief": "A walnut dining table that seats eight.",
             "deliverables": ["A walnut dining table"], "needed_by": None},
    "proposed": {"grades": [{"name": "Accepted", "amount": 4000, "criteria": "As agreed"},
                            {"name": "Not accepted", "amount": 0, "criteria": ""}],
                 "deposit_pct": 30, "milestones": [{"name": "Top glued", "pct": 30, "after_days": 30}],
                 "deemed_days": 14, "ip_option": "worker_keeps"},
    "questions": ["What size should it be?"],
    "assumptions": [],
}


def made(obj, idea="walnut dining table seats 8", kind="commission"):
    return draft.validate(json.dumps(obj), kind=kind, idea=idea, today=TODAY)


def test_a_good_answer_is_kept_whole():
    d = made(GOOD)
    assert d.said["title"] == "Walnut dining table for eight"
    assert d.said["deliverables"] == ["A walnut dining table"]
    assert d.proposed["deposit_pct"] == 30 and d.proposed["ip_option"] == "worker_keeps"
    assert d.proposed["milestones"] == [{"name": "Top glued", "pct": 30, "after_days": 30}]
    # No time in the idea: needed_by stays empty, and "when" is asked.
    assert d.said["needed_by"] is None and any("When" in q for q in d.questions)


def test_a_date_the_idea_never_named_is_dropped():
    d = made({**GOOD, "said": {**GOOD["said"], "needed_by": "2026-12-24"}}, idea="walnut table please")
    assert d.said["needed_by"] is None


def test_a_date_the_idea_did_name_is_kept():
    d = made({**GOOD, "said": {**GOOD["said"], "needed_by": "2026-12-24"}}, idea="walnut table by christmas")
    assert d.said["needed_by"] == "2026-12-24"


def test_a_date_in_the_past_or_far_off_is_dropped():
    for when in ("2025-01-01", "2031-01-01", "soon"):
        assert made({**GOOD, "said": {**GOOD["said"], "needed_by": when}}, idea="table by june").said["needed_by"] is None


def test_a_number_the_idea_never_said_is_flagged():
    said = {**GOOD["said"], "brief": "A walnut table, 96 inches long, that seats 8."}
    d = made({**GOOD, "said": said})
    assert any("96" in a for a in d.assumptions)
    assert not any("“8”" in a for a in d.assumptions)


def test_out_of_range_and_unknown_proposals_are_dropped():
    p = {**GOOD["proposed"], "deposit_pct": 95, "deemed_days": 400, "ip_option": "sell",
         "milestones": [{"name": "Half", "pct": 80, "after_days": 10}]}
    d = made({**GOOD, "proposed": p})
    assert d.proposed["deposit_pct"] is None and d.proposed["deemed_days"] is None and d.proposed["ip_option"] is None
    assert d.proposed["milestones"] == [{"name": "Half", "pct": 80, "after_days": 10}]
    over = {**GOOD["proposed"], "deposit_pct": 50, "milestones": [{"name": "Half", "pct": 50, "after_days": 10}]}
    assert made({**GOOD, "proposed": over}).proposed["milestones"] == []


def test_grades_that_are_not_a_rubric_are_dropped():
    for grades in ([{"name": "Only", "amount": 10}],
                   [{"name": "Low", "amount": 0}, {"name": "High", "amount": 10}],
                   [{"name": "Same", "amount": 10}, {"name": "same", "amount": 0}],
                   [{"name": "A", "amount": True}, {"name": "B", "amount": 0}]):
        assert made({**GOOD, "proposed": {**GOOD["proposed"], "grades": grades}}).proposed["grades"] == []


def test_a_chore_carries_no_cover_page():
    chore = {"said": {"title": "Tidy your room", "brief": "Tidy your room.", "needed_by": None, "repeat_days": 7,
                      "deliverables": ["ignored"]},
             "proposed": {"grades": [{"name": "Spotless", "amount": 2}, {"name": "Not done", "amount": 0}],
                          "deposit_pct": 20, "ip_option": "assign"},
             "questions": [], "assumptions": []}
    d = made(chore, idea="tidy room every saturday", kind="chore")
    assert d.said["repeat_days"] == 7 and "deliverables" not in d.said
    assert set(d.proposed) == {"grades"}


def test_an_unreadable_answer_refunds():
    for raw in ("not json", "[1, 2]", json.dumps({"said": {}, "proposed": {}})):
        with pytest.raises(draft.DraftError):
            draft.validate(raw, kind="commission", idea="x", today=TODAY)
    with pytest.raises(ValueError):
        draft.from_answer({"content": [], "stop_reason": "max_tokens"}, kind="commission", idea="x", today=TODAY)


def test_sats_are_tidy_figures():
    assert draft.tidy_sats(57) == 57 and draft.tidy_sats(6_249) == 6_200 and draft.tidy_sats(4_123_456) == 4_100_000
    d = draft.with_sats(made(GOOD), 80_000.0)
    assert d.proposed["grades"][0]["sats"] == 5_000_000 and d.proposed["grades"][1]["sats"] == 0


@pytest.mark.parametrize("idea", ["", "   ", "x" * 2001, "bad\x00byte"])
def test_an_idea_that_is_not_text_is_refused(idea):
    with pytest.raises(ValueError):
        draft.checked_idea(idea)


# ── The tool, as a caller meets it ────────────────────────────────────────


@pytest.fixture
def drafting(monkeypatch):
    seen: dict = {}

    async def load_credentials(names, **kw):
        return {"llm_api_key": "sk-or-test"} if seen.get("key", True) else {}

    async def operator_property(name):
        return {"draft_model": "anthropic/claude-haiku-5.5", "draft_effort": "low"}.get(name)

    async def ask(key, **kw):
        seen.update(kw, key_used=key)
        return seen.get("answer", answer(GOOD))

    async def reading(fiat, years, **kw):
        return {"fiat": fiat, "price": 80_000.0, "observed_at": 1, "history": [], "source": "mempool.space"}

    monkeypatch.setattr(server.runtime, "load_credentials", load_credentials)
    monkeypatch.setattr(server.runtime, "operator_property", operator_property)
    monkeypatch.setattr(server, "ask", ask)
    monkeypatch.setattr(price, "reading", reading)
    return seen


async def test_the_tool_drafts_with_the_operators_model_and_caches_the_prompt(billing, drafting):
    got = await call("draft_task", idea="walnut dining table seats 8", currency="usd", npub=PARENT)
    assert got["ok"] and got["said"]["title"] == "Walnut dining table for eight"
    assert got["proposed"]["grades"][0]["sats"] == 5_000_000 and got["currency"] == "USD"
    assert drafting["model"] == "anthropic/claude-haiku-5.5" and drafting["effort"] == "low"
    assert drafting["cache_system"] is True and drafting["system"] == draft.SYSTEM["commission"]
    assert "walnut dining table seats 8" in drafting["user"]
    assert billing["rollback"] == 0


async def test_no_key_is_a_refunded_refusal(billing, drafting):
    drafting["key"] = False
    got = await call("draft_task", idea="a table", npub=PARENT)
    assert not got.get("ok") and "by hand" in json.dumps(got)
    assert billing["rollback"] == 1


async def test_an_unreadable_answer_is_refunded(billing, drafting):
    drafting["answer"] = {"content": [{"type": "text", "text": "sorry"}], "stop_reason": "end_turn"}
    got = await call("draft_task", idea="a table", npub=PARENT)
    assert not got.get("ok") and billing["rollback"] == 1


async def test_a_bad_kind_or_currency_is_refused(billing, drafting):
    assert not (await call("draft_task", idea="a table", kind="poem", npub=PARENT)).get("ok")
    assert not (await call("draft_task", idea="a table", currency="XYZ", npub=PARENT)).get("ok")
