# app.py
"""POST /transactions

Creates a new transaction. Unprotected for now: the caller supplies `email`
directly in the request body (no auth/session to derive it from yet).
"""
import json
import traceback

import msgspec

from http_utils import json_response
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
        transaction = parse_transaction(body)
    except (json.JSONDecodeError, msgspec.ValidationError) as exc:
        return json_response(400, {"message": f"Invalid transaction: {exc}"})

    table = get_transactions_table()
    put_transaction(table, transaction)

    return json_response(201, msgspec.to_builtins(transaction))


if __name__ == "__main__":
    from datetime import date

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    from transaction import TransactionType, encode_transaction

    TEST_EMAIL = "jane@test"

    payload = encode_transaction(
        parse_transaction(
            {
                "email": TEST_EMAIL,
                "title": "Local test expense",
                "amount": "12.50",
                "categories": ["testing"],
                "date": date.today().isoformat(),
                "type": TransactionType.EXPENSE.value,
                "affectsBalance": True,
                "pending": False,
            }
        )
    )
    result = lambda_handler({"body": payload.decode()}, None)
    print(result)
