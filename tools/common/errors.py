"""Structured tool results.

Every tool returns one of these two shapes so the agent, and anyone reading a
trace, can tell a business outcome from a transient fault without parsing prose:

    {"status": "success", ...}
    {"status": "error", "code": ..., "message": ..., "retryable": bool}

`retryable` is the contract with the agent: true means "the same call may work
if repeated", false means "repeating this will fail the same way".
"""

from __future__ import annotations

from typing import Any

# Business errors — never retryable.
ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
CUSTOMER_NOT_FOUND = "CUSTOMER_NOT_FOUND"
INVALID_INPUT = "INVALID_INPUT"
INVALID_AMOUNT = "INVALID_AMOUNT"
AMOUNT_EXCEEDS_ORDER_TOTAL = "AMOUNT_EXCEEDS_ORDER_TOTAL"
REFUND_LIMIT_EXCEEDED = "REFUND_LIMIT_EXCEEDED"

# Infrastructure errors — the agent may retry.
DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"

# Anything we did not anticipate.
INTERNAL_ERROR = "INTERNAL_ERROR"


def ok(**fields: Any) -> dict[str, Any]:
    return {"status": "success", **fields}


def err(code: str, message: str, *, retryable: bool = False, **fields: Any) -> dict[str, Any]:
    return {"status": "error", "code": code, "message": message, "retryable": retryable, **fields}


class ToolError(Exception):
    """Raised inside a handler to short-circuit to a structured error result."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_result(self) -> dict[str, Any]:
        return err(self.code, self.message, retryable=self.retryable)
