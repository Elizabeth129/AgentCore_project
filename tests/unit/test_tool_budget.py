"""The per-turn tool-call budget, which bounds a looping model.

The regression these tests exist for: the counter used to be an int in a
ContextVar. Strands runs sync tool functions on worker threads, and a thread
receives a *copy* of the context, so each call incremented its own copy and the
budget never tripped no matter how long the loop ran.
"""

from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor

import pytest

import agent as A


@pytest.fixture(autouse=True)
def fresh_turn():
    A._turn.set(A._TurnCounter())
    yield


def spend(n: int) -> list[dict | None]:
    return [A._spend_budget("get_order") for _ in range(n)]


def test_calls_within_budget_are_allowed(monkeypatch):
    monkeypatch.setattr(A, "TOOL_CALL_BUDGET", 3)
    assert spend(3) == [None, None, None]


def test_the_call_past_the_budget_is_refused(monkeypatch):
    monkeypatch.setattr(A, "TOOL_CALL_BUDGET", 2)
    results = spend(3)
    assert results[:2] == [None, None]
    refused = results[2]
    assert refused["code"] == "TOOL_CALL_BUDGET_EXCEEDED"
    assert refused["retryable"] is False, "retrying a spent budget would loop forever"


def test_refusal_tells_the_model_to_stop(monkeypatch):
    monkeypatch.setattr(A, "TOOL_CALL_BUDGET", 1)
    spend(1)
    message = A._spend_budget("get_order")["message"]
    assert "Stop calling tools" in message


def test_count_accumulates_across_thread_copied_contexts(monkeypatch):
    """Each call runs on a worker thread with a copy of the *caller's* context,
    which is what `asyncio.to_thread` does and how Strands invokes sync tools."""
    monkeypatch.setattr(A, "TOOL_CALL_BUDGET", 2)

    with ThreadPoolExecutor(max_workers=1) as pool:
        outcomes = []
        for _ in range(3):
            # Copy here, on the caller's thread, where the turn was established.
            context = contextvars.copy_context()
            outcomes.append(pool.submit(context.run, A._spend_budget, "get_order").result())

    assert outcomes[0] is None and outcomes[1] is None
    assert outcomes[2] is not None, "the counter must be shared, not copied per call"
    assert outcomes[2]["code"] == "TOOL_CALL_BUDGET_EXCEEDED"


def test_a_new_turn_starts_with_a_full_budget(monkeypatch):
    monkeypatch.setattr(A, "TOOL_CALL_BUDGET", 1)
    spend(1)
    assert A._spend_budget("get_order") is not None

    A._turn.set(A._TurnCounter())  # what the entrypoint does per invocation
    assert A._spend_budget("get_order") is None


def test_budget_is_shared_across_different_tools(monkeypatch):
    """The budget bounds the turn, not each tool: a loop that alternates tools
    is still a loop."""
    monkeypatch.setattr(A, "TOOL_CALL_BUDGET", 2)
    assert A._spend_budget("get_order") is None
    assert A._spend_budget("get_customer") is None
    assert A._spend_budget("process_refund") is not None
