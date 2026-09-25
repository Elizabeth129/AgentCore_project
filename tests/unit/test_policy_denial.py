"""A Cedar denial must be reported, not retried, and not worked around.

Retrying a refused refund would be wrong three times over: it cannot succeed,
it triples the audit noise, and it is the behaviour an attacker probing the
ceiling would hope for.
"""

from __future__ import annotations

import agent as A
from telemetry import policy_decision

# What the Gateway actually returns when Cedar refuses a call, verbatim.
DENIAL = (
    "Tool execution failed: Tool Execution Denied: Tool call not allowed due to "
    "policy enforcement [No policy applies to the request (denied by default).]"
)
EXPLICIT_FORBID = (
    "Tool Execution Denied: Tool call not allowed due to policy enforcement "
    "[Explicit forbid matched the request.]"
)


def mcp_error(text: str) -> dict:
    return {"status": "error", "content": [{"text": text}], "isError": True}


def test_default_deny_is_recognised_as_a_policy_decision():
    out = A._unwrap(mcp_error(DENIAL), tool_label="process_refund")
    assert out["code"] == "POLICY_DENIED"
    assert out["retryable"] is False


def test_explicit_forbid_is_recognised_too():
    out = A._unwrap(mcp_error(EXPLICIT_FORBID), tool_label="process_refund")
    assert out["code"] == "POLICY_DENIED"
    assert out["retryable"] is False


def test_denial_is_not_mistaken_for_a_transport_fault():
    """The distinction that matters: a dropped connection is retryable, a refusal
    is not, and both arrive as an MCP error with text."""
    denied = A._unwrap(mcp_error(DENIAL), tool_label="process_refund")
    dropped = A._unwrap(mcp_error("connection reset by peer"), tool_label="process_refund")
    assert denied["retryable"] is False
    assert dropped["retryable"] is True
    assert denied["code"] != dropped["code"]


def test_denial_message_tells_the_model_not_to_retry_or_work_around():
    message = A._unwrap(mcp_error(DENIAL), tool_label="process_refund")["message"]
    assert "not something to retry" in message
    assert "human approval" in message


def test_denial_detail_is_preserved_for_the_trace():
    out = A._unwrap(mcp_error(DENIAL), tool_label="process_refund")
    assert "policy enforcement" in out["policy_detail"]


def test_policy_decision_attribution():
    assert policy_decision("error", "POLICY_DENIED") == "DENY_GATEWAY_POLICY"
    # The Lambda caught it, which means Cedar did not — correct outcome, but a
    # different state, and the trace must not blur the two.
    assert policy_decision("error", "REFUND_LIMIT_EXCEEDED") == "DENY_TOOL_VALIDATION"
    assert policy_decision("success", None) == "ALLOW"
    assert policy_decision("error", "ORDER_NOT_FOUND") is None
