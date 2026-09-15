# app.py
"""POST /auth/sync

Verifies a Cognito ID token sent as a Bearer token, then looks up the
corresponding user in the Users table and creates it if it doesn't exist yet.
"""
import json
import os
import traceback
import urllib.request
from typing import Any, Optional

from jose import jwt
from jose.exceptions import JOSEError

from models import User, get_users_table, put_user

# Load .env locally; silently skip if python-dotenv isn't installed (e.g. in Lambda)
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass
    
USER_POOL_ID = os.environ["COGNITO_USER_POOL_ID"]
APP_CLIENT_ID = os.environ["COGNITO_APP_CLIENT_ID"]
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
            audience=APP_CLIENT_ID,
            issuer=ISSUER,
            # We only ever receive the ID token, not the access token it was
            # issued alongside, so there's nothing to check at_hash against.
            options={"verify_at_hash": False},
        )
    except JOSEError as exc:
        raise AuthError(f"Invalid token: {exc}") from exc

    # Cognito issues both ID and access tokens; only ID tokens carry profile
    # claims (email, name) and an "aud" claim, so require that specifically.
    if claims.get("token_use") != "id":
        raise AuthError("Expected a Cognito ID token")

    return claims


def _extract_bearer_token(event: dict[str, Any]) -> str:
    headers = event.get("headers") or {}
    auth_header = next(
        (v for k, v in headers.items() if k.lower() == "authorization"), None
    )
    if not auth_header or not auth_header.lower().startswith("bearer "):
        raise AuthError("Missing or malformed Authorization header")
    return auth_header.split(" ", 1)[1].strip()


CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Methods": "*",
}


def _response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", **CORS_HEADERS},
        "body": json.dumps(body, default=str),
    }


def lambda_handler(event, context):
    # Catch-all so an unexpected crash still returns CORS headers instead of
    # a raw Lambda error the browser reports as an opaque "Failed to fetch".
    try:
        return _handle(event)
    except Exception:
        traceback.print_exc()
        return _response(500, {"message": "Internal server error"})


def _handle(event):
    try:
        token = _extract_bearer_token(event)
        claims = verify_token(token)
    except AuthError as exc:
        return _response(401, {"message": str(exc)})

    email = claims.get("email")
    if not email:
        return _response(401, {"message": "Token does not contain an email claim"})

    table = get_users_table()
    existing = table.get_item(Key={"email": email}).get("Item")
    if existing:
        return _response(200, {"user": existing, "created": False})

    user = User(email=email, name=claims.get("name"))
    put_user(table, user)
    return _response(
        201,
        {
            "user": {"email": user.email, "name": user.name, "createdAt": user.createdAt},
            "created": True,
        },
    )
