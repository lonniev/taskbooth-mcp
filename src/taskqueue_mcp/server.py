"""Task Queue — work posted with a signed rubric, paid in sats after it is graded.

A purchaser posts a task with a rubric: the grades, and the sats each grade earns.
A performer (a person or an agent, any npub) claims it, does it, and marks it done.
The purchaser checks the work in the real world and picks a grade. The sats go
from the purchaser's own wallet to the performer's, and this Operator never holds
them.

This module is the bootstrap: the standard DPYC tools (check_balance,
purchase_credits, Secure Courier, Oracle, pricing, peer grants) come from
``register_standard_tools`` in the tollbooth-dpyc wheel. The Task Queue's own
tools join ``_DOMAIN_TOOLS`` as they are built.

Run locally:
    python -m taskqueue_mcp.server
"""

from __future__ import annotations

import logging

from fastmcp import FastMCP
from mcp.types import Icon
from tollbooth.credential_templates import CredentialTemplate, FieldSpec
from tollbooth.credential_validators import validate_btcpay_creds
from tollbooth.runtime import OperatorRuntime, register_standard_tools
from tollbooth.tool_identity import STANDARD_IDENTITIES, ToolIdentity

from taskqueue_mcp import __version__

logger = logging.getLogger(__name__)

#: The Horizon project's own URL. The holder connection's OAuth endpoints live here.
SERVICE_URL = "https://taskqueue-mcp.fastmcp.app"

mcp = FastMCP(
    "taskqueue-mcp",
    version=__version__,
    icons=[Icon(src="https://tollbooth-dpyc.com/dpyc-logo.png", mimeType="image/png")],
    instructions=(
        "Task Queue — work posted with a signed rubric and paid in sats after it "
        "is graded. Monetized via Tollbooth DPYC Bitcoin Lightning micropayments.\n\n"
        "## How it works\n"
        "A purchaser posts a task with a rubric: the grades, and the sats each "
        "grade earns. A performer claims it, does it, and marks it done. The "
        "purchaser checks the work and picks a grade, and the sats go from the "
        "purchaser's own wallet to the performer's. This Operator never holds them.\n\n"
        "## Operator onboarding\n"
        "Call taskqueue_get_operator_onboarding_status to check readiness.\n"
        "1. Register with an Authority (provides a Neon database automatically)\n"
        "2. Deliver operator secrets via Secure Courier: btcpay_host, "
        "btcpay_api_key, btcpay_store_id. Call taskqueue_request_credential_channel to start.\n\n"
        "## Pricing\n"
        "Tool prices are set by the operator's pricing model. Use "
        "`taskqueue_check_price` to preview and `taskqueue_check_balance` to see your balance."
    ),
)

# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

#: The Task Queue's own tools, each with a frozen UUID minted once at its birth.
_DOMAIN_TOOLS: list[ToolIdentity] = []

TOOL_REGISTRY: dict[str, ToolIdentity] = {ti.tool_id: ti for ti in _DOMAIN_TOOLS}

runtime = OperatorRuntime(
    tool_registry={**STANDARD_IDENTITIES, **TOOL_REGISTRY},
    operator_credential_template=CredentialTemplate(
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
        "Hi — I'm Task Queue, an MCP service where work is posted with a signed "
        "rubric and paid in sats after it is graded. You (or your AI agent) "
        "requested a credential channel."
    ),
    service_name="Task Queue",
    credential_validator=validate_btcpay_creds,
    holder_auth_url=SERVICE_URL,
)

tool = register_standard_tools(
    mcp,
    "taskqueue",
    runtime,
    service_name="taskqueue",
    service_version=__version__,
)


if __name__ == "__main__":
    mcp.run()
