"""Who the caller is, according to the token — not according to the prompt.

The Runtime's inbound authorizer is `CUSTOM_JWT`, pointed at the Cognito pool's
discovery URL. AgentCore validates the token's **signature, issuer, audience and
expiry before our code runs**; a request with a missing, forged or expired token
is rejected at the door and `invoke` is never called. So by the time this module
sees the Authorization header, the token is already trustworthy, and we only
need to read the claims out of it.

We still re-check `iss` and `aud` here. Not because we distrust the authorizer,
but because those two checks are what catch a *misconfiguration* — an
`agentcore.json` pointing at a different pool than the one the tests mint tokens
from would otherwise silently serve the wrong customers.

The identity that matters is `custom:customer_id`, a Cognito user attribute.
That is what scopes the customer's memory. Nothing a customer types can change it, and no request header can override it.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import time
from typing import Any

# Canonicalised by the runtime SDK regardless of the wire casing.
AUTHORIZATION_HEADER = "Authorization"

CUSTOMER_ID_CLAIM = "custom:customer_id"

EXPECTED_ISSUER = os.environ.get("COGNITO_ISSUER")
EXPECTED_AUDIENCE = os.environ.get("COGNITO_CLIENT_ID")

# Used when there is no token at all, which in practice means a local run.
ANONYMOUS_ACTOR = "anonymous"

# The actor id becomes a memory namespace prefix, so a value containing `/`
# could reach another actor's records. Constrain it to the shape real ids have.
_ACTOR_ID_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")


class IdentityError(Exception):
    """The token is present but unusable. Treated as a failure, never as anonymous."""


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def decode_claims(token: str) -> dict[str, Any]:
    """Read a JWT's payload.

    This deliberately does NOT verify the signature — the Runtime authorizer
    already did, and re-doing it here would mean fetching and caching JWKS on
    every cold start for no additional guarantee. Never call this on a token
    that has not come through the authorizer.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise IdentityError("Authorization header is not a JWT.")
    try:
        claims = json.loads(_b64url_decode(parts[1]))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IdentityError("JWT payload could not be decoded.") from exc
    if not isinstance(claims, dict):
        raise IdentityError("JWT payload is not an object.")
    return claims


def _check_configuration(claims: dict[str, Any]) -> None:
    """Guard against pointing at the wrong pool or client."""
    if EXPECTED_ISSUER and claims.get("iss") != EXPECTED_ISSUER:
        raise IdentityError("Token issuer does not match the configured user pool.")

    if EXPECTED_AUDIENCE:
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if EXPECTED_AUDIENCE not in audiences and claims.get("client_id") != EXPECTED_AUDIENCE:
            raise IdentityError("Token audience does not match the configured app client.")

    expiry = claims.get("exp")
    if isinstance(expiry, (int, float)) and expiry < time.time():
        raise IdentityError("Token has expired.")


def _bearer_token(context: Any) -> str | None:
    headers = getattr(context, "request_headers", None) or {}
    for name, value in headers.items():
        if name.lower() == AUTHORIZATION_HEADER.lower():
            value = (value or "").strip()
            if value.lower().startswith("bearer "):
                return value[7:].strip()
            return value or None
    return None


def actor_id_from(context: Any) -> str:
    """The customer this request is for, taken from the verified token.

    Falls back to the `ACTOR_ID` environment variable only when there is no
    token at all — that is the local-development path, where no authorizer has
    run. In the deployed Runtime a request without a token never reaches here.
    """
    token = _bearer_token(context)
    if token is None:
        return _valid_or_anonymous(os.environ.get("ACTOR_ID"))

    claims = decode_claims(token)
    _check_configuration(claims)

    customer_id = claims.get(CUSTOMER_ID_CLAIM) or claims.get("sub")
    if not isinstance(customer_id, str) or not _ACTOR_ID_RE.match(customer_id):
        raise IdentityError("Token carries no usable customer identity.")
    return customer_id


def _valid_or_anonymous(value: str | None) -> str:
    if value and _ACTOR_ID_RE.match(value.strip()):
        return value.strip()
    return ANONYMOUS_ACTOR
