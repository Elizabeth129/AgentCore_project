"""Shared configuration for the tool Lambdas.

The CDK's Lambda compute config has no `envVars` field, so these cannot be
injected at deploy time. They are constants with an env override, and
`scripts/create_tables.py` builds the same names from the same convention.
"""

from __future__ import annotations

import os

PROJECT_PREFIX = os.environ.get("PROJECT_PREFIX", "csagent")
STAGE = os.environ.get("STAGE", "dev")


def _table(name: str) -> str:
    return os.environ.get(f"{name.upper()}_TABLE", f"{PROJECT_PREFIX}-{STAGE}-{name}")


ORDERS_TABLE = _table("orders")
CUSTOMERS_TABLE = _table("customers")
REFUNDS_TABLE = _table("refunds")

# The $1,000 ceiling, in integer cents. This is one of the two deterministic
# enforcement points (the other is the Cedar policy at the Gateway). It is
# deliberately not derived from anything the model can influence.
REFUND_LIMIT_CENTS = 100_000
