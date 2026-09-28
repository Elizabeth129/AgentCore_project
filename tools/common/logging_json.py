"""Structured JSON logging.

One JSON object per line so CloudWatch Logs Insights can filter on the fields
directly. Only identifiers are logged — never tokens, card data, or PII beyond
the IDs.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Keys that carry an identifier we want to query on. Anything else passed to
# `log()` is included as-is, so keep call sites deliberate.
_logger = logging.getLogger("csagent.tools")


def _configure() -> None:
    # Lambda pre-installs a handler on the root logger that prepends its own
    # prefix; use our own handler and stop propagating so the line stays pure JSON.
    if _logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(_LOG_LEVEL)
    _logger.propagate = False


_configure()


def log(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured line. `event` is the queryable event name."""
    record = {"event": event, **{k: v for k, v in fields.items() if v is not None}}
    trace_id = os.environ.get("_X_AMZN_TRACE_ID")
    if trace_id:
        record["trace_id"] = trace_id
    _logger.log(level, json.dumps(record, default=str))


def warn(event: str, **fields: Any) -> None:
    log(event, level=logging.WARNING, **fields)


def error(event: str, **fields: Any) -> None:
    log(event, level=logging.ERROR, **fields)
