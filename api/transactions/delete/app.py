# app.py
"""DELETE /transactions?transactionId=...&date=...

Deletes a transaction. Protected by the Cognito Lambda Authorizer — `email`
comes from the verified token (requestContext.authorizer.email), not the
query string, so a caller can only ever delete their own transactions.

The delete and the reversal of its effect on account balances (see
account.py) happen atomically. Afterwards, the pending statement of any
credit card it touched is recalculated.

A pending card statement can't be deleted: it's computed from the card's
purchases and payments, so it would just be recalculated back. Deleting a
settled one is allowed (it undoes that payment).
"""
import traceback

from account import AccountNotFound, TransactionConflict, delete_transaction
from http_utils import get_authenticated_email, json_response
from statement import sync_cards_touched_by
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
    email = get_authenticated_email(event)

    params = event.get("queryStringParameters") or {}
    transaction_id = params.get("transactionId")
    date = params.get("date")
    if not transaction_id or not date:
        return json_response(
            400, {"message": "Missing required query parameters: transactionId, date"}
        )

    table = get_transactions_table()
    # Read first: reversing the balance effect needs the stored amount/type/accounts
    existing = table.get_item(Key={"email": email, "sk": build_sk(date, transaction_id)}).get("Item")
    if not existing:
        return json_response(404, {"message": "Transaction not found"})
    if existing.get("statement") and existing.get("pending"):
        return json_response(
            400,
            {
                "message": "A pending card statement can't be deleted: it's recalculated from the card's"
                " purchases and payments. Settle it to pay it."
            },
        )

    try:
        delete_transaction(existing)
    except TransactionConflict:
        return json_response(
            409, {"message": "Transaction was changed or deleted by another request; reload it and retry"}
        )
    except AccountNotFound as exc:
        # Only possible for data written before accounts existed; account.py's
        # recompute CLI creates the missing account.
        return json_response(409, {"message": f"{exc}; it must exist to reverse this transaction"})

    sync_cards_touched_by(email, [existing])

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

    event = {
        "queryStringParameters": {
            "transactionId": seed.transactionId,
            "date": seed.date.isoformat(),
        },
        "requestContext": {"authorizer": {"email": TEST_EMAIL}},
    }
    result = lambda_handler(event, None)
    print(result)
