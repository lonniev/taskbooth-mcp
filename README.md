# Task Queue

A Tollbooth DPYC™ Operator where work is posted with a signed rubric and paid in sats after it is graded.

A purchaser posts a task with a rubric: the grades, and the sats each grade earns.
A performer claims it, does it, and marks it done.
The purchaser checks the work in the real world and picks a grade.
The sats go from the purchaser's own wallet to the performer's, over Nostr Wallet Connect.
This Operator never holds them.

Allowance, for household chores, is the first instance.

## Status

This version serves the standard DPYC tools only, under the `taskqueue_` prefix.
The Task Queue's own tools arrive in later releases.

## Run locally

```
uv sync
TOLLBOOTH_NOSTR_OPERATOR_NSEC=… uv run python -m taskqueue_mcp.server
```

## Test

```
uv run ruff check .
uv run pytest
```

## Deploy

The service runs on Prefect Horizon as the project `taskqueue-mcp`.
Horizon builds from `fastmcp.json` and needs one secret, `TOLLBOOTH_NOSTR_OPERATOR_NSEC`.
The Authority provisions the Neon database when it approves the adoption request.
The BTCPay credentials arrive by Secure Courier: call `taskqueue_request_credential_channel`.

## License

Apache-2.0
