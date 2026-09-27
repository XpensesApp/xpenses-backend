# app.py
"""GET /subscriptions?email=...

Lists all subscriptions for a user, newest first. Unprotected for now: the
caller passes `email` as a query string parameter (no auth/session yet).
"""
import traceback

from boto3.dynamodb.conditions import Key

from http_utils import json_response
from subscription import get_subscriptions_table


def lambda_handler(event, context):
    # Catch-all so an unexpected crash still returns CORS headers instead of
    # a raw Lambda error the browser reports as an opaque "Failed to fetch".
    try:
        return _handle(event)
    except Exception:
        traceback.print_exc()
        return json_response(500, {"message": "Internal server error"})


def _handle(event):
    params = event.get("queryStringParameters") or {}
    email = params.get("email")
    if not email:
        return json_response(400, {"message": "Missing required query parameter: email"})

    table = get_subscriptions_table()
    result = table.query(KeyConditionExpression=Key("email").eq(email))

    # Unlike Transaction's sk, subscriptionId isn't date-prefixed, so newest-first
    # sorting is done here rather than via ScanIndexForward on the base table.
    items = sorted(result.get("Items", []), key=lambda item: item["createdAt"], reverse=True)

    return json_response(200, {"subscriptions": items})


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    TEST_EMAIL = "jane@test"

    result = lambda_handler({"queryStringParameters": {"email": TEST_EMAIL}}, None)
    print(result)
