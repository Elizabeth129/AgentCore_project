"""DynamoDB access shared by the tool Lambdas."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import boto3
from botocore.config import Config

from . import errors

# Adaptive retries with backoff for throttling and transient 5xx (CLAUDE.md §8).
# The client is built once per container so the retry state is reused.
_CONFIG = Config(
    retries={"max_attempts": 3, "mode": "adaptive"},
    connect_timeout=2,
    read_timeout=5,
)

_resource = boto3.resource("dynamodb", config=_CONFIG)


def table(name: str):
    return _resource.Table(name)


def undecimal(value: Any) -> Any:
    """Convert DynamoDB Decimals back to int/float for JSON output.

    Money is always stored as whole cents, so a Decimal with no fractional part
    becomes an int. A fractional Decimal would mean corrupt data, so it is
    surfaced as a float rather than silently truncated.
    """
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [undecimal(v) for v in value]
    if isinstance(value, dict):
        return {k: undecimal(v) for k, v in value.items()}
    return value


def get_item(table_name: str, key: dict[str, Any]) -> dict[str, Any] | None:
    """GetItem, translating AWS faults into a retryable structured error."""
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        response = table(table_name).get_item(Key=key, ConsistentRead=True)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        raise errors.ToolError(
            errors.DEPENDENCY_UNAVAILABLE,
            f"DynamoDB {table_name} unavailable ({code}).",
            retryable=True,
        ) from exc
    except BotoCoreError as exc:
        raise errors.ToolError(
            errors.DEPENDENCY_UNAVAILABLE,
            f"DynamoDB {table_name} unreachable.",
            retryable=True,
        ) from exc

    item = response.get("Item")
    return undecimal(item) if item is not None else None
