# http_utils.py
import json
from typing import Any

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "*",
    "Access-Control-Allow-Methods": "*",
}


def json_response(status_code: int, body: Any) -> dict:
    """Build a Lambda-proxy JSON response with open CORS headers."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json", **CORS_HEADERS},
        "body": json.dumps(body, default=str),
    }


def get_authenticated_email(event: dict) -> str:
    """Read the verified email a Lambda Authorizer attached to the request.

    Only valid on routes actually protected by the authorizer (it populates
    requestContext.authorizer.email) — raising KeyError here on an
    unprotected route means the template's Auth wiring is missing, not that
    the caller did something wrong.
    """
    return event["requestContext"]["authorizer"]["email"]
