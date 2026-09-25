"""The result envelope must never be overwritten by payload data.

Regression: `get_order` splatted an order record whose own `status` was
"DELAYED" into `errors.ok(...)`, so the returned envelope read
`status: "DELAYED"` instead of `status: "success"`. Nothing downstream could then
tell a successful lookup from a failed one — the agent recorded
`tool.outcome=DELAYED` on its spans, and the `policy.decision` attribute, which
is derived from a `success` status, was never set on a successful call.
"""

from __future__ import annotations

import pytest

from common import errors
from get_order import handler as get_order_handler

ORDER = {
    "order_id": "ORD-1001",
    "customer_id": "CUST-001",
    "status": "DELAYED",
    "status_reason": "Carrier delay at the Leipzig sorting hub.",
    "total_cents": 24999,
    "currency": "USD",
}


@pytest.fixture
def orders(monkeypatch):
    monkeypatch.setattr(
        get_order_handler,
        "get_item",
        lambda _table, key: dict(ORDER) if key["order_id"] == "ORD-1001" else None,
    )


def test_lookup_status_is_success_not_the_order_state(orders):
    result = get_order_handler.get_order({"order_id": "ORD-1001"})
    assert result["status"] == "success"


def test_order_state_is_preserved_under_its_own_key(orders):
    result = get_order_handler.get_order({"order_id": "ORD-1001"})
    assert result["order_status"] == "DELAYED"
    assert result["status_reason"].startswith("Carrier delay")


def test_the_rest_of_the_record_survives(orders):
    result = get_order_handler.get_order({"order_id": "ORD-1001"})
    assert result["order_id"] == "ORD-1001"
    assert result["total_cents"] == 24999


def test_missing_order_still_reports_an_error(orders):
    result = get_order_handler.get_order({"order_id": "ORD-9999"})
    assert result["status"] == "error"
    assert result["code"] == "ORDER_NOT_FOUND"


@pytest.mark.parametrize("reserved", ["status", "code", "message", "retryable"])
def test_ok_refuses_to_let_payload_shadow_the_envelope(reserved):
    with pytest.raises(ValueError) as excinfo:
        errors.ok(**{reserved: "whatever"})
    assert reserved in str(excinfo.value)


def test_ok_accepts_ordinary_payload_fields():
    assert errors.ok(order_id="ORD-1") == {"status": "success", "order_id": "ORD-1"}
