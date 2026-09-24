"""Seed the Orders and Customers tables with the demo fixtures.

The same records the README documents. Money is integer cents throughout.
Re-running overwrites the fixtures and leaves the Refunds table untouched, so a
reseed never erases evidence of an idempotency test.

    python scripts/seed_data.py
"""

from __future__ import annotations

import os
import sys

import boto3

PROJECT_PREFIX = os.environ.get("PROJECT_PREFIX", "csagent")
STAGE = os.environ.get("STAGE", "dev")
REGION = os.environ.get("AWS_REGION", "us-east-1")

ORDERS = [
    {
        "order_id": "ORD-1001",
        "customer_id": "CUST-001",
        "status": "DELAYED",
        "status_reason": "Carrier delay at the Leipzig sorting hub.",
        "placed_at": "2026-09-12",
        "original_delivery": "2026-09-19",
        "expected_delivery": "2026-09-26",
        "total_cents": 24999,
        "currency": "USD",
        "items": [
            {"sku": "KB-87", "name": "Mechanical keyboard", "qty": 1, "unit_price_cents": 24999}
        ],
    },
    {
        "order_id": "ORD-1002",
        "customer_id": "CUST-001",
        "status": "DELIVERED",
        "status_reason": "Delivered and signed for.",
        "placed_at": "2026-08-30",
        "original_delivery": "2026-09-03",
        "expected_delivery": "2026-09-03",
        "total_cents": 8900,
        "currency": "USD",
        "items": [{"sku": "MP-01", "name": "Desk mat", "qty": 2, "unit_price_cents": 4450}],
    },
    {
        # Over the $1,000 ceiling on purpose: this is the refund-denial fixture.
        "order_id": "ORD-123",
        "customer_id": "CUST-002",
        "status": "DELAYED",
        "status_reason": "Item was out of stock; restocked and reshipped.",
        "placed_at": "2026-09-05",
        "original_delivery": "2026-09-12",
        "expected_delivery": "2026-09-27",
        "total_cents": 189900,
        "currency": "USD",
        "items": [{"sku": "MON-32", "name": "32-inch monitor", "qty": 1, "unit_price_cents": 189900}],
    },
]

CUSTOMERS = [
    {
        "customer_id": "CUST-001",
        "name": "Dana Osei",
        "email": "dana.osei@example.com",
        "tier": "GOLD",
        "contact_preference": "email",
        "since": "2023-04-11",
        "order_ids": ["ORD-1001", "ORD-1002"],
    },
    {
        "customer_id": "CUST-002",
        "name": "Marek Nowak",
        "email": "marek.nowak@example.com",
        "tier": "STANDARD",
        "contact_preference": "sms",
        "since": "2025-11-02",
        "order_ids": ["ORD-123"],
    },
]


def main() -> int:
    dynamodb = boto3.resource("dynamodb", region_name=REGION)

    for suffix, records, key in (
        ("orders", ORDERS, "order_id"),
        ("customers", CUSTOMERS, "customer_id"),
    ):
        table = dynamodb.Table(f"{PROJECT_PREFIX}-{STAGE}-{suffix}")
        with table.batch_writer() as batch:
            for record in records:
                batch.put_item(Item=record)
        print(f"seeded {len(records)} into {table.name}: {[r[key] for r in records]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
