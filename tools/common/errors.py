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


#

# Keys the envelope owns. A payload field of the same name would silently
# overwrite it — which is exactly what happened when `get_order` splatted an
# order record whose own `status` was "DELAYED": callers saw `status: "DELAYED"`
# instead of `status: "success"`, so nothing downstream could tell a successful
# lookup from a failed one. Renaming the payload field is the fix; this guard is
# what stops the next one being silent.
RESERVED_FIELDS = frozenset({"status", "code", "message", "retryable"})


def ok(**fields: Any) -> dict[str, Any]:
    clashes = RESERVED_FIELDS & fields.keys()
    if clashes:
        raise ValueError(
            f"payload field(s) {sorted(clashes)} would overwrite the result envelope; "
            "rename them in the handler (e.g. status -> order_status)"
        )
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
