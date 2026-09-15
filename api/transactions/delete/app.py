# app.py
"""DELETE /transactions?email=...&transactionId=...&date=...

Deletes a transaction. Unprotected for now: the caller passes the
identifying fields as query string parameters (no auth/session yet).
"""
import traceback

from botocore.exceptions import ClientError

from http_utils import json_response
from transaction import build_sk, get_transactions_table


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
    transaction_id = params.get("transactionId")
    date = params.get("date")
    if not email or not transaction_id or not date:
        return json_response(
            400, {"message": "Missing required query parameters: email, transactionId, date"}
        )

    table = get_transactions_table()
    try:
        table.delete_item(
            Key={"email": email, "sk": build_sk(date, transaction_id)},
            ConditionExpression="attribute_exists(sk)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return json_response(404, {"message": "Transaction not found"})
        raise

    return json_response(200, {"message": "Transaction deleted"})


if __name__ == "__main__":
    from datetime import date as date_

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    from transaction import TransactionType, parse_transaction, put_transaction

    TEST_EMAIL = "jane@test"

    # Seed a transaction directly so there's something to delete
    table = get_transactions_table()
    seed = parse_transaction(
        {
            "email": TEST_EMAIL,
            "title": "Seed for delete test",
            "amount": "3.00",
            "categories": ["testing"],
            "date": date_.today().isoformat(),
            "type": TransactionType.EXPENSE.value,
            "affectsBalance": True,
            "pending": False,
        }
    )
    put_transaction(table, seed)

    result = lambda_handler(
        {
            "queryStringParameters": {
                "email": TEST_EMAIL,
                "transactionId": seed.transactionId,
                "date": seed.date.isoformat(),
            }
        },
        None,
    )
    print(result)
