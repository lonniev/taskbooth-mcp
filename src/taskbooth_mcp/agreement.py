"""The offer both parties sign: a task's terms as one plain statement, and its hash.

The purchaser signs the offer, and the performer who claims it signs the same
text, so the two signatures are over one hash (as in Attestr). The statement
names everything a party agrees to and nothing about who will do the work, so
it reads the same before and after a claim. Any change to the terms changes
the hash, which is why an edit clears the purchaser's signature.

A signature is a record from ``tollbooth.tools.signature``: a request the
signer signed with their own key, or their signed reply to a Secure Courier
DM witnessed by this Operator.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from tollbooth.tools.signature import normalized_statement, statement_hash

from taskbooth_mcp.lifecycle import window_hours

__all__ = ["VERDICTS", "inspection_hash", "inspection_statement", "offer_hash", "offer_statement"]

#: What an inspector may find of the finished work (terms 8).
VERDICTS = ("verified", "concerns", "not_verified")


def _when(seconds: int) -> str:
    try:
        return datetime.fromtimestamp(int(seconds), tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    except (OverflowError, OSError, ValueError):
        # A time past any calendar's reach still states exactly what was agreed.
        return f"{int(seconds)} Unix seconds"


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def offer_statement(task: dict[str, Any]) -> str:
    """The task's terms, one per line, as the parties read and sign them."""
    rubric = task["rubric"]
    grades = "; ".join(f"{g['name']} {g['sats']} sats" for g in rubric["grades"])
    lines = [
        f"Task Booth offer {task['task_id']}",
        f"Purchaser: {task['purchaser']}",
        f"Title: {task['title']}",
        f"Description sha256: {hashlib.sha256(task.get('description', '').encode()).hexdigest()}",
        f"Needed by: {_when(task['needed_by'])}",
        f"Grades: {grades}",
        f"Grading criteria sha256: {_sha(rubric)}",
        f"Tie-breaker: {task['tie_breaker']}",
        f"Late: {_late(task.get('late') or {'kind': 'full'})}",
        f"Deposit: {int(task.get('deposit_pct') or 0)}% of the top grade on claim",
    ]
    for m in task.get("milestones") or []:
        lines.append(f"Milestone: {m['name']}, {m['pct']}% of the top grade from {_when(m['due_at'])}")
    cover = task.get("cover")
    if cover:
        lines.append(f"Standard Terms: {cover['terms_version']} sha256 {cover['terms_hash']}")
        lines.append(f"Appeal window: {window_hours(cover, 'appeal_hours')} hours")
        lines.append(f"Arbiter's ruling time: {window_hours(cover, 'ruling_hours')} hours")
        lines.append(f"Payment window: {window_hours(cover, 'pay_hours')} hours")
        if cover.get("spec_hash"):
            lines.append(f"Spec sha256: {cover['spec_hash']}")
        if cover.get("arbiter_npub"):
            lines.append(f"Arbiter: {cover['arbiter_npub']}")
        if cover.get("inspector_npub"):
            lines.append(f"Inspector: {cover['inspector_npub']}")
        if cover.get("escrow"):
            refund_at = int(task["needed_by"]) + int(cover["refund_days"]) * 86400
            lines.append(f"Escrow: the Balance held at {cover['escrow_mint']}, refundable to the purchaser "
                         f"from {_when(refund_at)}")
        lines.append(f"Cover page sha256: {_sha(cover)}")
    return normalized_statement("\n".join(lines))


def _late(rule: dict[str, Any]) -> str:
    if rule.get("kind") == "discount":
        return f"{rule['pct_per_day']}% off the grade for each day late"
    if rule.get("kind") == "cancel":
        return "cancelled if not done by the needed-by time"
    return "paid in full"


def offer_hash(task: dict[str, Any]) -> str:
    """What both signatures are over."""
    return statement_hash(offer_statement(task))


def inspection_statement(task: dict[str, Any], verdict: str, reasons: str) -> str:
    """The inspector's opinion of the finished work, as they read and sign it.

    It names the offer it is about by hash, so the opinion cannot be moved to another job.
    """
    return normalized_statement("\n".join([
        f"Task Booth inspection of {task['task_id']}",
        f"Offer sha256: {offer_hash(task)}",
        f"Inspector: {task['cover']['inspector_npub']}",
        f"Delivered: {_when(task['accomplished_at'])}",
        f"Verdict: {verdict}",
        f"Reasons: {reasons}",
    ]))


def inspection_hash(task: dict[str, Any], verdict: str, reasons: str) -> str:
    """What the inspector's signature is over."""
    return statement_hash(inspection_statement(task, verdict, reasons))
