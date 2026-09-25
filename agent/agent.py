"""Customer Support Agent — Strands agent behind an AgentCore Runtime entrypoint.

The three business tools live behind the AgentCore Gateway, backed by Lambda and
DynamoDB. The model does not see the Gateway's tools directly; it sees the three
wrappers below. That indirection is what lets every call carry:

  * **retry with exponential backoff and jitter** for transient failures only
    (`agent/retry.py`) — business errors are reported, not repeated;
  * **a timeout** shorter than the Runtime's request budget;
  * for refunds, **an idempotency key the model cannot choose**
    (`agent/idempotency.py`), stable across both in-turn retries and whole
    re-invocations of the agent.

Conversation state lives in AgentCore Memory (`agent/memory.py`), keyed on the
actor and the session.

Invoke payload: `{"prompt": "..."}`, with a Cognito JWT in the Authorization
header. The customer comes from that token's `custom:customer_id` claim
(`agent/identity.py`). An optional
`X-Amzn-Bedrock-AgentCore-Runtime-Custom-Operation-Id` header makes a whole
invocation retry-safe.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections import OrderedDict
from contextvars import ContextVar
from datetime import timedelta
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models.bedrock import BedrockModel
from strands.types.exceptions import MCPClientInitializationError

from gateway_client import build_gateway_client
from identity import IdentityError, actor_id_from
from idempotency import (
    current_operation_id,
    current_session_id,
    derive_idempotency_key,
    idempotency_scope,
)
from memory import build_session_manager
from prompts import SYSTEM_PROMPT
from retry import call_with_retry
import telemetry as tel

app = BedrockAgentCoreApp()
log = app.logger

MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-5-20250929-v1:0")

# Gateway tools are named `<targetName>___<toolName>`.
ORDER_TOOL = os.environ.get("ORDER_TOOL_NAME", "orders___get_order")
CUSTOMER_TOOL = os.environ.get("CUSTOMER_TOOL_NAME", "customers___get_customer")
REFUND_TOOL = os.environ.get("REFUND_TOOL_NAME", "refunds___process_refund")

# Longer than the Lambdas' own 10s timeout, so a slow tool surfaces its own
# error rather than being cut off here, and still far inside the Runtime's
# request budget even after three attempts.
TOOL_TIMEOUT = timedelta(seconds=15)

OPERATION_ID_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Operation-Id"

MAX_CACHED_SESSIONS = 128

# A ceiling on tool calls per invocation. A model that has lost the thread can
# call the same tool indefinitely; retries multiply it, and nothing downstream
# stops it because each individual call is legitimate. This bounds the damage and,
# more usefully, makes the loop *nameable* in a trace instead of just slow.
# Generous for real work: the busiest legitimate turn we have uses four.
TOOL_CALL_BUDGET = int(os.environ.get("TOOL_CALL_BUDGET", "12"))


class _TurnCounter:
    """Shared tool-call tally for one invocation.

    This has to be a mutable object rather than an int in the ContextVar.
    Strands runs sync tool functions on worker threads, and a thread gets a
    *copy* of the context — so incrementing an int would increment each copy
    separately and every call would see 1. Copies share this object by
    reference, so the count actually accumulates.
    """

    __slots__ = ("tool_calls",)

    def __init__(self) -> None:
        self.tool_calls = 0


# Reset per invocation, so one turn's budget cannot be spent by the previous one.
_turn: ContextVar[_TurnCounter] = ContextVar("_turn")


def _current_turn() -> _TurnCounter:
    try:
        return _turn.get()
    except LookupError:
        # Only reached outside an invocation, e.g. a local script.
        counter = _TurnCounter()
        _turn.set(counter)
        return counter

gateway = build_gateway_client()

# The MCP session is started on first use and shared by the process. Strands is
# not managing its lifecycle for us, because none of its tools are exposed.
_gateway_started = False
_gateway_lock = threading.Lock()


def _ensure_gateway_started() -> None:
    global _gateway_started
    with _gateway_lock:
        if _gateway_started:
            return
        try:
            gateway.start()
        except MCPClientInitializationError:
            # Already running — a concurrent turn won the race.
            pass
        _gateway_started = True


def _mark_gateway_stopped() -> None:
    global _gateway_started
    with _gateway_lock:
        _gateway_started = False


def _unwrap(result: Any, *, tool_label: str) -> dict[str, Any]:
    """Turn an MCP tool result into the structured dict the Lambda returned.

    Strands does not raise on a transport failure — it returns an error result
    whose content is a message rather than our JSON. Telling those two apart is
    what makes retry classification correct: a Lambda saying `ORDER_NOT_FOUND`
    must not be retried, while a connection that dropped must be.
    """
    for block in result.get("content") or []:
        text = block.get("text") if isinstance(block, dict) else None
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict) and "status" in parsed:
            return parsed

    if result.get("isError") or result.get("status") == "error":
        detail = " ".join(
            block.get("text", "")
            for block in result.get("content") or []
            if isinstance(block, dict)
        )
        # The Gateway validates arguments against the tool schema before the
        # Lambda runs, so a type error or a missing required field comes back
        # here rather than as a tool result. Those are permanent: the same
        # arguments will be rejected identically, and retrying them three times
        # just delays telling the model to fix its call.
        if "ValidationException" in detail or "Parameter validation failed" in detail:
            return {
                "status": "error",
                "code": "TOOL_INVALID_ARGUMENTS",
                "message": f"{tool_label} rejected the arguments: {detail[:300]}",
                "retryable": False,
            }
        return {
            "status": "error",
            "code": "TOOL_TRANSPORT_ERROR",
            "message": f"{tool_label} could not be reached.",
            "retryable": True,
        }

    return {
        "status": "error",
        "code": "TOOL_RESULT_UNREADABLE",
        "message": f"{tool_label} returned a response that could not be read.",
        "retryable": False,
    }


def _log_attempt(*, name: str, attempt: int, retrying: bool, error: BaseException | None) -> None:
    if error is not None:
        log.warning("tool_attempt tool=%s attempt=%d error=%s", name, attempt, type(error).__name__)
    elif retrying:
        log.warning("tool_attempt tool=%s attempt=%d outcome=retrying", name, attempt)
    else:
        log.info("tool_attempt tool=%s attempt=%d outcome=final", name, attempt)


def _spend_budget(label: str) -> dict[str, Any] | None:
    """Count this tool call; refuse it once the turn's budget is gone."""
    counter = _current_turn()
    counter.tool_calls += 1
    used = counter.tool_calls
    tel.set_attributes(**{tel.TOOL_CALLS_IN_TURN: used})

    if used <= TOOL_CALL_BUDGET:
        return None

    log.error(
        "tool_call_budget_exceeded tool=%s calls=%d budget=%d",
        label,
        used,
        TOOL_CALL_BUDGET,
    )
    tel.set_attributes(**{tel.LOOP_BUDGET_EXCEEDED: True})
    # Not retryable, and phrased so the model stops rather than rephrasing.
    return {
        "status": "error",
        "code": "TOOL_CALL_BUDGET_EXCEEDED",
        "message": (
            f"This conversation turn has already used {TOOL_CALL_BUDGET} tool calls. "
            "Stop calling tools and answer the customer with what you already have, "
            "or tell them you could not complete the request."
        ),
        "retryable": False,
    }


def _call_gateway(tool_name: str, arguments: dict[str, Any], *, label: str) -> dict[str, Any]:
    """Call one Gateway tool, retrying only what is worth retrying."""
    refused = _spend_budget(label)
    if refused is not None:
        tel.record_tool_result(tool_name=label, result=refused)
        return refused

    attempts_made = 0

    def attempt() -> dict[str, Any]:
        nonlocal attempts_made
        attempts_made += 1
        try:
            _ensure_gateway_started()
            raw = gateway.call_tool_sync(
                str(uuid.uuid4()), tool_name, arguments, read_timeout_seconds=TOOL_TIMEOUT
            )
        except MCPClientInitializationError:
            # The session died. Drop the flag so the next attempt reconnects.
            _mark_gateway_stopped()
            raise
        return _unwrap(raw, tool_label=label)

    result = call_with_retry(
        attempt,
        name=label,
        retryable_exceptions=(MCPClientInitializationError,),
        on_attempt=_log_attempt,
    )
    tel.record_tool_result(tool_name=label, result=result, attempts=attempts_made)
    return result


@tool
def get_order(order_id: str) -> dict[str, Any]:
    """Look up a single order: its status, why it is in that status, the
    expected delivery date, the items, and the total paid.

    Use this for any question about an order, including where it is, why it is
    late, and what was paid.

    Args:
        order_id: Order identifier in the form `ORD-<digits>`, e.g. `ORD-1001`.
            If the customer says a bare number, prefix it with `ORD-`.

    Returns:
        On success the order record, with `total_cents` as an integer number of
        US cents (24999 means $249.99). On failure `status: "error"` with a
        `code` such as `ORDER_NOT_FOUND`.
    """
    log.info("tool_call tool=get_order order_id=%s", order_id)
    tel.set_attributes(**{tel.TOOL_NAME: "get_order", tel.ORDER_ID: order_id})
    result = _call_gateway(ORDER_TOOL, {"order_id": order_id}, label="get_order")
    tel.set_attributes(**{tel.CUSTOMER_ID: result.get("customer_id")})
    return result


@tool
def get_customer(customer_id: str) -> dict[str, Any]:
    """Look up a customer account: name, email, loyalty tier, preferred contact
    channel, and the IDs of the orders they have placed.

    Use this for questions about the person rather than about a specific order.

    Args:
        customer_id: Customer identifier in the form `CUST-<digits>`, e.g. `CUST-001`.

    Returns:
        On success the customer record. On failure `status: "error"` with a
        `code` such as `CUSTOMER_NOT_FOUND`.
    """
    log.info("tool_call tool=get_customer customer_id=%s", customer_id)
    tel.set_attributes(**{tel.TOOL_NAME: "get_customer", tel.CUSTOMER_ID: customer_id})
    return _call_gateway(CUSTOMER_TOOL, {"customer_id": customer_id}, label="get_customer")


@tool
def process_refund(order_id: str, amount_cents: int) -> dict[str, Any]:
    """Refund money against an order. This moves money — call it only when the
    customer has asked for a refund and you know both the order and the amount.

    Calling it twice for the same order and amount is safe: the second call
    returns the first refund instead of issuing another.

    Args:
        order_id: Order to refund, in the form `ORD-<digits>`.
        amount_cents: Amount to refund as an integer number of US cents.
            $249.99 is 24999. Never pass dollars and never pass a decimal.
            Must be greater than zero and not more than the order total.

    Returns:
        On success, `{"status": "success", "refund_id": ..., "amount_cents": ...}`.
        On failure, `status: "error"` with a `code` of `INVALID_AMOUNT`,
        `ORDER_NOT_FOUND`, `AMOUNT_EXCEEDS_ORDER_TOTAL`, or
        `REFUND_LIMIT_EXCEEDED`. None of those are worth retrying.
    """
    # Derived here, never taken from the model, and identical on every attempt.
    idempotency_key = derive_idempotency_key(order_id, amount_cents)
    arguments = {
        "order_id": order_id,
        "amount_cents": amount_cents,
        "idempotency_key": idempotency_key,
        "operation_id": idempotency_scope(),
    }
    log.info(
        "tool_call tool=process_refund order_id=%s amount_cents=%s idempotency_key=%s",
        order_id,
        amount_cents,
        idempotency_key,
    )
    tel.set_attributes(
        **{
            tel.TOOL_NAME: "process_refund",
            tel.ORDER_ID: order_id,
            tel.REFUND_AMOUNT: amount_cents,
            tel.REFUND_IDEMPOTENCY_KEY: idempotency_key,
            tel.OPERATION_ID: idempotency_scope(),
        }
    )
    return _call_gateway(REFUND_TOOL, arguments, label="process_refund")


TOOLS = [get_order, get_customer, process_refund]


def _build_agent(actor_id: str, session_id: str) -> Agent:
    session_manager = build_session_manager(actor_id=actor_id, session_id=session_id)
    if session_manager is None:
        log.warning("memory_unavailable session_id=%s — answering without recall", session_id)
        return Agent(model=BedrockModel(model_id=MODEL_ID), system_prompt=SYSTEM_PROMPT, tools=TOOLS)
    # With a session manager, history lives in AgentCore Memory rather than in
    # a window this process keeps, so no conversation_manager is passed.
    return Agent(
        model=BedrockModel(model_id=MODEL_ID),
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        session_manager=session_manager,
    )


def _agent_cache():
    """One Agent per (actor, session), LRU-bounded so it cannot grow unbounded.

    Keyed on the actor too: two customers must never share an Agent, even if a
    caller reuses a session id.
    """
    cache: OrderedDict[tuple[str, str], Agent] = OrderedDict()

    def get_or_create(actor_id: str, session_id: str) -> Agent:
        key = (actor_id, session_id)
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
        if len(cache) >= MAX_CACHED_SESSIONS:
            evicted, _ = cache.popitem(last=False)
            log.info("session_cache_evicted session_id=%s", evicted[1])
        cache[key] = _build_agent(actor_id, session_id)
        return cache[key]

    return get_or_create


get_or_create_agent = _agent_cache()


def _extract_prompt(payload: Any) -> str:
    """Pull the user's text out of the invoke payload."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('payload must contain a non-empty "prompt" string')
    return prompt


def _operation_id_from(context: Any) -> str | None:
    """The caller's retry token for this whole invocation, if it sent one."""
    headers = getattr(context, "request_headers", None) or {}
    for name, value in headers.items():
        if name.lower() == OPERATION_ID_HEADER.lower():
            value = (value or "").strip()
            return value[:128] or None
    return None


@app.entrypoint
async def invoke(payload: dict, context: Any):
    session_id = getattr(context, "session_id", None) or "default-session"
    try:
        actor_id = actor_id_from(context)
    except IdentityError as exc:
        # A token that reached us but carries no usable identity is a failure,
        # not an anonymous session: serving it would mean guessing whose data
        # to open. The message names no claim values.
        log.warning("identity_rejected session_id=%s reason=%s", session_id, exc)
        raise
    prompt = _extract_prompt(payload)
    operation_id = _operation_id_from(context)
    log.info(
        "invoke session_id=%s actor_id=%s operation_id=%s prompt_chars=%d",
        session_id,
        actor_id,
        operation_id,
        len(prompt),
    )

    # Both scope refund idempotency keys; the operation id wins when present.
    current_session_id.set(session_id)
    current_operation_id.set(operation_id)
    _turn.set(_TurnCounter())

    # On the turn's own span, so every child tool span is reachable from a search
    # on the customer or the operation.
    tel.set_attributes(
        **{
            tel.ACTOR_ID: actor_id,
            tel.SESSION_ID: session_id,
            tel.OPERATION_ID: operation_id,
        }
    )

    agent = get_or_create_agent(actor_id, session_id)

    async for event in agent.stream_async(prompt):
        if not isinstance(event, dict) or "event" not in event:
            continue
        # Drop the empty contentBlockStart frames the runtime does not need.
        block_start = event["event"].get("contentBlockStart")
        if block_start is not None and not block_start.get("start"):
            continue
        yield event


if __name__ == "__main__":
    app.run()
