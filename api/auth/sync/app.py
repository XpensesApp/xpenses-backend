# app.py
"""POST /auth/sync

Verifies a Cognito ID token sent as a Bearer token, then looks up the
corresponding user in the Users table and creates it if it doesn't exist yet.

Also makes sure the user has their default account (see account.py), on
every sync rather than only when the user is created, so users that existed
before accounts did get one on their next login.
"""
import json
import traceback
from typing import Any

from account import ensure_default_account, get_accounts_table
from cognito_auth import AuthError, extract_bearer_token, verify_token

from models import User, get_users_table, put_user

# Load .env locally; silently skip if python-dotenv isn't installed (e.g. in Lambda)
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

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


def _get_auth_header(event: dict[str, Any]) -> str:
    headers = event.get("headers") or {}
    return next((v for k, v in headers.items() if k.lower() == "authorization"), "")


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
        token = extract_bearer_token(_get_auth_header(event))
        claims = verify_token(token)
    except AuthError as exc:
        return _response(401, {"message": str(exc)})

    email = claims.get("email")
    if not email:
        return _response(401, {"message": "Token does not contain an email claim"})

    # Idempotent; done before the user lookup so a crash in between is
    # repaired by the next sync instead of leaving a user without one.
    ensure_default_account(get_accounts_table(), email)

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
