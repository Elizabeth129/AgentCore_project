"""Classifying an MCP result decides whether a failure gets retried.

Getting this wrong is expensive in both directions: a transient fault marked
permanent loses a recoverable call, and a permanent fault marked transient burns
three attempts and three times the latency before reporting the same thing.
"""

from __future__ import annotations

import json

import pytest

import agent as A


def mcp_result(text: str | None = None, *, is_error: bool = False, status: str = "success") -> dict:
    content = [{"text": text}] if text is not None else []
    return {"status": status, "content": content, "isError": is_error}


def test_lambda_structured_result_passes_through():
    body = {"status": "success", "order_id": "ORD-1001", "total_cents": 24999}
    assert A._unwrap(mcp_result(json.dumps(body)), tool_label="get_order") == body


def test_lambda_business_error_keeps_its_own_classification():
    body = {"status": "error", "code": "ORDER_NOT_FOUND", "message": "no", "retryable": False}
    out = A._unwrap(mcp_result(json.dumps(body), is_error=False), tool_label="get_order")
    assert out["code"] == "ORDER_NOT_FOUND"
    assert out["retryable"] is False


def test_lambda_retryable_error_stays_retryable():
    body = {"status": "error", "code": "DEPENDENCY_UNAVAILABLE", "retryable": True}
    assert A._unwrap(mcp_result(json.dumps(body)), tool_label="get_order")["retryable"] is True


@pytest.mark.parametrize(
    "detail",
    [
        "ValidationException - Parameter validation failed: Invalid request parameters:\n"
        "- Field '/amount_cents' has invalid type: string found, integer expected",
        "ValidationException - Parameter validation failed: Invalid request parameters:\n"
        "- Missing required field(s): 'idempotency_key'",
    ],
)
def test_gateway_schema_rejection_is_not_retryable(detail):
    """The Gateway rejects bad arguments before the Lambda runs. Identical
    arguments will be rejected identically, so retrying is pure waste."""
    out = A._unwrap(mcp_result(detail, is_error=True), tool_label="process_refund")
    assert out["code"] == "TOOL_INVALID_ARGUMENTS"
    assert out["retryable"] is False
    assert "amount_cents" in out["message"] or "idempotency_key" in out["message"], (
        "the model needs to know which field to fix"
    )


def test_transport_failure_is_retryable():
    out = A._unwrap(mcp_result("connection reset by peer", is_error=True), tool_label="get_order")
    assert out["code"] == "TOOL_TRANSPORT_ERROR"
    assert out["retryable"] is True


def test_unreadable_success_is_not_retried():
    out = A._unwrap(mcp_result("this is not json"), tool_label="get_order")
    assert out["code"] == "TOOL_RESULT_UNREADABLE"
    assert out["retryable"] is False


def test_empty_content_is_not_retried():
    out = A._unwrap(mcp_result(), tool_label="get_order")
    assert out["code"] == "TOOL_RESULT_UNREADABLE"
    assert out["retryable"] is False


def test_json_without_status_is_not_mistaken_for_a_tool_result():
    out = A._unwrap(mcp_result(json.dumps({"order_id": "ORD-1"})), tool_label="get_order")
    assert out["code"] == "TOOL_RESULT_UNREADABLE"
