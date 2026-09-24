"""AgentCore Memory wiring.

Two kinds of memory, both handled by `AgentCoreMemorySessionManager`:

  * **Short term** — every message in a session is written as an event, keyed on
    `(actor_id, session_id)`. Resuming the same session replays them, so
    conversation history survives a cold start instead of living only in the
    process.
  * **Long term** — background strategies read those events and extract durable
    records into actor-scoped namespaces: `/preferences/{actorId}/` for stated
    preferences and `/facts/{actorId}/` for facts. Because the namespaces carry
    no session, a *new* session for the same actor retrieves them. That is what
    makes "my preferred AWS region is eu-west-1" survive into tomorrow's chat.

Extraction is asynchronous — a preference stated in session A is not readable a
second later. Tests must poll.

The actor is the customer, not the conversation, and it comes from the verified
JWT claim (`agent/identity.py`) — never from the prompt and never from a request
header a caller could set. That is what keeps one customer's memory out of
another's conversation.
"""

from __future__ import annotations

import os

from bedrock_agentcore.memory.integrations.strands.config import (
    AgentCoreMemoryConfig,
    RetrievalConfig,
)
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)

MEMORY_NAME = os.environ.get("MEMORY_NAME", "csagent_dev_memory")
_MEMORY_ENV = f"MEMORY_{MEMORY_NAME.upper()}_ID"


def memory_id() -> str | None:
    """The deployed Memory's id, injected as MEMORY_<NAME>_ID by `agentcore deploy`."""
    return os.environ.get(_MEMORY_ENV) or os.environ.get("MEMORY_ID") or None


def build_session_manager(*, actor_id: str, session_id: str) -> AgentCoreMemorySessionManager | None:
    """A session manager for this (actor, session), or None if Memory is not deployed.

    Returning None rather than raising keeps the agent usable — it answers
    without recall instead of failing the request outright.
    """
    mem_id = memory_id()
    if not mem_id:
        return None

    config = AgentCoreMemoryConfig(
        memory_id=mem_id,
        session_id=session_id,
        actor_id=actor_id,
        retrieval_config={
            # Preferences are the point of the cross-session test, so they are
            # retrieved with a low bar. Facts are noisier, hence the higher one.
            "/preferences/{actorId}/": RetrievalConfig(top_k=5, relevance_score=0.2),
            "/facts/{actorId}/": RetrievalConfig(top_k=5, relevance_score=0.4),
        },
        # The entrypoint drives the agent with stream_async; without this the
        # per-turn boto3 calls would block the event loop, and Strands refuses
        # to dispatch the async hooks otherwise.
        async_mode=True,
    )
    return AgentCoreMemorySessionManager(config)
