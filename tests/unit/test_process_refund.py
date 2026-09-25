"""The refund Lambda's decisions, with DynamoDB replaced by a fake.

The conditional write is the real safety mechanism, so the fake reproduces its
one important behaviour: a PutItem whose condition fails raises
`ConditionalCheckFailedException` rather than overwriting.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from process_refund import handler as refund

ORDER = {
    "order_id": "ORD-1001",
    "customer_id": "CUST-001",
    "total_cents": 24999,
    "currency": "USD",
}

BIG_ORDER = {
    "order_id": "ORD-123",
    "customer_id": "CUST-002",
    "total_cents": 189900,
    "currency": "USD",
}


class FakeTable:
    def __init__(self, store: dict) -> None:
        self.store = store
        self.put_calls = 0

    def put_item(self, Item, ConditionExpression=None):  # noqa: N803 - boto3 casing
        self.put_calls += 1
        key = Item["idempotency_key"]
        if ConditionExpression and key in self.store:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "exists"}},
                "PutItem",
            )
        self.store[key] = dict(Item)


@pytest.fixture
def refunds(monkeypatch):
    """Wire the handler to in-memory Orders and Refunds tables."""
    store: dict = {}
    fake = FakeTable(store)
    orders = {o["order_id"]: o for o in (ORDER, BIG_ORDER)}

    def fake_get_item(table_name, key):
        if "orders" in table_name:
            return orders.get(key["order_id"])
        return store.get(key["idempotency_key"])

    monkeypatch.setattr(refund, "get_item", fake_get_item)
    monkeypatch.setattr(refund, "table", lambda _name: fake)
    return fake


def call(**overrides):
    args = {"order_id": "ORD-1001", "amount_cents": 24999, "idempotency_key": "operation-123"}
    args.update(overrides)
    return refund.process_refund(args)


def test_refund_succeeds_within_the_limit(refunds):
    result = call()
    assert result["status"] == "success"
    assert result["amount_cents"] == 24999
    assert result["duplicate"] is False
    assert refunds.put_calls == 1


def test_same_idempotency_key_returns_the_original_refund(refunds):
    first = call()
    second = call()

    assert second["status"] == "success"
    assert second["duplicate"] is True
    assert second["refund_id"] == first["refund_id"], "a retry must not mint a new refund"
    assert len(refunds.store) == 1, "exactly one refund recorded"


def test_a_third_attempt_still_returns_the_original(refunds):
    first = call()
    call()
    third = call()
    assert third["refund_id"] == first["refund_id"]
    assert len(refunds.store) == 1


def test_a_different_key_is_a_different_refund(refunds):
    first = call()
    second = call(idempotency_key="operation-456")
    assert second["refund_id"] != first["refund_id"]
    assert len(refunds.store) == 2


def test_operation_id_is_recorded_but_does_not_drive_idempotency(refunds):
    call(operation_id="operation-123")
    stored = refunds.store["operation-123"]
    assert stored["operation_id"] == "operation-123"

    # Same operation, different order: must NOT collapse into the first refund.
    other = call(order_id="ORD-123", amount_cents=1000, idempotency_key="op-123-b",
                 operation_id="operation-123")
    assert other["duplicate"] is False
    assert len(refunds.store) == 2


def test_over_the_ceiling_is_denied_and_writes_nothing(refunds):
    result = call(order_id="ORD-123", amount_cents=189900, idempotency_key="op-big")
    assert result["status"] == "error"
    assert result["code"] == "REFUND_LIMIT_EXCEEDED"
    assert result["retryable"] is False
    assert refunds.put_calls == 0, "a denied refund must never reach the ledger"


def test_more_than_the_order_total_is_denied(refunds):
    result = call(amount_cents=30000)
    assert result["code"] == "AMOUNT_EXCEEDS_ORDER_TOTAL"
    assert refunds.put_calls == 0


def test_unknown_order_is_denied(refunds):
    result = call(order_id="ORD-9999")
    assert result["code"] == "ORDER_NOT_FOUND"
    assert refunds.put_calls == 0


@pytest.mark.parametrize("amount", [0, -1, "24999", 249.99, True, None])
def test_bad_amounts_are_rejected(refunds, amount):
    with pytest.raises(Exception) as excinfo:
        call(amount_cents=amount)
    assert getattr(excinfo.value, "code", "") in {"INVALID_AMOUNT", "INVALID_INPUT"}
    assert refunds.put_calls == 0


def test_missing_idempotency_key_is_rejected(refunds):
    with pytest.raises(Exception) as excinfo:
        refund.process_refund({"order_id": "ORD-1001", "amount_cents": 100})
    assert excinfo.value.code == "INVALID_INPUT"
    assert refunds.put_calls == 0
