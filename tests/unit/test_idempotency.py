"""The idempotency key must be identical across retries — and only then."""

from __future__ import annotations

import pytest

from idempotency import (
    current_operation_id,
    current_session_id,
    derive_idempotency_key,
    idempotency_scope,
)


@pytest.fixture(autouse=True)
def reset_context():
    session = current_session_id.set("session-A")
    operation = current_operation_id.set(None)
    yield
    current_session_id.reset(session)
    current_operation_id.reset(operation)


def test_same_refund_twice_yields_the_same_key():
    assert derive_idempotency_key("ORD-1001", 24999) == derive_idempotency_key("ORD-1001", 24999)


def test_key_is_stable_across_formatting_of_the_order_id():
    assert derive_idempotency_key(" ord-1001 ", 24999) == derive_idempotency_key("ORD-1001", 24999)


def test_different_amount_is_a_different_refund():
    assert derive_idempotency_key("ORD-1001", 24999) != derive_idempotency_key("ORD-1001", 1000)


def test_different_order_is_a_different_refund():
    assert derive_idempotency_key("ORD-1001", 24999) != derive_idempotency_key("ORD-1002", 24999)


def test_separate_sessions_are_separate_refunds():
    first = derive_idempotency_key("ORD-1001", 24999)
    current_session_id.set("session-B")
    assert derive_idempotency_key("ORD-1001", 24999) != first


def test_operation_id_overrides_the_session():
    """A caller retrying a timed-out invocation lands on the same key even
    though the runtime gave it a new session."""
    current_operation_id.set("operation-123")
    first = derive_idempotency_key("ORD-1001", 24999)

    current_session_id.set("a-completely-different-session")
    assert derive_idempotency_key("ORD-1001", 24999) == first


def test_one_operation_can_refund_two_orders_without_collision():
    current_operation_id.set("operation-123")
    assert derive_idempotency_key("ORD-1001", 24999) != derive_idempotency_key("ORD-1002", 8900)


def test_scope_prefers_the_operation_id():
    assert idempotency_scope() == "session-A"
    current_operation_id.set("operation-123")
    assert idempotency_scope() == "operation-123"


def test_key_shape():
    key = derive_idempotency_key("ORD-1001", 24999)
    assert len(key) == 32
    assert all(c in "0123456789abcdef" for c in key)
