# app.py
"""GET /accounts

Lists the authenticated user's accounts with their current balances,
default account first, then oldest to newest.
Protected by the Cognito Lambda Authorizer — `email` comes from the verified
token (requestContext.authorizer.email).

Balances are maintained on every transaction create/edit/delete (see
account.py), so this is a single small Query regardless of how much
transaction history the user has.
"""
import traceback

from boto3.dynamodb.conditions import Key

from account import get_accounts_table
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
    email = get_authenticated_email(event)

    table = get_accounts_table()
    accounts = []
    # A user has few accounts, so return them all; the loop only guards the
    # 1 MB per-Query limit instead of exposing paging to the client.
    # Strongly consistent: clients refetch balances right after a transaction write
    query_kwargs = {"KeyConditionExpression": Key("email").eq(email), "ConsistentRead": True}
    while True:
        result = table.query(**query_kwargs)
        accounts.extend(result.get("Items", []))
        if "LastEvaluatedKey" not in result:
            break
        query_kwargs["ExclusiveStartKey"] = result["LastEvaluatedKey"]

    accounts.sort(key=lambda a: (not a.get("isDefault"), a.get("createdAt", "")))
    # json_response serializes with default=str, which handles the Decimal balances
    return json_response(200, {"accounts": accounts})


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
