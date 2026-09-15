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
