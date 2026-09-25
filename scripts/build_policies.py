"""Render the Cedar policies in gateway/policies/ into agentcore.json.

Why this script exists rather than literal Cedar in the config:

AgentCore Policy will not accept a tool-specific action with anything other than
one concrete gateway:

  * `resource`                      -> "a wildcard resource was detected"
  * `resource is AgentCore::Gateway` -> "please constrain the resource to a
    specific AgentCore::Gateway resource when creating tool-specific policies"

The gateway ARN contains the account id, which must not be hard-coded into
committed files (CLAUDE.md §6). So the `.cedar` files carry a `${GATEWAY_ARN}`
placeholder and this script substitutes the ARN of the *deployed* gateway, read
from the CloudFormation stack output. A fresh account renders its own ARN.

The statements in `agentcore.json` are therefore a build artifact. Re-run this
after any change to a `.cedar` file, and before `agentcore deploy`:

    python scripts/build_policies.py
    agentcore deploy
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
STACK_NAME = os.environ.get("AGENTCORE_STACK", "AgentCore-csagent-default")
GATEWAY_ARN_OUTPUT_SUFFIX = "ArnOutput"
GATEWAY_OUTPUT_HINT = "Gateway"

ENGINE_NAME = os.environ.get("POLICY_ENGINE_NAME", "csagent_dev_policy")

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "agentcore" / "agentcore.json"
POLICY_DIR = ROOT / "gateway" / "policies"

# Order is cosmetic — Cedar evaluation does not depend on it, and an explicit
# forbid wins regardless of position — but a stable order keeps the config diff
# readable.
#
# On validation mode. The service's policy linter does not evaluate `when`
# clauses over `context.input.*`, so it reads every tool-scoped statement as
# total and reports a finding either way:
#
#   * the two unconditional read permits  -> "Overly Permissive: Policy Engine
#     will allow every request for the specified principal, action and resource"
#   * the conditional refund forbid       -> "Overly Restrictive: Policy Engine
#     will deny every request for the specified principal, action and resource"
#
# The first is accurate and intended: any authenticated caller may read an order
# or a customer. The second is a **false positive** — the forbid fires only when
# amount_cents > 100000, which the linter cannot see. Both therefore carry
# IGNORE_ALL_FINDINGS, and the behaviour is verified live instead (see
# docs/security.md): an over-limit refund is denied, an under-limit one succeeds.
#
# `refund_limit` keeps FAIL_ON_ANY_FINDINGS. Its `when` clause makes it partial
# enough to satisfy the linter, so strict validation costs nothing there and
# still guards the policy that grants refunds.
#
# It would be easy to silence the read-permit finding by adding
# `when { context.input.order_id like "ORD-*" }`, and tempting, because it looks
# stricter. It is the wrong trade: the policy sees the raw argument the model
# produced, the handler deliberately normalises a bare "123" into "ORD-123", and
# a policy that rejects the unprefixed form would deny a legitimate lookup to
# quiet an advisory warning.
POLICIES = [
    ("permit_get_order", "permit_get_order.cedar",
     "Allow reading an order. Required: the engine denies by default.",
     "IGNORE_ALL_FINDINGS"),
    ("permit_get_customer", "permit_get_customer.cedar",
     "Allow reading a customer record. Required: the engine denies by default.",
     "IGNORE_ALL_FINDINGS"),
    ("refund_limit", "refund_limit.cedar",
     "Allow a refund only up to the $1,000 ceiling (100000 cents).",
     "FAIL_ON_ANY_FINDINGS"),
    ("forbid_refund_over_limit", "forbid_refund_over_limit.cedar",
     "Forbid refunds above $1,000 outright; a forbid cannot be overridden by any permit.",
     "IGNORE_ALL_FINDINGS"),
]


def gateway_arn() -> str:
    explicit = os.environ.get("GATEWAY_ARN")
    if explicit:
        return explicit

    cfn = boto3.client("cloudformation", region_name=REGION)
    outputs = cfn.describe_stacks(StackName=STACK_NAME)["Stacks"][0].get("Outputs", [])
    for output in outputs:
        key = output["OutputKey"]
        if GATEWAY_OUTPUT_HINT in key and key.endswith(GATEWAY_ARN_OUTPUT_SUFFIX):
            return output["OutputValue"]
    raise SystemExit(
        f"No gateway ARN output found on stack {STACK_NAME}. "
        "Deploy the gateway first, or set GATEWAY_ARN."
    )


def main() -> int:
    arn = gateway_arn()
    print(f"gateway: {arn}")

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    engines = config.get("policyEngines", [])
    try:
        engine = next(e for e in engines if e["name"] == ENGINE_NAME)
    except StopIteration:
        raise SystemExit(f"No policy engine named {ENGINE_NAME!r} in {CONFIG_PATH}.")

    rendered = []
    for name, filename, description, validation_mode in POLICIES:
        template = (POLICY_DIR / filename).read_text(encoding="utf-8")
        if "${GATEWAY_ARN}" not in template:
            raise SystemExit(f"{filename} has no ${{GATEWAY_ARN}} placeholder — refusing to deploy it.")
        rendered.append(
            {
                "name": name,
                "description": description,
                "statement": template.replace("${GATEWAY_ARN}", arn).strip(),
                "sourceFile": f"gateway/policies/{filename}",
                "validationMode": validation_mode,
                # ACTIVE, never LOG_ONLY: a shadow-mode refund ceiling is not a
                # ceiling. LOG_ONLY belongs in a rollout, not in the committed state.
                "enforcementMode": "ACTIVE",
                # INITIATE: decide before the tool runs. RETURN_OUTPUT would let
                # the refund happen and then object to the answer.
                "authorizationPhase": "INITIATE",
            }
        )

    engine["policies"] = rendered
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

    print(f"rendered {len(rendered)} policies into agentcore/agentcore.json:")
    for policy in rendered:
        print(f"  {effect_of(policy['statement']):<7} {policy['name']}")
    return 0


def effect_of(statement: str) -> str:
    """The Cedar effect, ignoring the comment header each file starts with."""
    for line in statement.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        return "forbid" if stripped.startswith("forbid") else "permit"
    return "?"


if __name__ == "__main__":
    sys.exit(main())
