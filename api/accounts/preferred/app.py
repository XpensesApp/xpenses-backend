# app.py
"""PUT /accounts/preferred

Sets the user's preferred account: the one the frontend preselects when
creating a new transaction. Protected by the Cognito Lambda Authorizer —
`email` comes from the verified token.

Body: {"accountId": "<id>"}. Any of the user's accounts can be preferred;
"default" resets it to the default account. Afterwards, GET /accounts marks
it with `isPreferred: true`, and it can't be deleted until another one is
chosen. It's only a preference: a transaction sent without accountId still
goes to the default account.
"""
import json
import traceback

from account import AccountNotFound, TransactionConflict, ensure_default_account, get_accounts_table, set_preferred_account
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
    except json.JSONDecodeError as exc:
        return json_response(400, {"message": f"Invalid JSON: {exc}"})

    account_id = body.get("accountId") if isinstance(body, dict) else None
    if not isinstance(account_id, str) or not account_id:
        return json_response(400, {"message": "accountId is required"})

    email = get_authenticated_email(event)
    # The preference lives on the default account item, so make sure it exists
    ensure_default_account(get_accounts_table(), email)
    try:
        set_preferred_account(email, account_id)
    except AccountNotFound as exc:
        return json_response(404, {"message": str(exc)})
    except TransactionConflict:
        return json_response(409, {"message": "Accounts changed meanwhile; reload them and retry"})

    return json_response(200, {"preferredAccountId": account_id})


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    TEST_EMAIL = "jane@test"

    event = {"body": json.dumps({"accountId": "default"}), "requestContext": {"authorizer": {"email": TEST_EMAIL}}}
    print(lambda_handler(event, None))
