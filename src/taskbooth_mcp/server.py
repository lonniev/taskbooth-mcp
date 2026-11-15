"""Task Booth — work posted with a signed rubric, paid in sats after it is graded.

A purchaser posts a task with a rubric: the grades, and the sats each grade earns.
A performer (a person or an agent, any npub) claims it, does it, and marks it done.
The purchaser checks the work in the real world and picks a grade. The sats go
from the purchaser's own wallet to the performer's, and this Operator never holds
them.

This module is the bootstrap: the standard DPYC tools (check_balance,
purchase_credits, Secure Courier, Oracle, pricing, peer grants) come from
``register_standard_tools`` in the tollbooth-dpyc wheel. The Task Booth's own
tools join ``_DOMAIN_TOOLS`` as they are built.

Run locally:
    python -m taskbooth_mcp.server
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastmcp import FastMCP
from mcp.types import Icon
from tollbooth.credential_templates import CredentialTemplate, FieldSpec
from tollbooth.credential_validators import validate_btcpay_creds
from tollbooth.params import PatronNpub
from tollbooth.properties import PropertySpec
from tollbooth.runtime import OperatorRuntime, register_standard_tools
from tollbooth.tool_identity import (
    CREATES,
    DELETES,
    READS,
    READS_REMOTE,
    STANDARD_IDENTITIES,
    WRITES,
    WRITES_REMOTE,
    ToolIdentity,
)

from taskbooth_mcp import __version__, feed, payout, price
from taskbooth_mcp.lifecycle import Action, State
from taskbooth_mcp.store import NotYours, TaskStore

logger = logging.getLogger(__name__)

#: The Horizon project's own URL. The holder connection's OAuth endpoints live here.
# Horizon still serves this Operator at the address it was created with.
SERVICE_URL = "https://taskqueue-mcp.fastmcp.app"

mcp = FastMCP(
    "taskbooth-mcp",
    version=__version__,
    icons=[Icon(src="https://tollbooth-dpyc.com/dpyc-logo.png", mimeType="image/png")],
    instructions=(
        "Task Booth — work posted with a signed rubric and paid in sats after it "
        "is graded. Monetized via Tollbooth DPYC Bitcoin Lightning micropayments.\n\n"
        "## How it works\n"
        "A purchaser posts a task with a rubric: the grades, and the sats each "
        "grade earns. A performer claims it, does it, and marks it done. The "
        "purchaser checks the work and picks a grade, and the sats go from the "
        "purchaser's own wallet to the performer's. This Operator never holds them.\n\n"
        "## A task, start to finish\n"
        "1. The purchaser calls taskbooth_post_task with a title, a rubric, the npubs "
        "who may claim it (the audience) and a needed-by time.\n"
        "2. A performer in the audience calls taskbooth_claim_task.\n"
        "3. The performer does the work and calls taskbooth_mark_done.\n"
        "4. The purchaser checks the work and calls taskbooth_assess_task with a grade "
        "from the rubric, or taskbooth_reject_task.\n"
        "5. The performer calls taskbooth_accept_assessment, or taskbooth_appeal_assessment "
        "once; the rubric's tie-breaker then calls taskbooth_decide_appeal. The top "
        "grade needs no acceptance.\n"
        "6. The task is now owed. taskbooth_request_payout returns a Lightning invoice "
        "for exactly the grade's sats. The purchaser pays it from any wallet. The invoice "
        "comes from the performer's payout_address (a Lightning address, set with "
        "taskbooth_set_my_property) when they have one. A performer without one makes an "
        "invoice for that amount in their own wallet and passes it as invoice.\n"
        "7. The performer calls taskbooth_confirm_paid when the sats arrive.\n\n"
        "taskbooth_calendar_publish mints a calendar feed of your tasks for a phone's "
        "calendar app; taskbooth_calendar_list and taskbooth_calendar_revoke manage it.\n\n"
        "taskbooth_list_tasks shows every task you have a part in; taskbooth_get_task "
        "and taskbooth_task_history show one. A task past its needed-by time and "
        "still unclaimed reads as expired. Times are Unix seconds. One npub may hold "
        "every role on a task, so you can post, claim, grade and pay yourself to try it.\n\n"
        "## Operator onboarding\n"
        "Call taskbooth_get_operator_onboarding_status to check readiness.\n"
        "1. Register with an Authority (provides a Neon database automatically)\n"
        "2. Deliver operator secrets via Secure Courier: btcpay_host, "
        "btcpay_api_key, btcpay_store_id. Call taskbooth_request_credential_channel to start.\n\n"
        "## Pricing\n"
        "Tool prices are set by the operator's pricing model. Use "
        "`taskbooth_check_price` to preview and `taskbooth_check_balance` to see your balance."
    ),
)

# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

# Frozen UUIDs — minted once at tool birth and never changed.
POST_TASK_UUID = "da1fd787-3e7a-48c7-a94c-2809a36be9bb"
LIST_TASKS_UUID = "fdb1f7f9-1695-4d42-9c5f-7baecfcbb7ad"
GET_TASK_UUID = "b1a27481-967e-47c6-9619-04ff80706140"
TASK_HISTORY_UUID = "4f45575d-8223-4a4c-8dea-d452b5c32177"
CLAIM_TASK_UUID = "162db38d-98bc-45d6-bd9d-3487d63ed6df"
RELEASE_CLAIM_UUID = "f5894f1c-098f-4055-bfd7-ad53b98ab068"
WITHDRAW_TASK_UUID = "071c7ff5-f73a-4525-8959-67f0ae703e51"
MARK_DONE_UUID = "bf4363af-1fe5-4d10-b752-89d3705c7e6f"
ASSESS_TASK_UUID = "af1b1232-b336-4e76-86c0-6b74960e778b"
REJECT_TASK_UUID = "47245915-a9f0-408a-9ac9-172620b66e67"
ACCEPT_ASSESSMENT_UUID = "14ccb2e9-08c9-4412-8f73-8a093a71f917"
APPEAL_ASSESSMENT_UUID = "2e7bc8c7-d50a-47c0-9d6f-8fbc282c19f9"
DECIDE_APPEAL_UUID = "6d1ca911-8ca3-4cf2-9474-089256a3fba7"
DELEGATE_ASSESSOR_UUID = "fbb5abdb-95dc-4a05-b816-7239271db34c"
REQUEST_PAYOUT_UUID = "5969f5f9-ed64-4b15-ad4a-015d8192b960"
CONFIRM_PAID_UUID = "fc620f48-1061-41ff-8d73-8fb41d627617"
SAVE_RUBRIC_TEMPLATE_UUID = "ab2db4cf-915f-4979-a1b1-26e72abfcfbf"
LIST_RUBRIC_TEMPLATES_UUID = "a9114167-0545-4417-94b8-0458b9c4dbae"
BTC_PRICE_UUID = "1d9f26c9-7883-45b0-b626-9b5e66c38a44"
CALENDAR_PUBLISH_UUID = "d58d1de3-9455-46cf-ad9f-cc63f063e0de"
CALENDAR_FETCH_UUID = "e91b5e42-a7ce-4a07-a7a3-2948f25fb534"
CALENDAR_LIST_UUID = "a2a11111-6c18-4c91-a80e-dd39385321b5"
CALENDAR_REVOKE_UUID = "e0fe233b-2b79-4902-8429-f956b0d766dc"


def _identity(tool_id: str, capability: str, category: str, title: str, behavior: Any, intent: str) -> ToolIdentity:
    return ToolIdentity(tool_id=tool_id, capability=capability, category=category, title=title,
                        behavior=behavior, intent=intent)


#: The Task Booth's own tools, each with a frozen UUID minted once at its birth.
_DOMAIN_TOOLS: list[ToolIdentity] = [
    _identity(POST_TASK_UUID, "post_task", "write", "Post a task", CREATES,
              "Post a task with its rubric, its audience and a needed-by time"),
    _identity(LIST_TASKS_UUID, "list_tasks", "read", "List tasks", READS,
              "Every task the caller posted, performs, grades or may claim"),
    _identity(GET_TASK_UUID, "get_task", "read", "Get a task", READS,
              "One task whole, with its payout when one is owed"),
    _identity(TASK_HISTORY_UUID, "task_history", "read", "Task history", READS,
              "Every move made on a task, with who made it and when"),
    _identity(CLAIM_TASK_UUID, "claim_task", "write", "Claim a task", WRITES,
              "Take on a posted task as its performer"),
    _identity(RELEASE_CLAIM_UUID, "release_claim", "write", "Release a claim", WRITES,
              "Give a claimed task back, or take back a stale claim"),
    _identity(WITHDRAW_TASK_UUID, "withdraw_task", "write", "Withdraw a task", WRITES,
              "Take back a task nobody has claimed"),
    _identity(MARK_DONE_UUID, "mark_done", "write", "Mark a task done", WRITES,
              "Say the work is finished and ready to be checked"),
    _identity(ASSESS_TASK_UUID, "assess_task", "write", "Assess a task", WRITES,
              "Give finished work a grade from its rubric"),
    _identity(REJECT_TASK_UUID, "reject_task", "write", "Reject a task", WRITES,
              "Say the work was not done or not accepted at all"),
    _identity(ACCEPT_ASSESSMENT_UUID, "accept_assessment", "write", "Accept an assessment", WRITES,
              "Agree with the grade, which makes it final"),
    _identity(APPEAL_ASSESSMENT_UUID, "appeal_assessment", "write", "Appeal an assessment", WRITES,
              "Ask the rubric's tie-breaker to look at the grade, once"),
    _identity(DECIDE_APPEAL_UUID, "decide_appeal", "write", "Decide an appeal", WRITES,
              "Settle an appeal with a final grade"),
    _identity(DELEGATE_ASSESSOR_UUID, "delegate_assessor", "write", "Delegate an assessor", WRITES,
              "Let another npub grade a task for the purchaser"),
    _identity(REQUEST_PAYOUT_UUID, "request_payout", "write", "Request the payout invoice", WRITES_REMOTE,
              "The Lightning invoice that pays an owed task"),
    _identity(CONFIRM_PAID_UUID, "confirm_paid", "write", "Confirm payment", WRITES,
              "Say the sats for an owed task arrived"),
    _identity(SAVE_RUBRIC_TEMPLATE_UUID, "save_rubric_template", "write", "Save a rubric template", WRITES,
              "Keep a rubric to reuse on later tasks"),
    _identity(LIST_RUBRIC_TEMPLATES_UUID, "list_rubric_templates", "read", "List rubric templates", READS,
              "The rubrics the caller has kept"),
    _identity(CALENDAR_PUBLISH_UUID, "calendar_publish", "write", "Publish a calendar feed", CREATES,
              "Mint a calendar feed of the caller's tasks, reached by an unguessable token"),
    _identity(CALENDAR_FETCH_UUID, "calendar_fetch", "free", "Fetch a calendar feed", READS,
              "Read a calendar feed by its token, for the site that serves it"),
    _identity(CALENDAR_LIST_UUID, "calendar_list", "read", "List calendar feeds", READS,
              "The calendar feeds the caller keeps, and how often each is read"),
    _identity(CALENDAR_REVOKE_UUID, "calendar_revoke", "write", "Revoke a calendar feed", DELETES,
              "Stop a calendar feed so its token opens nothing"),
    _identity(BTC_PRICE_UUID, "btc_price", "free", "Bitcoin's price", READS_REMOTE,
              "What a bitcoin costs today and on this day in earlier years, as mempool.space reports it"),
]

#: What a patron may set about themselves here (taskbooth_set_my_property).
PATRON_PROPERTIES: dict[str, PropertySpec] = {
    "payout_address": PropertySpec(
        kind="text", default=None, label="Payout address",
        description="Optional. The Lightning address your rewards are paid to, like name@wallet.example.",
    ),
}

TOOL_REGISTRY: dict[str, ToolIdentity] = {ti.tool_id: ti for ti in _DOMAIN_TOOLS}

runtime = OperatorRuntime(
    tool_registry={**STANDARD_IDENTITIES, **TOOL_REGISTRY},
    operator_credential_template=CredentialTemplate(
        # The vault files the Operator's BTCPay credentials under this key; it changes only with a redelivery.
        service="taskqueue-operator",
        version=1,
        description="Operator credentials for BTCPay Lightning payments",
        fields={
            "btcpay_host": FieldSpec(
                required=True, sensitive=True,
                description="The URL of your BTCPay Server instance (e.g. https://btcpay.example.com).",
            ),
            "btcpay_api_key": FieldSpec(
                required=True, sensitive=True,
                description="Your BTCPay Server API key. Generate one under Account > Manage Account > API Keys.",
            ),
            "btcpay_store_id": FieldSpec(
                required=True, sensitive=True,
                description="Your BTCPay Store ID. Find it under Stores > Settings > General.",
            ),
        },
    ),
    operator_credential_greeting=(
        "Hi — I'm Task Booth, an MCP service where work is posted with a signed "
        "rubric and paid in sats after it is graded. You (or your AI agent) "
        "requested a credential channel."
    ),
    service_name="Task Booth",
    credential_validator=validate_btcpay_creds,
    holder_auth_url=SERVICE_URL,
    patron_properties=PATRON_PROPERTIES,
)

tool = register_standard_tools(
    mcp,
    "taskbooth",
    runtime,
    service_name="taskbooth",
    service_version=__version__,
)

_STORE: TaskStore | None = None


async def store() -> TaskStore:
    global _STORE
    if _STORE is None:
        created = TaskStore(await runtime.vault())
        await created.ensure_schema()
        _STORE = created
    return _STORE


async def payout_address_of(npub: str) -> str:
    """The Lightning address ``npub`` set as their payout_address, or ``""``."""
    values = await (await runtime.property_store()).values(npub)
    value = values.get("payout_address", ("", ""))[0]
    return value if isinstance(value, str) else ""


async def _do(coro: Any) -> Any:
    """Run a store call; a task that is not the caller's is a refusal, so the fare is refunded."""
    try:
        return await coro
    except NotYours as exc:
        raise ValueError(str(exc)) from None


async def _move(npub: str, task_id: str, action: Action, **kwargs: Any) -> dict[str, Any]:
    db = await store()
    task = await _do(db.act(runtime.resolve_npub(npub), str(task_id), action, **kwargs))
    return {"ok": True, "task": task}


# ---------------------------------------------------------------------------
# Posting and reading
# ---------------------------------------------------------------------------


@tool
@runtime.paid_tool(POST_TASK_UUID)
async def post_task(
    title: str,
    rubric: dict[str, Any],
    audience: list[str],
    needed_by: int,
    description: str = "",
    earliest_start: int = 0,
    every_days: int = 0,
    tie_breaker: str = "",
    assessors: list[str] | None = None,
    org_id: str = "",
    project_id: str = "",
    signature: str = "",
    npub: PatronNpub = "",
) -> dict[str, Any]:
    """Post a task for others (or yourself) to claim.

    Args:
        title: What needs doing, e.g. "Clean your room".
        rubric: The grades and what each earns:
            {"grades": [{"name": "Spotless", "sats": 2000, "criteria": "Nothing on the floor"},
                        {"name": "Not done", "sats": 0}], "appeal": "One appeal"}.
            The highest grade is the most you can owe.
        audience: The npubs who may see and claim it. Name your own to try it yourself.
        needed_by: When it must be done, in Unix seconds. Unclaimed past this, it reads as expired.
        description: More about the work.
        earliest_start: When work may begin, in Unix seconds. 0 for any time.
        every_days: Repeat every this many days (7 for weekly). 0 for a one-off.
        tie_breaker: The npub who decides an appeal. Omitted, it is you.
        assessors: Other npubs who may grade it for you.
        org_id: Your Purser Organization, if this is its work.
        project_id: The Purser Project it belongs to.
        signature: Your signature on the offer, when your client makes one.
    """
    db = await store()
    task_id = await db.post(
        runtime.resolve_npub(npub), title=title, rubric=rubric, audience=audience, needed_by=needed_by,
        description=description, earliest_start=earliest_start, every_days=every_days,
        tie_breaker=tie_breaker, assessors=assessors, org_id=org_id, project_id=project_id,
        purchaser_signature=signature,
    )
    return {"ok": True, "task": await db.get(runtime.resolve_npub(npub), task_id)}


@tool
@runtime.paid_tool(LIST_TASKS_UUID)
async def list_tasks(npub: PatronNpub = "") -> dict[str, Any]:
    """Every task you posted, perform, grade, or may claim; soonest needed first."""
    return {"ok": True, "tasks": await (await store()).tasks_of(runtime.resolve_npub(npub))}


@tool
@runtime.paid_tool(GET_TASK_UUID)
async def get_task(task_id: str, npub: PatronNpub = "") -> dict[str, Any]:
    """One task whole. Its payout is included for the purchaser and the performer once an invoice exists.

    Args:
        task_id: The task.
    """
    db = await store()
    task = await _do(db.get(runtime.resolve_npub(npub), str(task_id)))
    out: dict[str, Any] = {"ok": True, "task": task}
    if {"purchaser", "performer"} & set(task["your_roles"]):
        kept = await db.payout(task["task_id"])
        if kept:
            out["payout"] = payout.as_handed_over(kept)
    return out


@tool
@runtime.paid_tool(TASK_HISTORY_UUID)
async def task_history(task_id: str, npub: PatronNpub = "") -> dict[str, Any]:
    """Every move made on a task, oldest first: who made it, when, and what it changed.

    Args:
        task_id: The task.
    """
    db = await store()
    return {"ok": True, "events": await _do(db.events(runtime.resolve_npub(npub), str(task_id)))}


# ---------------------------------------------------------------------------
# The performer's moves
# ---------------------------------------------------------------------------


@tool
@runtime.paid_tool(CLAIM_TASK_UUID)
async def claim_task(task_id: str, signature: str = "", npub: PatronNpub = "") -> dict[str, Any]:
    """Take on a posted task. You must be in its audience.

    Args:
        task_id: The task.
        signature: Your countersignature on the offer, when your client makes one.
    """
    return await _move(npub, task_id, Action.CLAIM, performer_signature=signature)


@tool
@runtime.paid_tool(RELEASE_CLAIM_UUID)
async def release_claim(task_id: str, npub: PatronNpub = "") -> dict[str, Any]:
    """Give a task you claimed back, or, as its purchaser, take back a claim that has gone stale.

    Args:
        task_id: The task.
    """
    return await _move(npub, task_id, Action.RELEASE)


@tool
@runtime.paid_tool(MARK_DONE_UUID)
async def mark_done(task_id: str, npub: PatronNpub = "") -> dict[str, Any]:
    """Say the work is finished. The purchaser then checks it and grades it.

    Args:
        task_id: The task you claimed.
    """
    return await _move(npub, task_id, Action.MARK_DONE)


@tool
@runtime.paid_tool(ACCEPT_ASSESSMENT_UUID)
async def accept_assessment(task_id: str, npub: PatronNpub = "") -> dict[str, Any]:
    """Agree with the grade you were given. That makes it final, and its sats owed.

    Args:
        task_id: The task you performed.
    """
    return await _move(npub, task_id, Action.ACCEPT)


@tool
@runtime.paid_tool(APPEAL_ASSESSMENT_UUID)
async def appeal_assessment(task_id: str, reasons: str = "", npub: PatronNpub = "") -> dict[str, Any]:
    """Ask the rubric's tie-breaker to look at your grade. You may appeal once.

    Args:
        task_id: The task you performed.
        reasons: Why the grade should be different.
    """
    return await _move(npub, task_id, Action.APPEAL, reasons=reasons)


@tool
@runtime.paid_tool(CONFIRM_PAID_UUID)
async def confirm_paid(task_id: str, npub: PatronNpub = "") -> dict[str, Any]:
    """Say the sats for a task you performed arrived in your wallet. The task is then paid.

    Args:
        task_id: The task you performed.
    """
    return await _move(npub, task_id, Action.CONFIRM_PAID)


# ---------------------------------------------------------------------------
# The purchaser's moves
# ---------------------------------------------------------------------------


@tool
@runtime.paid_tool(WITHDRAW_TASK_UUID)
async def withdraw_task(task_id: str, npub: PatronNpub = "") -> dict[str, Any]:
    """Take back a task you posted that nobody has claimed. A repeating task stops repeating.

    Args:
        task_id: The task.
    """
    return await _move(npub, task_id, Action.WITHDRAW)


@tool
@runtime.paid_tool(ASSESS_TASK_UUID)
async def assess_task(task_id: str, grade: str, reasons: str = "", npub: PatronNpub = "") -> dict[str, Any]:
    """Give finished work a grade from its rubric. Check the work yourself first.

    The top grade is final at once. Any other waits for the performer to accept or appeal.

    Args:
        task_id: A task marked done that you may grade.
        grade: The name of a grade in the task's rubric.
        reasons: Why this grade.
    """
    return await _move(npub, task_id, Action.ASSESS, grade=grade, reasons=reasons)


@tool
@runtime.paid_tool(REJECT_TASK_UUID)
async def reject_task(task_id: str, reasons: str = "", npub: PatronNpub = "") -> dict[str, Any]:
    """Say the work was not done, or not accepted at all. Nothing is owed.

    For work that was done poorly, give it a low grade with taskbooth_assess_task instead.

    Args:
        task_id: A task marked done that you may grade.
        reasons: Why it is rejected.
    """
    return await _move(npub, task_id, Action.REJECT, reasons=reasons)


@tool
@runtime.paid_tool(DECIDE_APPEAL_UUID)
async def decide_appeal(task_id: str, grade: str, reasons: str = "", npub: PatronNpub = "") -> dict[str, Any]:
    """Settle an appeal as the rubric's tie-breaker. Your grade is final.

    Args:
        task_id: A task under appeal that names you as tie-breaker.
        grade: The name of a grade in the task's rubric.
        reasons: Why this grade.
    """
    return await _move(npub, task_id, Action.DECIDE_APPEAL, grade=grade, reasons=reasons)


@tool
@runtime.paid_tool(DELEGATE_ASSESSOR_UUID)
async def delegate_assessor(
    task_id: str, assessor_npub: str, allowed: bool = True, npub: PatronNpub = ""
) -> dict[str, Any]:
    """Let another npub grade a task you posted, or stop letting them.

    Args:
        task_id: A task you posted.
        assessor_npub: Who may grade it for you.
        allowed: False to take the permission away.
    """
    db = await store()
    task = await _do(db.set_assessor(runtime.resolve_npub(npub), str(task_id), assessor_npub, bool(allowed)))
    return {"ok": True, "task": task}


# ---------------------------------------------------------------------------
# Payout
# ---------------------------------------------------------------------------


@tool
@runtime.paid_tool(REQUEST_PAYOUT_UUID)
async def request_payout(task_id: str, invoice: str = "", npub: PatronNpub = "") -> dict[str, Any]:
    """The Lightning invoice that pays an owed task, for exactly the sats its grade earns.

    The purchaser pays it from any wallet; the sats go straight to the performer.
    Asking again returns the same invoice until it expires, then a fresh one.
    The performer calls taskbooth_confirm_paid once the sats arrive.

    The invoice is fetched from the performer's payout_address when they have
    one. A performer without one makes an invoice for exactly the sats owed in
    their own wallet and passes it here as invoice.

    Args:
        task_id: An owed task you posted or performed.
        invoice: The performer's own BOLT11 invoice for exactly the sats owed. Only the performer passes one.
    """
    me = runtime.resolve_npub(npub)
    db = await store()
    task = await _do(db.get(me, str(task_id)))
    if not {"purchaser", "performer"} & set(task["your_roles"]):
        raise ValueError("Only the purchaser or the performer may ask for a task's payout invoice.")
    if task["state"] != State.OWED:
        raise ValueError(f"This task is {task['standing']}; an invoice is made only for a task that is owed.")
    if invoice:
        if "performer" not in task["your_roles"]:
            raise ValueError("Only the performer may hand in their own invoice.")
        return {"ok": True, "payout": await payout.invoice_from_performer(db, task, invoice, performer=me)}
    address = await payout_address_of(task["performer"])
    return {"ok": True, "payout": await payout.invoice_for(db, task, address, requested_by=me)}


# ---------------------------------------------------------------------------
# Rubric templates
# ---------------------------------------------------------------------------


@tool
@runtime.paid_tool(SAVE_RUBRIC_TEMPLATE_UUID)
async def save_rubric_template(
    name: str, rubric: dict[str, Any], template_id: str = "", npub: PatronNpub = ""
) -> dict[str, Any]:
    """Keep a rubric to reuse on later tasks, or replace one you kept.

    Args:
        name: What to call it, e.g. "Bedroom chores".
        rubric: The grades and what each earns, as for taskbooth_post_task.
        template_id: A template of yours to replace. Omit to keep a new one.
    """
    db = await store()
    saved = await _do(db.save_template(runtime.resolve_npub(npub), name, rubric, str(template_id)))
    return {"ok": True, "template_id": saved}


@tool
@runtime.paid_tool(LIST_RUBRIC_TEMPLATES_UUID)
async def list_rubric_templates(npub: PatronNpub = "") -> dict[str, Any]:
    """The rubrics you have kept."""
    return {"ok": True, "templates": await (await store()).templates_of(runtime.resolve_npub(npub))}



# ---------------------------------------------------------------------------
# Calendar feeds
# ---------------------------------------------------------------------------


@tool
@runtime.paid_tool(CALENDAR_PUBLISH_UUID)
async def calendar_publish(name: str, time_zone: str, npub: PatronNpub = "") -> dict[str, Any]:
    """Mint a calendar feed of your tasks that a phone or computer can subscribe to.

    Returns a token. The site you use serves the feed at an address built from
    it. The token is the feed's only lock: anyone who has the address can read
    your tasks' titles, details, due days and rewards. The feed names nobody
    and carries no npub. taskbooth_calendar_revoke stops it.

    Args:
        name: What the calendar is called in the calendar app, e.g. "Sam's chores".
        time_zone: Your IANA time zone, e.g. "America/New_York". It decides which day a task falls on.
    """
    db = await store()
    return {"ok": True, "feed": await db.publish_feed(runtime.resolve_npub(npub), name, time_zone)}


@tool
@runtime.paid_tool(CALENDAR_FETCH_UUID)
async def calendar_fetch(token: str) -> dict[str, Any]:
    """Read a calendar feed by its token, as iCalendar text.

    For the site that serves the feed's address: a calendar app speaks HTTP and
    has nowhere to carry an npub proof, so the token is the credential. The
    feed is built now, from the tasks as they stand.

    Args:
        token: The feed token from taskbooth_calendar_publish.
    """
    db = await store()
    found = await db.feed_for(token)
    if found is None:
        raise ValueError("That is not a calendar feed.")
    tasks = await db.tasks_of(found["owner"])
    ics = feed.chores_ics(tasks, name=found["name"], zone=found["zone"], token=found["token"], at=int(time.time()))
    return {"ok": True, "ics": ics, "name": found["name"], "count": ics.count("BEGIN:VEVENT")}


@tool
@runtime.paid_tool(CALENDAR_LIST_UUID)
async def calendar_list(npub: PatronNpub = "") -> dict[str, Any]:
    """The calendar feeds you keep, with each one's token and how often it has been read."""
    return {"ok": True, "feeds": await (await store()).feeds_of(runtime.resolve_npub(npub))}


@tool
@runtime.paid_tool(CALENDAR_REVOKE_UUID)
async def calendar_revoke(token: str, npub: PatronNpub = "") -> dict[str, Any]:
    """Stop one of your calendar feeds. Its address opens nothing afterwards.

    Args:
        token: The feed token to stop.
    """
    db = await store()
    if not await db.revoke_feed(runtime.resolve_npub(npub), token):
        raise ValueError("No calendar feed with that token is yours.")
    return {"ok": True}



# ---------------------------------------------------------------------------
# Bitcoin's price
# ---------------------------------------------------------------------------


@tool
@runtime.paid_tool(BTC_PRICE_UUID)
async def btc_price(fiat: str = "USD", years: int = 4) -> dict[str, Any]:
    """What one bitcoin costs today, and on this day in each of the last few years.

    Read from mempool.space and passed on with the time each price was observed.
    A reading is reused for up to an hour. Divide by 100,000,000 for the price of one sat.

    Args:
        fiat: USD, EUR, GBP, CAD, CHF, AUD or JPY.
        years: How many earlier years to include, 0 to 10.
    """
    return {"ok": True, **await price.reading(fiat, years)}


if __name__ == "__main__":
    mcp.run()
