"""`process_refund` — refund money against an order, exactly once.

This is the only tool that writes. Two things make it safe:

  * **The $1,000 ceiling is checked here**, against the amount in the request
    and against the order total in DynamoDB. This runs no matter what the model
    was persuaded to ask for, and is defence in depth behind the Cedar policy
    at the Gateway.
  * **Idempotency is a conditional write.** The Refunds table is keyed on
    `idempotency_key`, and the PutItem requires `attribute_not_exists`. A retry
    with the same key loses the race by design and gets the original refund
    back instead of issuing a second one.

IAM: `dynamodb:GetItem` on Orders; `GetItem` + `PutItem` on Refunds. No deletes,
no scans, no other tables.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from common import errors, logging_json as jlog, metrics
from common.config import ORDERS_TABLE, REFUND_LIMIT_CENTS, REFUNDS_TABLE
from common.dynamo import get_item, table, undecimal
from common.gateway import handler


def _require_str(args: dict[str, Any], field: str) -> str:
    value = args.get(field)
    if not isinstance(value, str) or not value.strip():
        raise errors.ToolError(errors.INVALID_INPUT, f"{field} is required and must be a string.")
    return value.strip()


def _require_amount_cents(args: dict[str, Any]) -> int:
    value = args.get("amount_cents")
    # bool is an int subclass; True must not be read as 1 cent.
    if isinstance(value, bool) or not isinstance(value, int):
        raise errors.ToolError(
            errors.INVALID_AMOUNT,
            "amount_cents must be an integer number of cents, not a decimal or a string.",
        )
    if value <= 0:
        raise errors.ToolError(errors.INVALID_AMOUNT, "amount_cents must be greater than zero.")
    return value


def process_refund(args: dict[str, Any]) -> dict[str, Any]:
    order_id = _require_str(args, "order_id").upper()
    idempotency_key = _require_str(args, "idempotency_key")
    amount_cents = _require_amount_cents(args)
    # Recorded for traceability only. The idempotency decision is made on
    # idempotency_key alone, so a caller cannot widen or narrow it by relabelling
    # the operation.
    operation_id = args.get("operation_id")
    operation_id = operation_id.strip()[:128] if isinstance(operation_id, str) else None

    jlog.log(
        "process_refund_requested",
        order_id=order_id,
        amount_cents=amount_cents,
        idempotency_key=idempotency_key,
        operation_id=operation_id,
    )

    order = get_item(ORDERS_TABLE, {"order_id": order_id})
    if order is None:
        return errors.err(errors.ORDER_NOT_FOUND, f"No order with id {order_id}.")

    order_total = int(order.get("total_cents", 0))
    if amount_cents > order_total:
        jlog.warn(
            "refund_denied",
            order_id=order_id,
            amount_cents=amount_cents,
            reason="exceeds_order_total",
        )
        metrics.emit(
            metrics.REFUNDS_DENIED,
            tool="process_refund",
            reason="exceeds_order_total",
            order_id=order_id,
            amount_cents=amount_cents,
        )
        return errors.err(
            errors.AMOUNT_EXCEEDS_ORDER_TOTAL,
            f"Order {order_id} totalled {order_total} cents; cannot refund {amount_cents}.",
        )

    if amount_cents > REFUND_LIMIT_CENTS:
        jlog.warn(
            "refund_denied",
            order_id=order_id,
            amount_cents=amount_cents,
            reason="exceeds_refund_limit",
            limit_cents=REFUND_LIMIT_CENTS,
        )
        metrics.emit(
            metrics.REFUNDS_DENIED,
            tool="process_refund",
            reason="exceeds_refund_limit",
            order_id=order_id,
            amount_cents=amount_cents,
        )
        return errors.err(
            errors.REFUND_LIMIT_EXCEEDED,
            f"Refunds above {REFUND_LIMIT_CENTS} cents require human approval.",
        )

    record = {
        "idempotency_key": idempotency_key,
        "refund_id": f"RFND-{uuid.uuid4().hex[:12].upper()}",
        "order_id": order_id,
        "customer_id": order.get("customer_id"),
        "amount_cents": amount_cents,
        "currency": order.get("currency", "USD"),
        "created_at": int(time.time()),
    }
    if operation_id:
        record["operation_id"] = operation_id

    try:
        table(REFUNDS_TABLE).put_item(
            Item=record,
            ConditionExpression="attribute_not_exists(idempotency_key)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return _return_existing(idempotency_key, order_id)
        raise errors.ToolError(
            errors.DEPENDENCY_UNAVAILABLE,
            "Could not record the refund; it was not processed.",
            retryable=True,
        ) from exc
    except BotoCoreError as exc:
        raise errors.ToolError(
            errors.DEPENDENCY_UNAVAILABLE,
            "Could not record the refund; it was not processed.",
            retryable=True,
        ) from exc

    jlog.log(
        "refund_processed",
        refund_id=record["refund_id"],
        order_id=order_id,
        amount_cents=amount_cents,
        idempotency_key=idempotency_key,
    )
    metrics.emit(
        metrics.REFUNDS_PROCESSED,
        tool="process_refund",
        order_id=order_id,
        amount_cents=amount_cents,
        refund_id=record["refund_id"],
        idempotency_key=idempotency_key,
    )
    return errors.ok(**undecimal(record), duplicate=False)


def _return_existing(idempotency_key: str, order_id: str) -> dict[str, Any]:
    """The key was already used: return that refund rather than a second one."""
    existing = get_item(REFUNDS_TABLE, {"idempotency_key": idempotency_key})
    jlog.log(
        "duplicate_refund_prevented",
        order_id=order_id,
        idempotency_key=idempotency_key,
        refund_id=(existing or {}).get("refund_id"),
    )
    metrics.emit(
        metrics.DUPLICATE_REFUND_PREVENTED,
        tool="process_refund",
        order_id=order_id,
        idempotency_key=idempotency_key,
        refund_id=(existing or {}).get("refund_id"),
    )
    if existing is None:
        # The conditional write lost to a concurrent identical call that has not
        # become readable yet. Nothing was double-refunded; the caller should ask again.
        return errors.err(
            errors.DEPENDENCY_UNAVAILABLE,
            "An identical refund is already in flight.",
            retryable=True,
        )
    return errors.ok(**existing, duplicate=True)


lambda_handler = handler(process_refund)
