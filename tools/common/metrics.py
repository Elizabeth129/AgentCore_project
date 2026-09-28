"""CloudWatch metrics via Embedded Metric Format.

EMF means a metric is just a specially shaped log line: CloudWatch extracts it
from the Lambda's own log group. That avoids `cloudwatch:PutMetricData` on the
Lambda roles entirely — which matters, because that action does not support
resource-level permissions, so granting it would mean a `"Resource": "*"` on
three otherwise tightly scoped roles.

Dimensions are deliberately low-cardinality (`Stage`, `Tool`). Order and refund
ids go in the log body as searchable fields, never as dimensions — a dimension
per order id would create a new metric per order and a large bill.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from .config import STAGE

NAMESPACE = "csagent"

REFUNDS_PROCESSED = "RefundsProcessed"
REFUNDS_DENIED = "RefundsDenied"
DUPLICATE_REFUND_PREVENTED = "DuplicateRefundPrevented"
TOOL_ERRORS = "ToolErrors"
TOOL_INVOCATIONS = "ToolInvocations"


def emit(metric: str, value: float = 1.0, *, tool: str, unit: str = "Count", **fields: Any) -> None:
    """Write one EMF record. Never raises: telemetry must not break a tool."""
    try:
        record = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": NAMESPACE,
                        "Dimensions": [["Stage", "Tool"]],
                        "Metrics": [{"Name": metric, "Unit": unit}],
                    }
                ],
            },
            "Stage": STAGE,
            "Tool": tool,
            metric: value,
            "event": "metric",
            **{k: v for k, v in fields.items() if v is not None},
        }
        sys.stdout.write(json.dumps(record, default=str) + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001 - a metric must never fail a refund
        pass
