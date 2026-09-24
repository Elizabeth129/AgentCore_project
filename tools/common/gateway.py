"""Glue between an AgentCore Gateway Lambda target and a plain Python function.

A Gateway Lambda target invokes the function with the tool's arguments as the
event body, and names the tool in the Lambda client context under
`bedrockAgentCoreToolName`. The name is only used for logging here: each of our
targets exposes exactly one tool, so there is nothing to dispatch on.
"""

from __future__ import annotations

from typing import Any, Callable

from . import errors, logging_json as jlog


def tool_name(context: Any) -> str | None:
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    return custom.get("bedrockAgentCoreToolName")


def arguments(event: Any) -> dict[str, Any]:
    """The tool arguments, tolerating the common envelope shapes."""
    if not isinstance(event, dict):
        raise errors.ToolError(errors.INVALID_INPUT, "Tool arguments must be a JSON object.")
    for envelope in ("arguments", "input", "body"):
        inner = event.get(envelope)
        if isinstance(inner, dict):
            return inner
    return event


def handler(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[Any, Any], dict[str, Any]]:
    """Wrap a tool function so every outcome is a structured result.

    An unhandled exception would otherwise reach the Gateway as an opaque
    Lambda failure; here it becomes a retryable INTERNAL_ERROR that the agent
    can explain and a trace can be searched for.
    """

    def lambda_handler(event: Any, context: Any) -> dict[str, Any]:
        name = tool_name(context)
        try:
            args = arguments(event)
            result = fn(args)
        except errors.ToolError as exc:
            jlog.warn("tool_error", tool=name, code=exc.code, message=exc.message)
            return exc.as_result()
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all at the boundary
            jlog.error("tool_unhandled_exception", tool=name, error_type=type(exc).__name__)
            return errors.err(
                errors.INTERNAL_ERROR,
                "The tool failed unexpectedly.",
                retryable=True,
            )
        if result.get("status") == "error":
            jlog.warn("tool_error", tool=name, code=result.get("code"))
        return result

    return lambda_handler
