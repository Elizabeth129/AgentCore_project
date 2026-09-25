"""Custom span attributes.

Strands already emits the span *skeleton* — one `invoke_agent` span per turn,
a `chat`/model span per model call, an `execute_tool <name>` span per tool call.
What it cannot know is our domain: which order, which customer, which refund,
which error code. Without those, a trace tells you *that* a tool call failed but
not *what* it was trying to do, and you end up correlating by timestamp.

Everything here writes onto the span the caller is already inside, so a tool
wrapper's attributes land on that tool's `execute_tool` span and are searchable
in Transaction Search (`aws/spans`) alongside the built-in `gen_ai.*` fields.

Attribute names follow CLAUDE.md §9. Nothing sensitive is recorded: identifiers
only, never tokens, card data, or PII beyond the IDs.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace

# Domain attribute names, in one place so the queries in docs/observability.md
# and the code that writes them cannot drift apart.
TOOL_NAME = "tool.name"
TOOL_OUTCOME = "tool.outcome"
TOOL_ATTEMPTS = "tool.attempts"
TOOL_DUPLICATE = "tool.duplicate"
ERROR_CODE = "error.code"
ERROR_RETRYABLE = "error.retryable"
CUSTOMER_ID = "customer_id"
ORDER_ID = "order_id"
ACTOR_ID = "actor_id"
OPERATION_ID = "operation_id"
SESSION_ID = "session.id"
REFUND_AMOUNT = "refund.amount"
REFUND_ID = "refund.id"
REFUND_IDEMPOTENCY_KEY = "refund.idempotency_key"
POLICY_DECISION = "policy.decision"
TOOL_CALLS_IN_TURN = "loop.tool_calls"
LOOP_BUDGET_EXCEEDED = "loop.budget_exceeded"


def set_attributes(**attributes: Any) -> None:
    """Attach attributes to the current span, skipping any that are None."""
    span = trace.get_current_span()
    if not span.is_recording():
        return
    for key, value in attributes.items():
        if value is None:
            continue
        # OTEL accepts str/bool/int/float and sequences of those; anything else
        # is stringified rather than silently dropped by the exporter.
        if not isinstance(value, (str, bool, int, float)):
            value = str(value)
        span.set_attribute(key, value)


def record_tool_result(
    *,
    tool_name: str,
    result: dict[str, Any],
    attempts: int | None = None,
) -> None:
    """Mark the current tool span with the outcome, and fail it on error.

    Setting the span status is what makes the failure show up as an error in the
    GenAI Observability view and in X-Ray's error counts — an attribute alone
    leaves the span looking successful.
    """
    status = result.get("status")
    set_attributes(
        **{
            TOOL_NAME: tool_name,
            TOOL_OUTCOME: status,
            TOOL_ATTEMPTS: attempts,
            ERROR_CODE: result.get("code"),
            ERROR_RETRYABLE: result.get("retryable"),
            REFUND_ID: result.get("refund_id"),
            TOOL_DUPLICATE: result.get("duplicate"),
        }
    )

    if status == "error":
        span = trace.get_current_span()
        if span.is_recording():
            span.set_status(
                trace.Status(
                    trace.StatusCode.ERROR,
                    f"{result.get('code', 'UNKNOWN')}: {result.get('message', '')}"[:300],
                )
            )
