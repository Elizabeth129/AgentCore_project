"""Turn a tool Lambda's fault injection on or off.

The Lambda compute block in `agentcore.json` has no `envVars` field, so
`FAULT_MODE` is set directly on the function. That is deliberate configuration
drift: it is a **test-only** switch, it is never committed, and the next
`agentcore deploy` does not reintroduce it. `--clear` always puts the function
back, and `run` guarantees it even if the command in between fails.

    python scripts/inject_fault.py --function csagent-orders --mode error
    python scripts/inject_fault.py --function csagent-orders --clear
    python scripts/inject_fault.py --function csagent-orders --mode error --status

Modes: error | throttle | timeout | business (see tools/common/faults.py).
"""

from __future__ import annotations

import argparse
import os
import sys

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
MODES = ("error", "throttle", "timeout", "business", "crash")

FUNCTIONS = ("csagent-orders", "csagent-customers", "csagent-refunds")


def current_env(client, function: str) -> dict[str, str]:
    config = client.get_function_configuration(FunctionName=function)
    return dict(config.get("Environment", {}).get("Variables", {}))


def apply(client, function: str, variables: dict[str, str]) -> None:
    client.update_function_configuration(
        FunctionName=function, Environment={"Variables": variables}
    )
    client.get_waiter("function_updated_v2").wait(FunctionName=function)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--function",
        default="csagent-orders",
        help=f"Lambda to affect. Known: {', '.join(FUNCTIONS)}",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mode", choices=MODES, help="fault to inject")
    group.add_argument("--clear", action="store_true", help="remove the fault")
    group.add_argument("--status", action="store_true", help="show the current setting")
    args = parser.parse_args()

    client = boto3.client("lambda", region_name=REGION)
    variables = current_env(client, args.function)

    if args.status:
        print(f"{args.function}: FAULT_MODE={variables.get('FAULT_MODE', '(unset)')}")
        return 0

    if args.clear:
        if variables.pop("FAULT_MODE", None) is None:
            print(f"{args.function}: already clear")
            return 0
        apply(client, args.function, variables)
        print(f"{args.function}: FAULT_MODE cleared")
        return 0

    variables["FAULT_MODE"] = args.mode
    apply(client, args.function, variables)
    print(f"{args.function}: FAULT_MODE={args.mode}")
    print("Remember to run with --clear when the test is done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
