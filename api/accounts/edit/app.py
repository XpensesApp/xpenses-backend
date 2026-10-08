# app.py
"""PUT /accounts

Updates an existing account's editable fields (full replace of `name`,
`type`, `isSavings`, `paymentDay`, `openingBalance`). Protected by the Cognito
Lambda Authorizer — `email` comes from the verified token.

The body must include `accountId`. Server-managed fields in the body are
ignored. Changing `openingBalance` shifts `balance` by the same difference,
since balance = openingBalance + transaction effects.
"""
import json
import traceback
from decimal import Decimal

import msgspec
from botocore.exceptions import ClientError

from account import account_from_request, get_accounts_table
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

    account_id = body.get("accountId")
    if not account_id:
        return json_response(400, {"message": "accountId is required to identify the account to edit"})

    email = get_authenticated_email(event)
    try:
        account = account_from_request(body, email, account_id)
    except msgspec.ValidationError as exc:
        return json_response(400, {"message": f"Invalid account: {exc}"})

    table = get_accounts_table()
    key = {"email": email, "accountId": account_id}
    existing = table.get_item(Key=key).get("Item")
    if not existing:
        return json_response(404, {"message": "Account not found"})

    old_opening = Decimal(existing.get("openingBalance") or 0)
    try:
        result = table.update_item(
            Key=key,
            UpdateExpression=(
                "SET #name = :name, #type = :type, isSavings = :isSavings, paymentDay = :paymentDay,"
                " openingBalance = :opening, updatedAt = :now ADD balance :diff"
            ),
            # Guards the read-modify-write of the balance diff against a concurrent edit/delete
            ConditionExpression="attribute_exists(accountId) AND openingBalance = :oldOpening",
            ExpressionAttributeNames={"#name": "name", "#type": "type"},  # both DynamoDB reserved words
            ExpressionAttributeValues={
                ":name": account.name,
                ":type": account.type.value,
                ":isSavings": account.isSavings,
                ":paymentDay": account.paymentDay,
                ":opening": account.openingBalance,
                ":now": account.updatedAt.isoformat(),
                ":diff": account.openingBalance - old_opening,
                ":oldOpening": old_opening,
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return json_response(
                409, {"message": "Account was changed or deleted by another request; reload it and retry"}
            )
        raise

    return json_response(200, result["Attributes"])


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    TEST_EMAIL = "jane@test"

    event = {
        "body": json.dumps({"accountId": "default", "name": "Sin cuenta", "type": "cash"}),
        "requestContext": {"authorizer": {"email": TEST_EMAIL}},
    }
    result = lambda_handler(event, None)
    print(result)
