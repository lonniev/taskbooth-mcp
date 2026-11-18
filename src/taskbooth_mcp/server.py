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
from tollbooth import cashu
from tollbooth.credential_templates import CredentialTemplate, FieldSpec
from tollbooth.credential_validators import validate_btcpay_creds
from tollbooth.lnurl import LnurlResolutionError, resolve_lightning_address
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

from taskbooth_mcp import __version__, escrow, feed, payout, price, terms
from taskbooth_mcp.lifecycle import Action, State
from taskbooth_mcp.store import NotYours, TaskStore

logger = logging.getLogger(__name__)

#: The Horizon project's own URL. The holder connection's OAuth endpoints live here.
SERVICE_URL = "https://taskbooth-mcp.fastmcp.app"

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
        "A task posted with deposit_pct asks for that share of its top grade once it is "
        "claimed. taskbooth_request_deposit returns the invoice, the purchaser pays it, and "
        "the performer calls taskbooth_confirm_deposit. The award then pays only what the "
        "grade earns beyond the deposit.\n\n"
        "Milestones are dated part-payments of the top grade, payable from their date: "
        "taskbooth_request_milestone and taskbooth_confirm_milestone work like the deposit's "
        "tools, by milestone index. A cover page (spec link and hash, deliverables, a deemed "
        "grade, IP and confidentiality) puts the job under the MakerMarket Standard Terms, "
        "whose text and hash taskbooth_standard_terms returns. Finished work the purchaser "
        "leaves ungraded past the cover page's deemed-acceptance period is graded at its "
        "deemed grade. Every amount is in sats.\n\n"
        "A task with a cover page is a contract once both parties sign the same offer: "
        "taskbooth_offer_terms shows the statement and its hash. The purchaser signs before "
        "anyone may claim it, and the performer signs before marking it done. An agent with "
        "its own key calls taskbooth_sign_offer as a signed request; anyone else calls "
        "taskbooth_request_offer_signature, replies to the DM, then calls "
        "taskbooth_receive_offer_signature.\n\n"
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
EDIT_TASK_UUID = "9621461f-699c-4a6d-96c3-7f2c9278a9ba"
NUDGE_TASK_UUID = "d1a006a3-4b43-447c-82e3-97ca2229651f"
REQUEST_DEPOSIT_UUID = "771d7242-dcdd-466b-ba61-f9c06d18a547"
CONFIRM_DEPOSIT_UUID = "118e4115-a77d-4569-9162-e62a81ce6088"
REQUEST_MILESTONE_UUID = "bc0046f7-0c57-4866-ac2f-9850b3adfcce"
CONFIRM_MILESTONE_UUID = "04b1d2a2-9280-468b-935c-06c6593c3b60"
STANDARD_TERMS_UUID = "e9653c73-6503-45af-a0b5-1e336ebe7660"
OFFER_TERMS_UUID = "ede94cef-da9f-43c8-818d-0cccffdfb9fe"
SIGN_OFFER_UUID = "ebb9aa17-2ab9-4e30-b19a-2adae5524231"
REQUEST_OFFER_SIGNATURE_UUID = "28ed89a0-b1c5-45cd-a77b-de8ef919d391"
RECEIVE_OFFER_SIGNATURE_UUID = "599ac87b-fa6d-405d-8509-280b30bdd49c"
SAVE_ORGANIZATION_UUID = "0ac80c75-0f6f-4103-a124-04aa2fde8880"
LIST_ORGANIZATIONS_UUID = "79afc839-9b86-4796-9add-d64873bff833"
GET_ORGANIZATION_UUID = "826274a4-cb23-4c5b-8b52-968feaa2dd7b"
SAVE_MEMBER_UUID = "cd661183-58c4-4627-8946-0f0bc9f83901"
REMOVE_MEMBER_UUID = "0743b2af-ef8e-449d-90b1-d70f2c665b4b"
SAVE_PROJECT_UUID = "35b7db5c-db49-46d5-a2a2-b3e80c261fb0"
ARCHIVE_PROJECT_UUID = "da662acd-f718-4308-b006-c64faf3bdf18"
GET_PROJECT_UUID = "49b3fa09-53cd-48d5-a5cb-90185427663d"
ESCROW_INVOICE_UUID = "f56bc35c-58c4-486e-b6b0-7a20d44d4061"
ESCROW_STATUS_UUID = "cc34e815-f5cb-4690-8c0e-56928aa3844c"
ESCROW_PROPOSE_UUID = "d69f5474-42e1-4899-b77e-de6bf4bb3491"
ESCROW_COLLECT_UUID = "a308862f-7b58-499d-80b1-f3ab68295a47"
ESCROW_SIGN_UUID = "2ea31626-27a0-44c4-b07c-b11faf20925e"


def _identity(tool_id: str, capability: str, category: str, title: str, behavior: Any, intent: str) -> ToolIdentity:
    return ToolIdentity(tool_id=tool_id, capability=capability, category=category, title=title,
                        behavior=behavior, intent=intent)


#: The Task Booth's own tools, each with a frozen UUID minted once at its birth.
_DOMAIN_TOOLS: list[ToolIdentity] = [
    _identity(POST_TASK_UUID, "post_task", "write", "Post a task", CREATES,
              "Post a task with its rubric, its audience and a needed-by time"),
    _identity(EDIT_TASK_UUID, "edit_task", "write", "Edit a task", WRITES,
              "Change a posted task's terms while nobody has claimed it"),
    _identity(NUDGE_TASK_UUID, "nudge_task", "write", "Send a reminder", WRITES,
              "Remind the purchaser to pay, or the performer that the deadline is near"),
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
    _identity(REQUEST_DEPOSIT_UUID, "request_deposit", "write", "Request the deposit invoice", WRITES_REMOTE,
              "The Lightning invoice that pays a claimed task's deposit"),
    _identity(CONFIRM_DEPOSIT_UUID, "confirm_deposit", "write", "Confirm the deposit", WRITES,
              "Say the sats for a claimed task's deposit arrived"),
    _identity(REQUEST_MILESTONE_UUID, "request_milestone", "write", "Request a milestone invoice", WRITES_REMOTE,
              "The Lightning invoice that pays a task's milestone once it falls due"),
    _identity(CONFIRM_MILESTONE_UUID, "confirm_milestone", "write", "Confirm a milestone", WRITES,
              "Say the sats for a task's milestone arrived"),
    _identity(STANDARD_TERMS_UUID, "standard_terms", "free", "The Standard Terms", READS,
              "The Standard Terms a cover page puts a job under, with the hash a signature covers"),
    _identity(OFFER_TERMS_UUID, "offer_terms", "read", "Read the offer to sign", READS,
              "A task's terms as the one statement both parties sign, its hash, and who has signed"),
    _identity(SIGN_OFFER_UUID, "sign_offer", "write", "Sign the offer", WRITES,
              "Sign a task's offer with your own key, by calling this tool as a signed request"),
    _identity(REQUEST_OFFER_SIGNATURE_UUID, "request_offer_signature", "write", "Ask to sign by DM", WRITES_REMOTE,
              "Have the offer sent to you as a Nostr DM, to sign by replying"),
    _identity(RECEIVE_OFFER_SIGNATURE_UUID, "receive_offer_signature", "write", "Collect a DM signature",
              WRITES_REMOTE, "Collect your signed reply to the offer and keep it as your signature"),
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
    _identity(SAVE_ORGANIZATION_UUID, "save_organization", "write", "Save an Organization", CREATES,
              "Make an Organization to file tasks under, or rename one you own"),
    _identity(LIST_ORGANIZATIONS_UUID, "list_organizations", "read", "List Organizations", READS,
              "The Organizations you own or belong to"),
    _identity(GET_ORGANIZATION_UUID, "get_organization", "read", "Get an Organization", READS,
              "An Organization with its members and Projects"),
    _identity(SAVE_MEMBER_UUID, "save_member", "write", "Add a member", WRITES,
              "Add an npub to your Organization, or change their label"),
    _identity(REMOVE_MEMBER_UUID, "remove_member", "write", "Remove a member", DELETES,
              "Take an npub out of your Organization"),
    _identity(SAVE_PROJECT_UUID, "save_project", "write", "Save a Project", CREATES,
              "Make a Project in your Organization, or change its brief, budget, circle and default terms"),
    _identity(ARCHIVE_PROJECT_UUID, "archive_project", "write", "Archive a Project", WRITES,
              "Close a Project to new tasks, or open it again"),
    _identity(GET_PROJECT_UUID, "get_project", "read", "Get a Project", READS,
              "A Project with what its tasks have committed and paid"),
    _identity(ESCROW_INVOICE_UUID, "escrow_invoice", "write", "Fund a job's escrow", WRITES_REMOTE,
              "The Lightning invoice that puts a claimed job's Balance into escrow at the agreed mint"),
    _identity(ESCROW_STATUS_UUID, "escrow_status", "read", "Read a job's escrow", READS,
              "Where a job's escrow stands, and what you may sign now"),
    _identity(ESCROW_PROPOSE_UUID, "escrow_propose", "write", "Propose an escrow split", WRITES,
              "Propose how a job's escrow divides between performer and purchaser"),
    _identity(ESCROW_COLLECT_UUID, "escrow_collect", "write", "Collect from escrow", WRITES_REMOTE,
              "Ready your share of a job's escrow to be paid to your payout address"),
    _identity(ESCROW_SIGN_UUID, "escrow_sign", "write", "Sign for escrow", WRITES_REMOTE,
              "Sign what a job's escrow is waiting for: funding it, a split, or your payout"),
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

#: What the owner sets for this Operator (taskbooth_set_operator_property).
OPERATOR_PROPERTIES: dict[str, PropertySpec] = {
    "escrow_mints": PropertySpec(
        kind="text", default=None, label="Escrow mints",
        description="The Cashu mints offered for escrow: https addresses separated by spaces. Empty offers none.",
    ),
}

TOOL_REGISTRY: dict[str, ToolIdentity] = {ti.tool_id: ti for ti in _DOMAIN_TOOLS}

runtime = OperatorRuntime(
    tool_registry={**STANDARD_IDENTITIES, **TOOL_REGISTRY},
    operator_credential_template=CredentialTemplate(
        service="taskbooth-operator",
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
    operator_properties=OPERATOR_PROPERTIES,
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


async def escrow_mints() -> tuple[str, ...]:
    """The mints the owner offers for escrow; an address that is not a public https mint is skipped."""
    value = await runtime.operator_property("escrow_mints")
    mints = []
    for word in (value or "").replace(",", " ").split() if isinstance(value, str) else []:
        try:
            mints.append(cashu.checked_mint_url(word))
        except ValueError:
            logger.warning("escrow_mints names an address that is not a public https mint; skipped")
    return tuple(mints)


def _mint_for(url: str) -> cashu.MintClient:
    return cashu.MintClient(url)


async def _invoice_for(address: str, sats: int) -> str:
    try:
        return await resolve_lightning_address(address, sats)
    except LnurlResolutionError as exc:
        raise ValueError(f"Your payout address could not make an invoice: {exc}") from None


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
    late: dict[str, Any] | None = None,
    deposit_pct: int = 0,
    cover: dict[str, Any] | None = None,
    milestones: list[dict[str, Any]] | None = None,
    site: str = "",
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
        org_id: The Organization this is filed under (taskbooth_list_organizations). Give it with
            project_id, or neither. You must own or belong to it.
        project_id: The Project in that Organization it belongs to. An archived Project takes none.
        signature: Your signature on the offer, when your client makes one.
        late: What happens if it is done after needed_by. {"kind": "full"} pays in full (the default);
            {"kind": "discount", "pct_per_day": 10} takes 10% off the grade for each day late, never
            below zero; {"kind": "cancel"} closes it as missed once needed_by passes undone.
        deposit_pct: The share of the top grade, 0 to 90 percent, paid to the performer when they
            claim it. The award later pays only what the grade earns beyond it. 0 asks for none.
        cover: A job's cover page, which puts it under the Standard Terms (taskbooth_standard_terms):
            {"spec_url": "https://…", "spec_hash": "<sha256 hex>", "deliverables": ["…"],
             "deemed_grade": "<a rubric grade>", "deemed_acceptance_days": 14,
             "ip_option": "assign" | "license" | "worker_keeps" | "custom", "ip_custom_text": "…",
             "confidential": false, "client_is_consumer": false, "third_party_allowed": false,
             "governing_law": "…", "venue": "…",
             "arbiter_npub": "npub1…", "escrow": true, "escrow_mint": "https://…", "refund_days": 14}.
            Every field is optional. A named arbiter signs the offer and decides any appeal. Escrow holds the
            Balance at a mint this Operator offers (taskbooth_get_operator_properties, escrow_mints); it needs
            an arbiter, and refund_days sets the refund date after the needed-by time.
        milestones: Dated part-payments of the top grade, up to 6:
            [{"name": "Halfway", "due_at": <Unix seconds>, "pct": 45}]. With deposit_pct they ask for
            at most 90%. Each is payable from its date, and the award pays only what is left.
        site: The site posting it, a short lowercase name such as "chipin". A site lists its own tasks
            and every task posted with no site (taskbooth_list_tasks). Omit for none.
    """
    db = await store()
    task_id = await db.post(
        runtime.resolve_npub(npub), title=title, rubric=rubric, audience=audience, needed_by=needed_by,
        description=description, earliest_start=earliest_start, every_days=every_days,
        tie_breaker=tie_breaker, assessors=assessors, org_id=org_id, project_id=project_id,
        purchaser_signature=signature, late=late, deposit_pct=deposit_pct, cover=cover, milestones=milestones,
        site=site, escrow_mints=await escrow_mints(),
    )
    return {"ok": True, "task": await db.get(runtime.resolve_npub(npub), task_id)}


@tool
@runtime.paid_tool(EDIT_TASK_UUID)
async def edit_task(
    task_id: str,
    title: str | None = None,
    description: str | None = None,
    rubric: dict[str, Any] | None = None,
    audience: list[str] | None = None,
    needed_by: int | None = None,
    every_days: int | None = None,
    late: dict[str, Any] | None = None,
    deposit_pct: int | None = None,
    cover: dict[str, Any] | None = None,
    milestones: list[dict[str, Any]] | None = None,
    signature: str = "",
    npub: PatronNpub = "",
) -> dict[str, Any]:
    """Change a task you posted while nobody has claimed it. Give only what changes.

    Once someone claims it, its terms are what they agreed to and cannot change.
    Each value is checked as post_task checks it. Your signature on the old terms
    is cleared unless you sign the new ones.

    Args:
        task_id: The task.
        title, description, rubric, audience, needed_by, every_days, late, deposit_pct, cover, milestones:
            As in post_task.
        signature: Your signature on the changed offer, when your client makes one.
    """
    given = {"title": title, "description": description, "rubric": rubric, "audience": audience,
             "needed_by": needed_by, "every_days": every_days, "late": late, "deposit_pct": deposit_pct,
             "cover": cover, "milestones": milestones}
    changes = {k: v for k, v in given.items() if v is not None}
    db = await store()
    task = await _do(db.edit(runtime.resolve_npub(npub), str(task_id), changes, signature=signature,
                             escrow_mints=await escrow_mints()))
    return {"ok": True, "task": task}


@tool
@runtime.paid_tool(NUDGE_TASK_UUID)
async def nudge_task(task_id: str, kind: str, npub: PatronNpub = "") -> dict[str, Any]:
    """Send the other side of a task a reminder they see the next time they look.

    "pay": the performer of an owed task asks the purchaser to pay.
    "due": the purchaser tells the performer the deadline is near (within two days) or past.
    One of each kind per task every 12 hours. It changes nothing else about the task.

    Args:
        task_id: The task.
        kind: "pay" or "due".
    """
    db = await store()
    task = await _do(db.nudge(runtime.resolve_npub(npub), str(task_id), str(kind)))
    return {"ok": True, "task": task}


@tool
@runtime.paid_tool(LIST_TASKS_UUID)
async def list_tasks(site: str = "", npub: PatronNpub = "") -> dict[str, Any]:
    """Every task you posted, perform, grade, or may claim, and every task filed in your Organizations'
    Projects; soonest needed first. Each task's org_id and project_id say where it is filed.

    Args:
        site: Only that site's tasks and those posted with no site. Omit for every task.
    """
    return {"ok": True, "tasks": await _do((await store()).tasks_of(runtime.resolve_npub(npub), site=site))}


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

    A job whose escrow holds sats is settled first, with taskbooth_escrow_propose.

    Args:
        task_id: The task.
    """
    me = runtime.resolve_npub(npub)
    task, _ = await _do((await store()).escrow_task(me, str(task_id)))
    record = task.get("escrow") or {}
    if record.get("state") == escrow.INVOICED:
        state = str((await _mint_for(record["mint"]).mint_quote_state(record["funding"]["quote"])).get("state", ""))
        if state.upper() != "UNPAID":
            raise ValueError("The escrow invoice was paid. Fund the escrow with taskbooth_escrow_sign, "
                             "then settle it with taskbooth_escrow_propose.")
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


@tool
@runtime.paid_tool(REQUEST_DEPOSIT_UUID)
async def request_deposit(task_id: str, invoice: str = "", npub: PatronNpub = "") -> dict[str, Any]:
    """The Lightning invoice that pays a claimed task's deposit, for exactly the deposit's sats.

    The purchaser pays it from any wallet; the sats go straight to the performer.
    Asking again returns the same invoice until it expires, then a fresh one.
    The performer calls taskbooth_confirm_deposit once the sats arrive.

    The invoice is fetched from the performer's payout_address when they have
    one. A performer without one makes an invoice for exactly the deposit in
    their own wallet and passes it here as invoice.

    Args:
        task_id: A claimed task, with a deposit, that you posted or performed.
        invoice: The performer's own BOLT11 invoice for exactly the deposit. Only the performer passes one.
    """
    me = runtime.resolve_npub(npub)
    return {"ok": True, "deposit": await _advance_invoice(me, task_id, None, invoice)}


async def _advance_invoice(me: str, task_id: str, which: int | None, invoice: str) -> dict[str, Any]:
    db = await store()
    task = await _do(db.advance_task(me, str(task_id), which))
    if invoice and me != task["performer"]:
        raise ValueError("Only the performer may hand in their own invoice.")
    address = "" if invoice else await payout_address_of(task["performer"])
    return await payout.advance_invoice(db, task, which, address=address, invoice=invoice, requested_by=me)


@tool
@runtime.paid_tool(CONFIRM_DEPOSIT_UUID)
async def confirm_deposit(task_id: str, npub: PatronNpub = "") -> dict[str, Any]:
    """Say the deposit arrived. Only the performer, for a deposit invoiced here.

    Args:
        task_id: A claimed or done task you perform.
    """
    db = await store()
    task = await _do(db.confirm_advance(runtime.resolve_npub(npub), str(task_id)))
    return {"ok": True, "task": task}


@tool
@runtime.paid_tool(REQUEST_MILESTONE_UUID)
async def request_milestone(task_id: str, index: int, invoice: str = "", npub: PatronNpub = "") -> dict[str, Any]:
    """The Lightning invoice that pays one of a task's milestones, for exactly its sats, once it falls due.

    It works like taskbooth_request_deposit: the purchaser pays it from any wallet,
    the sats go straight to the performer, and asking again returns the same
    invoice until it expires. The performer calls taskbooth_confirm_milestone once
    the sats arrive.

    Args:
        task_id: A claimed or done task you posted or perform.
        index: Which milestone, counting from 0 in the task's milestones list.
        invoice: The performer's own BOLT11 invoice for exactly the milestone. Only the performer passes one.
    """
    me = runtime.resolve_npub(npub)
    return {"ok": True, "milestone": await _advance_invoice(me, task_id, index, invoice)}


@tool
@runtime.paid_tool(CONFIRM_MILESTONE_UUID)
async def confirm_milestone(task_id: str, index: int, npub: PatronNpub = "") -> dict[str, Any]:
    """Say a milestone's sats arrived. Only the performer, for a milestone invoiced here.

    Args:
        task_id: A claimed or done task you perform.
        index: Which milestone, counting from 0.
    """
    db = await store()
    task = await _do(db.confirm_advance(runtime.resolve_npub(npub), str(task_id), index))
    return {"ok": True, "task": task}


@tool
@runtime.paid_tool(OFFER_TERMS_UUID)
async def offer_terms(task_id: str, npub: PatronNpub = "") -> dict[str, Any]:
    """The offer both parties sign: the task's terms as one statement, its sha256, and the signatures so far.

    Args:
        task_id: The task.
    """
    db = await store()
    return {"ok": True, **await _do(db.offer(runtime.resolve_npub(npub), str(task_id)))}


@tool
@runtime.paid_tool(SIGN_OFFER_UUID)
async def sign_offer(task_id: str, statement_hash: str, npub: PatronNpub = "") -> dict[str, Any]:
    """Sign a task's offer with your own Nostr key, as a NIP-98 signed request.

    The request you sign carries statement_hash, so the signed request itself is
    your signature. The purchaser signs while the task is open; the performer
    signs once they have claimed it. Without your own key, use
    taskbooth_request_offer_signature instead.

    Args:
        task_id: The task.
        statement_hash: The sha256 taskbooth_offer_terms returned for the offer you read.
    """
    from tollbooth.tools.signature import signed_request_record

    me = runtime.resolve_npub(npub)
    record = signed_request_record(str(statement_hash))
    if record is None or record["signer"] != me:
        raise ValueError(
            "This call is not signed with your own key, so it cannot stand as your signature. "
            "Call taskbooth_request_offer_signature to sign by replying to a DM instead."
        )
    db = await store()
    task = await _do(db.sign(me, str(task_id), record, operator_npub=runtime.operator_npub()))
    return {"ok": True, "task": task}


@tool
@runtime.paid_tool(REQUEST_OFFER_SIGNATURE_UUID)
async def request_offer_signature(task_id: str, npub: PatronNpub = "") -> dict[str, Any]:
    """Have a task's offer sent to you as a Nostr DM, to sign by replying.

    After calling this, STOP and wait until you have replied to the DM from your
    Nostr client. Then call taskbooth_receive_offer_signature once, with the nonce
    and statement_hash returned here.

    Args:
        task_id: The task.
    """
    me = runtime.resolve_npub(npub)
    db = await store()
    offer = await _do(db.offer(me, str(task_id)))
    sent = await runtime.request_signature(me, offer["statement"])
    if not sent.get("success"):
        raise ValueError(sent.get("error") or "The signature request could not be sent.")
    return {"ok": True, "nonce": sent["nonce"], "statement_hash": sent["statement_hash"],
            "rendezvous_relay": sent.get("rendezvous_relay", ""), "message": sent.get("message", "")}


@tool
@runtime.paid_tool(RECEIVE_OFFER_SIGNATURE_UUID)
async def receive_offer_signature(
    task_id: str, nonce: str, statement_hash: str, npub: PatronNpub = "",
) -> dict[str, Any]:
    """Collect your signed reply to the offer DM and keep it as your signature. Call once.

    Args:
        task_id: The task.
        nonce: The nonce taskbooth_request_offer_signature returned.
        statement_hash: The statement_hash it returned.
    """
    me = runtime.resolve_npub(npub)
    got = await runtime.receive_signature(me, str(nonce), str(statement_hash))
    if not got.get("success"):
        raise ValueError(got.get("error") or "No signed reply was found.")
    db = await store()
    task = await _do(db.sign(me, str(task_id), got["signature"], operator_npub=runtime.operator_npub()))
    return {"ok": True, "task": task}


@tool
@runtime.paid_tool(ESCROW_INVOICE_UUID)
async def escrow_invoice(task_id: str, npub: PatronNpub = "") -> dict[str, Any]:
    """The Lightning invoice that puts a claimed job's Balance into escrow. Only the purchaser.

    The Balance is the top grade less the deposit and milestones. Pay the invoice
    from any wallet, then sign sign_digest with your Nostr key (BIP-340) and pass
    the signature to taskbooth_escrow_sign: the mint then locks the sats to the
    purchaser, performer and arbiter, any two of whom may move them, and after the
    refund date to the purchaser alone. Asking again returns the same invoice until
    it expires. Purchaser, performer and arbiter must all have signed the offer.

    Args:
        task_id: A claimed job whose cover page asks for escrow.
    """
    db = await store()
    me = runtime.resolve_npub(npub)
    return {"ok": True, "escrow": await _do(escrow.open_invoice(db, _mint_for, me, str(task_id), int(time.time())))}


@tool
@runtime.paid_tool(ESCROW_STATUS_UUID)
async def escrow_status(task_id: str, npub: PatronNpub = "") -> dict[str, Any]:
    """Where a job's escrow stands, and in for_you what you may sign now, each with its sign_digest.

    Sign a sign_digest with your own Nostr key as a BIP-340 Schnorr signature and
    pass it to taskbooth_escrow_sign.

    Args:
        task_id: The job.
    """
    db = await store()
    me = runtime.resolve_npub(npub)
    return {"ok": True, **await _do(escrow.status(db, me, str(task_id), int(time.time())))}


@tool
@runtime.paid_tool(ESCROW_PROPOSE_UUID)
async def escrow_propose(task_id: str, to_performer: int = -1, npub: PatronNpub = "") -> dict[str, Any]:
    """Propose how a job's escrow divides between the performer and the purchaser.

    The purchaser, the performer or the arbiter may propose; a new proposal
    replaces an unsigned one. It takes effect once two of the three sign it, or the
    purchaser alone after the refund date when it gives the performer nothing. Each
    share is then paid to its owner's payout address with taskbooth_escrow_collect.
    The mint's fee to move the escrow comes out of the total first.

    Args:
        task_id: The job.
        to_performer: The performer's sats; the purchaser gets the rest. Omit (or -1) to propose the
            award of a job with a final grade.
    """
    db = await store()
    me = runtime.resolve_npub(npub)
    share = None if to_performer == -1 else to_performer
    return {"ok": True, **await _do(escrow.propose(db, me, str(task_id), share, int(time.time())))}


@tool
@runtime.paid_tool(ESCROW_COLLECT_UUID)
async def escrow_collect(task_id: str, npub: PatronNpub = "") -> dict[str, Any]:
    """Ready your share of a job's escrow to be paid to your payout address.

    Fetches an invoice from your payout_address for your share less the mint's
    Lightning fee reserve, and returns the sign_digest to sign. Pass that
    signature to taskbooth_escrow_sign and the mint pays the invoice. Whatever of
    the reserve Lightning does not use stays here for you to collect later.

    Args:
        task_id: A job whose escrow has been split.
    """
    db = await store()
    me = runtime.resolve_npub(npub)
    address = await payout_address_of(me)
    return {"ok": True, **await _do(escrow.collect(db, _mint_for, _invoice_for, me, str(task_id), address,
                                                   int(time.time())))}


@tool
@runtime.paid_tool(ESCROW_SIGN_UUID)
async def escrow_sign(task_id: str, signature: str, npub: PatronNpub = "") -> dict[str, Any]:
    """Sign what a job's escrow is waiting on from you, and carry it out once enough have signed.

    In order: your payout from taskbooth_escrow_collect, funding the escrow (the
    purchaser, once the invoice is paid), or a proposed split. The signature is
    BIP-340 Schnorr by your Nostr key over the sign_digest that
    taskbooth_escrow_status lists for you.

    Args:
        task_id: The job.
        signature: 128 hex characters.
    """
    db = await store()
    me = runtime.resolve_npub(npub)
    return {"ok": True, **await _do(escrow.sign(db, _mint_for, me, str(task_id), signature, int(time.time())))}


@tool
@runtime.paid_tool(STANDARD_TERMS_UUID)
async def standard_terms(version: str = "") -> dict[str, Any]:
    """The Task Booth Standard Terms: their text, and the sha256 a cover page and a signature name.

    Args:
        version: A version such as "v0". Empty for the current one.
    """
    return {"ok": True, "version": version or terms.CURRENT, "hash": terms.terms_hash(version),
            "text": terms.text(version), "versions": sorted(terms.VERSIONS)}


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
# Organizations and Projects
# ---------------------------------------------------------------------------


@tool
@runtime.paid_tool(SAVE_ORGANIZATION_UUID)
async def save_organization(name: str, about: str = "", org_id: str = "", npub: PatronNpub = "") -> dict[str, Any]:
    """Make an Organization to file tasks under, or change one you own.

    Its members see its Projects and every task filed in them, and may post
    tasks into them. Only you, its owner, change it, its members or its Projects.

    Args:
        name: What it is called, e.g. "The New Library".
        about: A sentence or two about it.
        org_id: One of yours to change. Omit to make a new one.
    """
    db = await store()
    saved = await _do(db.save_org(runtime.resolve_npub(npub), name, about, str(org_id)))
    return {"ok": True, "org_id": saved}


@tool
@runtime.paid_tool(LIST_ORGANIZATIONS_UUID)
async def list_organizations(npub: PatronNpub = "") -> dict[str, Any]:
    """The Organizations you own or belong to, each with your place in it: "owner" or "member"."""
    return {"ok": True, "organizations": await (await store()).orgs_of(runtime.resolve_npub(npub))}


@tool
@runtime.paid_tool(GET_ORGANIZATION_UUID)
async def get_organization(org_id: str, npub: PatronNpub = "") -> dict[str, Any]:
    """An Organization you own or belong to, with its members and its Projects.

    Args:
        org_id: The Organization.
    """
    db = await store()
    return {"ok": True, "organization": await _do(db.org(runtime.resolve_npub(npub), str(org_id)))}


@tool
@runtime.paid_tool(SAVE_MEMBER_UUID)
async def save_member(org_id: str, member: str, label: str = "", npub: PatronNpub = "") -> dict[str, Any]:
    """Add an npub to an Organization you own, or change their label.

    Args:
        org_id: Your Organization.
        member: The npub to add.
        label: What to call them, e.g. "Head librarian".
    """
    db = await store()
    await _do(db.save_member(runtime.resolve_npub(npub), str(org_id), member, label))
    return {"ok": True}


@tool
@runtime.paid_tool(REMOVE_MEMBER_UUID)
async def remove_member(org_id: str, member: str, npub: PatronNpub = "") -> dict[str, Any]:
    """Take an npub out of an Organization you own. They stop seeing it and its tasks at once.

    Tasks they posted stay theirs.

    Args:
        org_id: Your Organization.
        member: The npub to take out.
    """
    db = await store()
    if not await _do(db.remove_member(runtime.resolve_npub(npub), str(org_id), str(member))):
        raise ValueError("That npub is not a member of the Organization.")
    return {"ok": True}


@tool
@runtime.paid_tool(SAVE_PROJECT_UUID)
async def save_project(
    org_id: str,
    name: str,
    brief: str = "",
    budget_sats: int = 0,
    circle: list[str] | None = None,
    defaults: dict[str, Any] | None = None,
    project_id: str = "",
    npub: PatronNpub = "",
) -> dict[str, Any]:
    """Make a Project in an Organization you own, or change one. Give every field when changing it.

    A Project suggests what a new task in it starts from; the site fills a new
    task's form from these, and nothing is applied to a task you did not post.

    Args:
        org_id: Your Organization.
        name: What the Project is called, e.g. "Portraits for the reading room".
        brief: What the Project is for.
        budget_sats: What it may spend in all, in sats. 0 for no budget.
        circle: The npubs invited to each new task in it.
        defaults: Default cover terms: {"deemed_acceptance_days": 14, "ip_option": "assign" | "license" |
            "worker_keeps" | "custom", "ip_custom_text": "…", "confidential": false, "client_is_consumer": false,
            "third_party_allowed": false, "governing_law": "…", "venue": "…"}. Every field is optional.
        project_id: One of the Organization's Projects to change. Omit to make a new one.
    """
    db = await store()
    saved = await _do(db.save_project(runtime.resolve_npub(npub), str(org_id), name, brief, budget_sats,
                                      circle, defaults, str(project_id)))
    return {"ok": True, "project_id": saved}


@tool
@runtime.paid_tool(ARCHIVE_PROJECT_UUID)
async def archive_project(org_id: str, project_id: str, archived: bool = True, npub: PatronNpub = "") -> dict[str, Any]:
    """Close a Project in an Organization you own to new tasks, or open it again. Its tasks go on as they are.

    Args:
        org_id: Your Organization.
        project_id: The Project.
        archived: True to close it, false to open it again.
    """
    db = await store()
    await _do(db.archive_project(runtime.resolve_npub(npub), str(org_id), str(project_id), archived))
    return {"ok": True}


@tool
@runtime.paid_tool(GET_PROJECT_UUID)
async def get_project(project_id: str, npub: PatronNpub = "") -> dict[str, Any]:
    """A Project in an Organization you own or belong to, with what its tasks have committed and paid.

    totals.committed_sats: the top grade of each task still open or under way, what each graded task
    earned, and what was paid ahead on any that closed ungraded. totals.paid_sats: what the performers
    confirmed arrived. totals.by_standing counts its tasks by where they stand.

    Args:
        project_id: The Project.
    """
    db = await store()
    return {"ok": True, "project": await _do(db.project(runtime.resolve_npub(npub), str(project_id)))}


# ---------------------------------------------------------------------------
# Calendar feeds
# ---------------------------------------------------------------------------


@tool
@runtime.paid_tool(CALENDAR_PUBLISH_UUID)
async def calendar_publish(name: str, time_zone: str, site: str = "", npub: PatronNpub = "") -> dict[str, Any]:
    """Mint a calendar feed of your tasks that a phone or computer can subscribe to.

    Returns a token. The site you use serves the feed at an address built from
    it. The token is the feed's only lock: anyone who has the address can read
    your tasks' titles, details, due days and rewards. The feed names nobody
    and carries no npub. taskbooth_calendar_revoke stops it.

    Args:
        name: What the calendar is called in the calendar app, e.g. "Sam's chores".
        time_zone: Your IANA time zone, e.g. "America/New_York". It decides which day a task falls on.
        site: Only that site's tasks and those posted with no site, as taskbooth_list_tasks. Omit for every task.
    """
    db = await store()
    return {"ok": True, "feed": await db.publish_feed(runtime.resolve_npub(npub), name, time_zone, site=site)}


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
    tasks = await db.tasks_of(found["owner"], site=found.get("site", ""))
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
