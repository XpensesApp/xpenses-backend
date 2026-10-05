# app.py
"""PUT /transactions

Updates an existing transaction (full replace). Protected by the Cognito
Lambda Authorizer — `email` comes from the verified token
(requestContext.authorizer.email), not the body, so a caller can only ever
edit their own transactions. The body must still include the original
`transactionId`/`date` to identify the record, plus every field (existing
values included) since this replaces the item rather than patching
individual attributes.
"""
import json
import traceback

import msgspec

from http_utils import get_authenticated_email, json_response
from transaction import get_transactions_table, parse_transaction, put_transaction


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

    if "transactionId" not in body:
        return json_response(
            400, {"message": "transactionId is required to identify the transaction to edit"}
        )

    body["email"] = get_authenticated_email(event)

    try:
        transaction = parse_transaction(body)
    except msgspec.ValidationError as exc:
        return json_response(400, {"message": f"Invalid transaction: {exc}"})

    table = get_transactions_table()
    existing = table.get_item(Key={"email": transaction.email, "sk": transaction.sk}).get("Item")
    if not existing:
        return json_response(404, {"message": "Transaction not found"})

    put_transaction(table, transaction)
    return json_response(200, msgspec.to_builtins(transaction))


if __name__ == "__main__":
    from datetime import date

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    from transaction import TransactionType

    TEST_EMAIL = "jane@test"

    # Seed a transaction directly so there's something to edit
    table = get_transactions_table()
    seed = parse_transaction(
        {
            "email": TEST_EMAIL,
            "title": "Seed for edit test",
            "amount": "5.00",
            "categories": ["testing"],
            "date": date.today().isoformat(),
            "type": TransactionType.EXPENSE.value,
            "affectsBalance": True,
            "pending": False,
        }
    )
    put_transaction(table, seed)

    body = json.dumps(
        {
            "email": TEST_EMAIL,  # overwritten by the (simulated) authorizer context below anyway
            "title": "Edited via local test",
            "amount": "7.25",
            "categories": ["testing", "edited"],
            "date": seed.date.isoformat(),
            "type": TransactionType.EXPENSE.value,
            "affectsBalance": True,
            "pending": False,
            "transactionId": seed.transactionId,
        }
    )
    event = {"body": body, "requestContext": {"authorizer": {"email": TEST_EMAIL}}}
    result = lambda_handler(event, None)
    print(result)
