"""Deterministic idempotency keys for refunds.

The key must never come from the model. If the LLM invented it, a re-plan or a
retried turn would produce a fresh key and the customer would be refunded
twice. Deriving it from (order_id, amount_cents, session_id) means every retry
of the *same* refund inside the *same* conversation collapses onto one key,
while a genuinely new refund request gets a new one.

`current_session_id` is a ContextVar rather than an argument so the session
never has to travel through the tool schema, where the model could overwrite it.
"""

from __future__ import annotations

import hashlib
from contextvars import ContextVar

current_session_id: ContextVar[str] = ContextVar("current_session_id", default="local-session")


def derive_idempotency_key(order_id: str, amount_cents: int, session_id: str | None = None) -> str:
    """Return a stable 32-char key for one refund attempt."""
    session = session_id if session_id is not None else current_session_id.get()
    payload = f"{order_id.strip().upper()}|{int(amount_cents)}|{session}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
