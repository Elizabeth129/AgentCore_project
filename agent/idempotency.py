"""Deterministic idempotency keys for refunds.

The key must never come from the model. If the LLM invented it, a re-plan or a
retried turn would produce a fresh key and the customer would be refunded twice.

There are two levels of retry to survive, and the key has to be stable across
both:

  * **Inside one turn** — `agent/retry.py` retries a transient refund failure.
    Every attempt reuses one key, so at most one refund is recorded.
  * **Across whole invocations** — a caller whose request timed out re-invokes
    the agent. It cannot know whether the first attempt reached the backend, so
    it sends the same *operation id* again (the
    `X-Amzn-Bedrock-AgentCore-Runtime-Custom-Operation-Id` header, e.g.
    `operation-123`). Both invocations then derive the same key and the second
    gets the first refund back.

When no operation id is supplied the session id is used instead, which still
collapses retries within one conversation.

The key mixes in the order and the amount rather than being the operation id
verbatim: one operation may legitimately refund two different orders, and those
must not collide into a single record. The operation id is stored alongside the
refund so the link back is not lost.

Both values are ContextVars rather than arguments so they never travel through
the tool schema, where the model could overwrite them.
"""

from __future__ import annotations

import hashlib
from contextvars import ContextVar

current_session_id: ContextVar[str] = ContextVar("current_session_id", default="local-session")
current_operation_id: ContextVar[str | None] = ContextVar("current_operation_id", default=None)


def idempotency_scope() -> str:
    """The value that makes two attempts count as the same operation."""
    return current_operation_id.get() or current_session_id.get()


def derive_idempotency_key(order_id: str, amount_cents: int, scope: str | None = None) -> str:
    """Return a stable 32-character key for one refund attempt."""
    payload = f"{scope if scope is not None else idempotency_scope()}|{order_id.strip().upper()}|{int(amount_cents)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
