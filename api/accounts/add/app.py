# app.py
"""POST /accounts

Creates a new account. Protected by the Cognito Lambda Authorizer — `email`
comes from the verified token (requestContext.authorizer.email), not from the
request body.

The server generates `accountId` and owns `balance`/`transactionCount`/
`isDefault`/`createdAt`/`updatedAt`; any of those in the body are ignored.
The starting `balance` is the optional `openingBalance`.
"""
import json
import traceback

import msgspec

from account import account_from_request, account_to_item, get_accounts_table
from http_utils import get_authenticated_email, json_response


def lambda_handler(event, context):
    # Catch-all so an unexpected crash still returns CORS headers instead of
    # a raw Lambda error the browser reports as an opaque "Failed to fetch".
    try:
        return _handle(event)
    except Exception:
        traceback.print_exc()
        return json_response(500, {"message": "Internal server error"})


def _handle(event):
    try:
        body = json.loads(event.get("body") or "{}")
        account = account_from_request(body, get_authenticated_email(event))
    except (json.JSONDecodeError, msgspec.ValidationError) as exc:
        return json_response(400, {"message": f"Invalid account: {exc}"})

    account.balance = account.openingBalance
    item = account_to_item(account)
    get_accounts_table().put_item(Item=item, ConditionExpression="attribute_not_exists(accountId)")

    return json_response(201, item)


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    TEST_EMAIL = "jane@test"

    event = {
        "body": json.dumps({"name": "CMR", "type": "credit", "paymentDay": 5, "openingBalance": "-120000"}),
        "requestContext": {"authorizer": {"email": TEST_EMAIL}},
    }
    result = lambda_handler(event, None)
    print(result)
