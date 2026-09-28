"""MCP client for the AgentCore Gateway.

The Gateway's inbound authorizer is `AWS_IAM`, so every MCP request over
streamable HTTP is SigV4-signed with whatever credentials the caller has: the
Runtime's execution role in AgentCore, or the developer's credentials when
running locally. The runtime is granted `bedrock-agentcore:InvokeGateway` on
this gateway only — nothing else reaches the tools.

The gateway URL is not hard-coded. Deploying an in-project gateway injects
`AGENTCORE_GATEWAY_<NAME>_URL` and `..._AUTH_TYPE` into every runtime in the
project, where `<NAME>` is the gateway name upper-cased with `-` replaced by `_`.
"""

from __future__ import annotations

import os
import re
from typing import Iterator

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session
from strands.tools.mcp.mcp_client import MCPClient

GATEWAY_NAME = os.environ.get("GATEWAY_NAME", "csagent-dev-tools")
_ENV_PREFIX = f"AGENTCORE_GATEWAY_{GATEWAY_NAME.upper().replace('-', '_')}"

SERVICE = "bedrock-agentcore"

# Headers httpx or the proxy may rewrite after signing; signing them would make
# the signature unverifiable at the other end.
_UNSIGNED_HEADERS = {"connection", "content-length", "transfer-encoding", "expect"}


class SigV4Auth_(httpx.Auth):
    """Signs each outgoing MCP request with SigV4."""

    # The signature covers a hash of the body, so httpx must materialise it first.
    requires_request_body = True

    def __init__(self, service: str = SERVICE, region: str | None = None) -> None:
        self._service = service
        self._session = Session()
        self._region = region or os.environ.get("AWS_REGION") or self._session.get_config_variable("region")
        if not self._region:
            raise RuntimeError("AWS_REGION is not set; cannot sign Gateway requests.")

    def auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        credentials = self._session.get_credentials()
        if credentials is None:
            raise RuntimeError("No AWS credentials available to sign the Gateway request.")
        frozen = credentials.get_frozen_credentials()

        headers = {k: v for k, v in request.headers.items() if k.lower() not in _UNSIGNED_HEADERS}
        aws_request = AWSRequest(
            method=request.method,
            url=str(request.url),
            data=request.content,
            headers=headers,
        )
        SigV4Auth(frozen, self._service, self._region).add_auth(aws_request)

        for key, value in aws_request.headers.items():
            request.headers[key] = value
        yield request


def gateway_url() -> str:
    url = os.environ.get(f"{_ENV_PREFIX}_URL") or os.environ.get("GATEWAY_URL")
    if not url:
        raise RuntimeError(
            f"Gateway URL not found. Expected {_ENV_PREFIX}_URL (injected by `agentcore deploy`) "
            "or GATEWAY_URL for local runs."
        )
    return url


def build_gateway_client(*, expose_to_model: bool = False) -> MCPClient:
    """An MCPClient for the Gateway.

    By default every Gateway tool is hidden from the model. The agent exposes
    its own wrappers instead (`agent/agent.py`), which is what lets each call
    carry retry with backoff, a timeout, and — for refunds — an idempotency key
    the model cannot choose. 
    """
    return MCPClient(
        url=gateway_url(),
        auth_provider=SigV4Auth_(),
        tool_filters=None if expose_to_model else {"rejected": [re.compile(r".*")]},
        startup_timeout=30,
    )
