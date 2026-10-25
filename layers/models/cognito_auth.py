# cognito_auth.py
"""Shared Cognito ID token verification, used by both auth/sync (inline) and
the Lambda Authorizer that protects other routes. Kept in one place so the
verification rule only ever lives once.
"""
import json
import os
import urllib.request
from typing import Any, Optional

from jose import jwt
from jose.exceptions import JOSEError

# Loaded here (not just in each importing app.py) since the env vars below
# are read at import time, before an importer's own load_dotenv() call runs.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

USER_POOL_ID = os.environ["COGNITO_USER_POOL_ID"]
# Comma-separated: every app client whose ID tokens are accepted (more than
# one while users move from one client to another).
APP_CLIENT_IDS = {
    client_id.strip() for client_id in os.environ["COGNITO_APP_CLIENT_IDS"].split(",") if client_id.strip()
}
COGNITO_REGION = os.environ["COGNITO_REGION"]

ISSUER = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/{USER_POOL_ID}"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"

# Cached across warm invocations of the same execution environment
_jwks_cache: Optional[dict[str, Any]] = None


class AuthError(Exception):
    """Raised for any bearer token that fails verification."""


def _get_jwks() -> dict[str, Any]:
    global _jwks_cache
    if _jwks_cache is None:
        with urllib.request.urlopen(JWKS_URL, timeout=5) as response:
            _jwks_cache = json.loads(response.read())
    return _jwks_cache


def _get_signing_key(token: str) -> dict[str, Any]:
    try:
        kid = jwt.get_unverified_header(token)["kid"]
    except JOSEError as exc:
        raise AuthError("Malformed token header") from exc

    for key in _get_jwks()["keys"]:
        if key["kid"] == kid:
            return key
    raise AuthError("Signing key not found for token")


def verify_token(token: str) -> dict[str, Any]:
    """Verify a Cognito ID token's signature and standard claims, returning its payload."""
    signing_key = _get_signing_key(token)

    try:
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=ISSUER,
            # We only ever receive the ID token, not the access token it was
            # issued alongside, so there's nothing to check at_hash against.
            # The audience is checked below: jose only accepts a single one.
            options={"verify_at_hash": False, "verify_aud": False},
        )
    except JOSEError as exc:
        raise AuthError(f"Invalid token: {exc}") from exc

    # An ID token's "aud" is the app client that issued it
    audience = claims.get("aud")
    if not isinstance(audience, str) or audience not in APP_CLIENT_IDS:
        raise AuthError("Invalid token: Invalid audience")

    # Cognito issues both ID and access tokens; only ID tokens carry profile
    # claims (email, name) and an "aud" claim, so require that specifically.
    if claims.get("token_use") != "id":
        raise AuthError("Expected a Cognito ID token")

    return claims


def extract_bearer_token(header_value: Optional[str]) -> str:
    """Strip the 'Bearer ' prefix from a raw Authorization header value."""
    if not header_value or not header_value.lower().startswith("bearer "):
        raise AuthError("Missing or malformed Authorization header")
    return header_value.split(" ", 1)[1].strip()
