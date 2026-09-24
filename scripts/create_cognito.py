"""Create the Cognito user pool that guards inbound calls to the Runtime.

The pool issues the JWT a caller must present. The agent reads the customer's
identity from that token's claims, so `custom:customer_id` on the user — not
anything the caller types — decides whose memory and orders are in scope.

No password is ever written to a file or printed into the repo. A random one is
generated on first run and stored in Secrets Manager; `scripts/get_token.py`
reads it back from there. Re-running the script reuses what already exists.

    python scripts/create_cognito.py                  # create/repair
    python scripts/create_cognito.py --write-config   # also patch agentcore.json

Cognito is not part of the AgentCore CDK schema, so it is scripted here
(CLAUDE.md §4).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import string
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

PROJECT_PREFIX = os.environ.get("PROJECT_PREFIX", "csagent")
STAGE = os.environ.get("STAGE", "dev")
REGION = os.environ.get("AWS_REGION", "us-east-1")

POOL_NAME = f"{PROJECT_PREFIX}-{STAGE}-users"
CLIENT_NAME = f"{PROJECT_PREFIX}-{STAGE}-agent-client"
SECRET_NAME = f"{PROJECT_PREFIX}/{STAGE}/test-user"

# The demo customer. Matches the seeded DynamoDB fixture so the agent can look
# this person's real orders up.
TEST_USERNAME = "dana"
TEST_EMAIL = "dana.osei@example.com"
TEST_CUSTOMER_ID = "CUST-001"

CONFIG_PATH = Path(__file__).resolve().parents[1] / "agentcore" / "agentcore.json"

TAGS = {"project": PROJECT_PREFIX, "stage": STAGE}


def _generate_password() -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_"
    # Cognito's default policy wants upper, lower, digit and symbol; sampling
    # one of each first guarantees it rather than hoping.
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*-_"),
    ]
    rest = [secrets.choice(alphabet) for _ in range(20)]
    chars = required + rest
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def find_pool(cognito) -> str | None:
    paginator = cognito.get_paginator("list_user_pools")
    for page in paginator.paginate(MaxResults=60):
        for pool in page["UserPools"]:
            if pool["Name"] == POOL_NAME:
                return pool["Id"]
    return None


def ensure_pool(cognito) -> str:
    existing = find_pool(cognito)
    if existing:
        print(f"exists   user pool {POOL_NAME} ({existing})")
        return existing

    response = cognito.create_user_pool(
        PoolName=POOL_NAME,
        Policies={
            "PasswordPolicy": {
                "MinimumLength": 12,
                "RequireUppercase": True,
                "RequireLowercase": True,
                "RequireNumbers": True,
                "RequireSymbols": True,
            }
        },
        Schema=[
            {"Name": "email", "AttributeDataType": "String", "Required": True, "Mutable": True},
            {
                # The claim the agent trusts. Mutable so support can re-link a
                # login to a different customer record without recreating it.
                "Name": "customer_id",
                "AttributeDataType": "String",
                "Mutable": True,
                "StringAttributeConstraints": {"MinLength": "1", "MaxLength": "64"},
            },
        ],
        AutoVerifiedAttributes=["email"],
        UsernameAttributes=["email"],
        UserPoolTags=TAGS,
    )
    pool_id = response["UserPool"]["Id"]
    print(f"created  user pool {POOL_NAME} ({pool_id})")
    return pool_id


def ensure_client(cognito, pool_id: str) -> str:
    for client in cognito.list_user_pool_clients(UserPoolId=pool_id, MaxResults=60)["UserPoolClients"]:
        if client["ClientName"] == CLIENT_NAME:
            print(f"exists   app client {CLIENT_NAME} ({client['ClientId']})")
            return client["ClientId"]

    response = cognito.create_user_pool_client(
        UserPoolId=pool_id,
        ClientName=CLIENT_NAME,
        # Public client: no secret to store or leak. The test harness signs in
        # with a username and password read from Secrets Manager.
        GenerateSecret=False,
        ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
        ReadAttributes=["email", "custom:customer_id"],
        # Short-lived by design; the refresh token is what lasts.
        IdTokenValidity=60,
        AccessTokenValidity=60,
        TokenValidityUnits={"IdToken": "minutes", "AccessToken": "minutes"},
        PreventUserExistenceErrors="ENABLED",
    )
    client_id = response["UserPoolClient"]["ClientId"]
    print(f"created  app client {CLIENT_NAME} ({client_id})")
    return client_id


def ensure_password(secrets_client) -> str:
    try:
        stored = secrets_client.get_secret_value(SecretId=SECRET_NAME)
        print(f"exists   secret {SECRET_NAME}")
        return json.loads(stored["SecretString"])["password"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    password = _generate_password()
    secrets_client.create_secret(
        Name=SECRET_NAME,
        Description="Password for the Cognito test user used by the csagent scenario tests.",
        SecretString=json.dumps({"username": TEST_USERNAME, "password": password}),
        Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()],
    )
    print(f"created  secret {SECRET_NAME}")
    return password


def ensure_user(cognito, pool_id: str, password: str) -> None:
    try:
        cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=TEST_EMAIL,
            UserAttributes=[
                {"Name": "email", "Value": TEST_EMAIL},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "custom:customer_id", "Value": TEST_CUSTOMER_ID},
            ],
            MessageAction="SUPPRESS",
        )
        print(f"created  user {TEST_EMAIL} (custom:customer_id={TEST_CUSTOMER_ID})")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "UsernameExistsException":
            raise
        print(f"exists   user {TEST_EMAIL}")

    # Always (re)assert the password so the stored secret and the pool agree,
    # and clear the FORCE_CHANGE_PASSWORD state a new user starts in.
    cognito.admin_set_user_password(
        UserPoolId=pool_id, Username=TEST_EMAIL, Password=password, Permanent=True
    )


def discovery_url(pool_id: str) -> str:
    return f"https://cognito-idp.{REGION}.amazonaws.com/{pool_id}/.well-known/openid-configuration"


def write_config(pool_id: str, client_id: str) -> None:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    for runtime in config["runtimes"]:
        runtime["authorizerType"] = "CUSTOM_JWT"
        runtime["authorizerConfiguration"] = {
            "customJwtAuthorizer": {
                "discoveryUrl": discovery_url(pool_id),
                "allowedAudience": [client_id],
            }
        }
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"patched  {CONFIG_PATH.relative_to(CONFIG_PATH.parents[1])}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write-config",
        action="store_true",
        help="patch agentcore.json with the pool's discovery URL and audience",
    )
    args = parser.parse_args()

    cognito = boto3.client("cognito-idp", region_name=REGION)
    secrets_client = boto3.client("secretsmanager", region_name=REGION)

    pool_id = ensure_pool(cognito)
    client_id = ensure_client(cognito, pool_id)
    password = ensure_password(secrets_client)
    ensure_user(cognito, pool_id, password)

    if args.write_config:
        write_config(pool_id, client_id)

    print("\nAdd to .env:")
    print(f"  COGNITO_USER_POOL_ID={pool_id}")
    print(f"  COGNITO_CLIENT_ID={client_id}")
    print(f"\ndiscoveryUrl: {discovery_url(pool_id)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
