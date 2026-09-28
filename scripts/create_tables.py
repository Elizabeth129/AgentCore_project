"""Create the Orders, Customers and Refunds DynamoDB tables.

The AgentCore CDK app models AgentCore resources only, so the data store is
provisioned with boto3 here. Safe to re-run: existing tables are left alone.

    python scripts/create_tables.py
"""

from __future__ import annotations

import os
import sys

import boto3
from botocore.exceptions import ClientError

PROJECT_PREFIX = os.environ.get("PROJECT_PREFIX", "csagent")
STAGE = os.environ.get("STAGE", "dev")
REGION = os.environ.get("AWS_REGION", "us-east-1")

# (table suffix, partition key) — must match tools/common/config.py.
TABLES = [
    ("orders", "order_id"),
    ("customers", "customer_id"),
    ("refunds", "idempotency_key"),
]

TAGS = [
    {"Key": "project", "Value": PROJECT_PREFIX},
    {"Key": "stage", "Value": STAGE},
]


def main() -> int:
    dynamodb = boto3.client("dynamodb", region_name=REGION)
    created: list[str] = []

    for suffix, key in TABLES:
        name = f"{PROJECT_PREFIX}-{STAGE}-{suffix}"
        try:
            dynamodb.create_table(
                TableName=name,
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                # On-demand: this is a demo workload with no steady traffic, and
                # it avoids paying for provisioned capacity between test runs.
                BillingMode="PAY_PER_REQUEST",
                Tags=TAGS,
            )
            created.append(name)
            print(f"creating {name} (key: {key})")
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceInUseException":
                print(f"exists   {name}")
                continue
            raise

    for name in created:
        dynamodb.get_waiter("table_exists").wait(TableName=name)
        print(f"active   {name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
