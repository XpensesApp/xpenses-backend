# app.py
"""DELETE /accounts?accountId=...

Deletes an account. Protected by the Cognito Lambda Authorizer — `email`
comes from the verified token.

Refused for the default account (every user always has one), for the
user's preferred account (choose another one first), and for any account
still referenced by a transaction: those transactions would point at
nothing, and their balance effect would be lost. Move or delete them first.
"""
import traceback

from account import AccountIsPreferred, AccountNotDeletable, delete_account, get_accounts_table
from http_utils import get_authenticated_email, json_response
from transaction import DEFAULT_ACCOUNT_ID


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

    params = event.get("queryStringParameters") or {}
    account_id = params.get("accountId")
    if not account_id:
        return json_response(400, {"message": "Missing required query parameter: accountId"})
    if account_id == DEFAULT_ACCOUNT_ID:
        return json_response(400, {"message": "The default account can't be deleted"})

    try:
        delete_account(email, account_id)
    except AccountIsPreferred:
        return json_response(
            409,
            {"message": "This is your preferred account for new transactions; choose another preferred account first"},
        )
    except AccountNotDeletable:
        existing = get_accounts_table().get_item(Key={"email": email, "accountId": account_id}).get("Item")
        if not existing:
            return json_response(404, {"message": "Account not found"})
        return json_response(
            409,
            {
                "message": f"Account still has {existing['transactionCount']} transaction(s);"
                " move or delete them first"
            },
        )

    return json_response(200, {"message": "Account deleted"})


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    TEST_EMAIL = "jane@test"

    event = {
        "queryStringParameters": {"accountId": "some-account-id"},
        "requestContext": {"authorizer": {"email": TEST_EMAIL}},
    }
    result = lambda_handler(event, None)
    print(result)
