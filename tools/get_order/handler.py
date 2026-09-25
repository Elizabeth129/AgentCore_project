"""`get_order` — read one order from the Orders table.

IAM: `dynamodb:GetItem` on Orders only. This Lambda never writes.
"""

from __future__ import annotations

from typing import Any

from common import errors, logging_json as jlog
from common.config import ORDERS_TABLE
from common.dynamo import get_item
from common.gateway import handler


def _normalise_order_id(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise errors.ToolError(errors.INVALID_INPUT, "order_id is required and must be a string.")
    order_id = raw.strip().upper()
    # Customers say "order 123"; the model is told to prefix it, but normalise
    # here too so a bare number is never a lookup miss.
    if not order_id.startswith("ORD-"):
        order_id = f"ORD-{order_id.removeprefix('ORD')}"
    return order_id


def get_order(args: dict[str, Any]) -> dict[str, Any]:
    order_id = _normalise_order_id(args.get("order_id"))
    jlog.log("get_order", order_id=order_id)

    order = get_item(ORDERS_TABLE, {"order_id": order_id})
    if order is None:
        return errors.err(errors.ORDER_NOT_FOUND, f"No order with id {order_id}.")

    # The order's own `status` ("DELAYED", "DELIVERED") is renamed, because the
    # envelope owns `status` and a collision would hide whether the call itself
    # succeeded. `errors.ok` rejects the clash rather than letting it through.
    order["order_status"] = order.pop("status", None)
    return errors.ok(**order)


lambda_handler = handler(get_order)
