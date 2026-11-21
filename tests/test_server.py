"""The bootstrap as a caller meets it, through FastMCP's in-memory client."""

from fastmcp import Client

from taskbooth_mcp import __version__, server


async def tool_names() -> set[str]:
    async with Client(server.mcp) as c:
        return {t.name for t in await c.list_tools()}


async def test_standard_tools_are_served_under_the_taskbooth_prefix():
    names = await tool_names()
    for standard in ("service_status", "check_balance", "purchase_credits", "check_price",
                     "request_credential_channel", "get_operator_onboarding_status"):
        assert f"taskbooth_{standard}" in names
    assert all(n.startswith("taskbooth_") for n in names)


async def test_every_served_tool_is_in_the_registry():
    """A tool the runtime cannot price is a tool nobody can pay for."""
    registered = {ti.capability for ti in server.runtime._tool_registry.values()}
    names = await tool_names()
    assert names
    assert {n.removeprefix("taskbooth_") for n in names} <= registered


def test_domain_tool_ids_are_unique_uuids():
    ids = [ti.tool_id for ti in server._DOMAIN_TOOLS]
    assert len(ids) == len(set(ids))
    assert set(ids).isdisjoint(server.STANDARD_IDENTITIES)


def test_operator_credentials_are_all_sensitive():
    """BTCPay secrets and the LLM key must never be echoed back in a tool response."""
    fields = server.runtime._operator_credential_template.fields
    assert set(fields) == {"btcpay_host", "btcpay_api_key", "btcpay_store_id", "llm_api_key"}
    for name, spec in fields.items():
        assert spec.sensitive, name


def test_version_is_one_value():
    assert server.mcp.version == __version__


def test_the_package_says_the_version_pyproject_releases():
    # A release bumps pyproject.toml; this keeps the version the service reports from staying behind.
    import tomllib
    from pathlib import Path

    released = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]["version"]
    assert __version__ == released
