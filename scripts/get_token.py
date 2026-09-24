"""Sign the test user in and print a Cognito ID token.

The password is read from Secrets Manager, never from a file or an argument, so
nothing long-lived lives in the repo or in shell history. The token itself is
short-lived (60 minutes).

    python scripts/get_token.py                       # the ID token
    agentcore invoke --prompt "..." --bearer-token "$(python scripts/get_token.py)"

The ID token, not the access token, is what the Runtime is configured to accept:
Cognito puts custom attributes such as `custom:customer_id` in the ID token, and
that claim is how the agent knows which customer it is serving.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys

import boto3

PROJECT_PREFIX = os.environ.get("PROJECT_PREFIX", "csagent")
STAGE = os.environ.get("STAGE", "dev")
REGION = os.environ.get("AWS_REGION", "us-east-1")

POOL_NAME = f"{PROJECT_PREFIX}-{STAGE}-users"
CLIENT_NAME = f"{PROJECT_PREFIX}-{STAGE}-agent-client"
SECRET_NAME = f"{PROJECT_PREFIX}/{STAGE}/test-user"

TEST_EMAIL = "dana.osei@example.com"


def resolve_pool(cognito) -> str:
    explicit = os.environ.get("COGNITO_USER_POOL_ID")
    if explicit:
        return explicit
    paginator = cognito.get_paginator("list_user_pools")
    for page in paginator.paginate(MaxResults=60):
        for pool in page["UserPools"]:
            if pool["Name"] == POOL_NAME:
                return pool["Id"]
    raise SystemExit(f"No user pool named {POOL_NAME}. Run scripts/create_cognito.py first.")


def resolve_client(cognito, pool_id: str) -> str:
    explicit = os.environ.get("COGNITO_CLIENT_ID")
    if explicit:
        return explicit
    for client in cognito.list_user_pool_clients(UserPoolId=pool_id, MaxResults=60)["UserPoolClients"]:
        if client["ClientName"] == CLIENT_NAME:
            return client["ClientId"]
    raise SystemExit(f"No app client named {CLIENT_NAME}. Run scripts/create_cognito.py first.")


def decode_claims(token: str) -> dict:
    """Read a JWT's payload for display. This does NOT verify the signature."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", default=TEST_EMAIL)
    parser.add_argument("--claims", action="store_true", help="print the token's claims instead")
    args = parser.parse_args()

    cognito = boto3.client("cognito-idp", region_name=REGION)
    secrets_client = boto3.client("secretsmanager", region_name=REGION)

    pool_id = resolve_pool(cognito)
    client_id = resolve_client(cognito, pool_id)
    password = json.loads(
        secrets_client.get_secret_value(SecretId=SECRET_NAME)["SecretString"]
    )["password"]

    response = cognito.initiate_auth(
        ClientId=client_id,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": args.username, "PASSWORD": password},
    )
    token = response["AuthenticationResult"]["IdToken"]

    if args.claims:
        print(json.dumps(decode_claims(token), indent=2))
    else:
        # No trailing newline: this is meant to be captured into a variable.
        sys.stdout.write(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
