"""The signed offer says in words every term the cover page sets (peer review, 2026-11-19)."""

from __future__ import annotations

from typing import Any

import pytest

from taskbooth_mcp import terms
from taskbooth_mcp.agreement import offer_statement

NEEDED_BY = 1_800_000_000


def _task(late: dict[str, Any] | None = None, **cover: Any) -> dict[str, Any]:
    page = {
        "terms_version": terms.CURRENT, "terms_hash": terms.terms_hash(),
        "spec_url": "https://example.com/spec.pdf", "spec_hash": "ab" * 32,
        "deliverables": ["One oil portrait", "A framed print"],
        "deemed_grade": "Good", "deemed_acceptance_days": 14,
        "ip_option": "assign", "ip_custom_text": "", "confidential": True,
        "third_party_allowed": False, "governing_law": "", "venue": "",
        "fail_after_hours": 168, "appeal_hours": 168, "ruling_hours": 168, "pay_hours": 168,
        "arbiter_npub": "", "inspector_npub": "", "escrow": False, "escrow_mint": "", "refund_days": 0,
        **cover,
    }
    return {
        "task_id": "t1", "purchaser": "npub1purchaser", "title": "A portrait", "description": "",
        "needed_by": NEEDED_BY, "rubric": {"grades": [{"name": "Good", "sats": 1000}]},
        "tie_breaker": "npub1purchaser", "late": late, "deposit_pct": 10, "milestones": [], "cover": page,
    }


@pytest.mark.parametrize(("late", "said"), [
    (None, "Late: paid in full"),
    ({"kind": "full"}, "Late: paid in full"),
    ({"kind": "discount", "pct_per_day": 10}, "Late: 10% off the grade for each day late, never below zero"),
    ({"kind": "steps", "steps": [{"after_hours": 0, "pct_off": 10}, {"after_hours": 72, "pct_off": 25}]},
     "Late: late steps, the last that applies: 10% off once more than 0 hours late; 25% off once more than 72 hours late"),
    ({"kind": "cancel"}, "Late: the job ends as missed if not delivered by the needed-by time"),
])
def test_every_late_rule_is_stated_as_the_code_applies_it(late, said):
    text = offer_statement(_task(late))
    assert said in text
    if late and late["kind"] != "full":
        assert "paid in full" not in text


def test_every_cover_term_is_written_out_and_not_only_hashed():
    text = offer_statement(_task())
    for line in (
        "Spec: https://example.com/spec.pdf",
        "Deliverable 1: One oil portrait",
        "Deliverable 2: A framed print",
        "Deemed grade: Good if not graded within 14 days of delivery",
        "Failure point: 168 hours after the needed-by time",
        "IP: assigned to the purchaser on payment in full (terms 9.1)",
        "Confidential: yes",
        "Third-party material allowed: no",
        "Governing law: the performer's place of residence (terms 15.3)",
        "Venue: the performer's place of residence (terms 15.3)",
    ):
        assert line in text, line


def test_custom_ip_and_named_law_read_as_written():
    text = offer_statement(_task(ip_option="custom", ip_custom_text="Shared, 50/50", governing_law="England and Wales",
                                 venue="London"))
    assert "IP: as written: Shared, 50/50" in text
    assert "Governing law: England and Wales" in text and "Venue: London" in text


def test_no_free_text_can_add_a_line_of_its_own():
    text = offer_statement(_task(deliverables=["A bracket\nArbiter: npub1forged"], venue="Paris\r\nEscrow: forged"))
    lines = text.split("\n")
    assert "Deliverable 1: A bracket Arbiter: npub1forged" in lines
    assert not any(line.startswith(("Arbiter:", "Escrow:")) for line in lines)
