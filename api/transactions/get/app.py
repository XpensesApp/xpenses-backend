# app.py
"""GET /transactions?email=...

Lists all transactions for a user, newest first. Unprotected for now: the
caller passes `email` as a query string parameter (no auth/session yet).
"""
import traceback

from boto3.dynamodb.conditions import Key

from http_utils import json_response
from transaction import get_transactions_table


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

    table = get_transactions_table()
    result = table.query(
        KeyConditionExpression=Key("email").eq(email),
        ScanIndexForward=False,  # newest first (sk is date-prefixed)
    )

    # json_response serializes with default=str, which handles the Decimal amounts
    return json_response(200, {"transactions": result.get("Items", [])})


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    TEST_EMAIL = "jane@test"

    result = lambda_handler({"queryStringParameters": {"email": TEST_EMAIL}}, None)
    print(result)
