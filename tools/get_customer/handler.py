"""`get_customer` — read one customer from the Customers table.

IAM: `dynamodb:GetItem` on Customers only. This Lambda never writes.
"""

from __future__ import annotations

from typing import Any

from common import errors, logging_json as jlog
from common.config import CUSTOMERS_TABLE
from common.dynamo import get_item
from common.gateway import handler


def _normalise_customer_id(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise errors.ToolError(errors.INVALID_INPUT, "customer_id is required and must be a string.")
    customer_id = raw.strip().upper()
    if not customer_id.startswith("CUST-"):
        customer_id = f"CUST-{customer_id.removeprefix('CUST')}"
    return customer_id


def get_customer(args: dict[str, Any]) -> dict[str, Any]:
    customer_id = _normalise_customer_id(args.get("customer_id"))
    jlog.log("get_customer", customer_id=customer_id)

    customer = get_item(CUSTOMERS_TABLE, {"customer_id": customer_id})
    if customer is None:
        return errors.err(errors.CUSTOMER_NOT_FOUND, f"No customer with id {customer_id}.")
    return errors.ok(**customer)


lambda_handler = handler(get_customer)
