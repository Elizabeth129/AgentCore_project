"""Customer Support Agent — Strands agent behind an AgentCore Runtime entrypoint.

Tools now come from the AgentCore Gateway over MCP, not from this process. The
Gateway fronts three Lambdas backed by DynamoDB, and the Runtime reaches it with
SigV4 using its execution role (`agent/gateway_client.py`).

`process_refund` is the one exception: the Gateway tool is hidden from the model
and wrapped below, so the idempotency key is derived by us rather than invented
by the LLM. Everything else the model sees is the Gateway's own tool list.

Conversation state lives in AgentCore Memory (`agent/memory.py`), keyed on the
actor and the session, so history survives a cold start and preferences carry
across sessions.

Invoke payload: `{"prompt": "..."}`, with a Cognito JWT in the Authorization
header. The customer comes from that token's `custom:customer_id` claim
(`agent/identity.py`), which the Runtime authorizer has already validated.
"""

from __future__ import annotations

import json
import os
import uuid
from collections import OrderedDict
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool
from strands.agent.conversation_manager.sliding_window_conversation_manager import (
    SlidingWindowConversationManager,
)
from strands.models.bedrock import BedrockModel
from strands.types.exceptions import MCPClientInitializationError

from gateway_client import build_gateway_client
from identity import IdentityError, actor_id_from
from idempotency import current_session_id, derive_idempotency_key
from memory import build_session_manager
from prompts import SYSTEM_PROMPT

app = BedrockAgentCoreApp()
log = app.logger

MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-5-20250929-v1:0")

# Gateway tools are named `<targetName>___<toolName>`.
REFUND_TOOL = os.environ.get("REFUND_TOOL_NAME", "refunds___process_refund")

MAX_CACHED_SESSIONS = 128

# One client for the process. Strands manages its lifecycle as a tool provider;
# the refund wrapper below restarts it if it is called outside that window.
gateway = build_gateway_client(rejected_tools=[REFUND_TOOL])


def _unwrap(result: Any) -> dict[str, Any]:
    """Turn an MCP tool result back into the structured dict the Lambda returned.

    MCP carries the result as text content, so without this the model would see
    a JSON string wrapped in protocol scaffolding instead of `status`/`code`.
    """
    content = result.get("content") or []
    for block in content:
        text = block.get("text") if isinstance(block, dict) else None
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {
        "status": "error",
        "code": "TOOL_RESULT_UNREADABLE",
        "message": "The refund service returned a response that could not be read.",
        "retryable": True,
    }


@tool
def process_refund(order_id: str, amount_cents: int) -> dict[str, Any]:
    """Refund money against an order. This moves money — call it only when the
    customer has asked for a refund and you know both the order and the amount.

    Calling it twice for the same order and amount in one conversation is safe:
    the second call returns the first refund instead of issuing another.

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
    arguments = {
        "order_id": order_id,
        "amount_cents": amount_cents,
        # Derived here, never taken from the model. See agent/idempotency.py.
        "idempotency_key": derive_idempotency_key(order_id, amount_cents),
    }
    log.info("refund_tool_call order_id=%s amount_cents=%s", order_id, amount_cents)

    try:
        result = gateway.call_tool_sync(str(uuid.uuid4()), REFUND_TOOL, arguments)
    except MCPClientInitializationError:
        gateway.start()
        result = gateway.call_tool_sync(str(uuid.uuid4()), REFUND_TOOL, arguments)

    return _unwrap(result)


def _build_agent(actor_id: str, session_id: str) -> Agent:
    session_manager = build_session_manager(actor_id=actor_id, session_id=session_id)
    if session_manager is None:
        log.warning("memory_unavailable session_id=%s — answering without recall", session_id)
        return Agent(
            model=BedrockModel(model_id=MODEL_ID),
            system_prompt=SYSTEM_PROMPT,
            tools=[gateway, process_refund],
            conversation_manager=SlidingWindowConversationManager(window_size=40),
        )
    # With a session manager, history lives in AgentCore Memory rather than in
    # a window this process keeps, so no conversation_manager is passed.
    return Agent(
        model=BedrockModel(model_id=MODEL_ID),
        system_prompt=SYSTEM_PROMPT,
        tools=[gateway, process_refund],
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
    log.info(
        "invoke session_id=%s actor_id=%s prompt_chars=%d",
        session_id,
        actor_id,
        len(prompt),
    )

    # Scopes refund idempotency keys to this conversation.
    current_session_id.set(session_id)

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
