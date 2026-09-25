"""Test-suite setup.

The unit tests must run with no AWS access and no deployed stack, so the
environment the agent module reads at import time is stubbed here. Importing
`agent` builds an MCP client object, which needs a URL — it never connects,
because every test that touches the gateway replaces the call.
"""

from __future__ import annotations

import os

os.environ.setdefault("GATEWAY_URL", "https://gateway.invalid/mcp")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
# Keep the agent's identity checks inert unless a test sets them deliberately.
os.environ.pop("COGNITO_ISSUER", None)
os.environ.pop("COGNITO_CLIENT_ID", None)
