# app.py
"""GET /transactions

Lists all transactions for the authenticated user, newest first. Protected
by the Cognito Lambda Authorizer — `email` comes from the verified token
(requestContext.authorizer.email), no query parameter needed anymore.
"""
import traceback

from boto3.dynamodb.conditions import Key

from http_utils import get_authenticated_email, json_response
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
    email = get_authenticated_email(event)

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

    event = {"requestContext": {"authorizer": {"email": TEST_EMAIL}}}
    result = lambda_handler(event, None)
    print(result)
