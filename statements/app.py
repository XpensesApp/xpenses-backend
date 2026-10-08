# app.py
"""Daily credit card statements job.

Triggered by an EventBridge scheduled rule (once a day). For every credit
account, makes the statement for its most recent due date (paymentDay, on or
before today) match the stored data: creates it if missing (rolling over an
unpaid older one), recalculates it if still pending, deletes it if nothing is
due anymore. See statement.py for the rules.

Working from "most recent due date" instead of "is today the due date" makes
the job self-healing: a missed or failed run, a card created/edited after
its due date passed, or a recalculation the transaction API couldn't finish
is all caught up on the next run.

Idempotent: statement transactionIds are deterministic (card + due date),
inserts are conditional, and an up-to-date statement isn't rewritten.
Pass {"date": "YYYY-MM-DD"} in the event to run as of another day.
"""
import traceback
from datetime import date

from boto3.dynamodb.conditions import Attr

from account import AccountType, TransactionConflict, get_accounts_table, query_all_by_email
from statement import sync_card_statement
from transaction import get_transactions_table


def lambda_handler(event, context):
    today = date.fromisoformat(event["date"]) if event and event.get("date") else date.today()

    accounts_table = get_accounts_table()
    transactions_table = get_transactions_table()

    summary = {"date": today.isoformat(), "create": 0, "update": 0, "delete": 0, "settled": 0, "none": 0, "skipped": 0, "failed": 0}
    transactions_by_email: dict[str, list[dict]] = {}

    for card in _credit_accounts(accounts_table):
        account_id = card.get("accountId")
        try:
            if card.get("paymentDay") is None or card.get("isDefault"):
                # No due date to bill on, or the card is the fallback account a
                # statement's (default-account -> card) transfer would start from.
                summary["skipped"] += 1
                continue

            email = card["email"]
            # One full-history read per user per run, shared by all their cards.
            # Safe to share: a statement written for one card is pending, so it
            # never counts as a payment or purchase of another card.
            transactions = transactions_by_email.get(email)
            if transactions is None:
                transactions = transactions_by_email[email] = query_all_by_email(transactions_table, email)

            action = sync_card_statement(card, today, transactions_table, transactions)
            summary[action] += 1
        except TransactionConflict:
            # Kept clashing with concurrent writes; tomorrow's run recomputes from fresh state.
            summary["failed"] += 1
        except Exception:
            # One bad card shouldn't block the rest of the run; it's retried tomorrow.
            print(f"Failed to sync the statement of {card.get('email')} / {account_id}:")
            traceback.print_exc()
            summary["failed"] += 1

    print(summary)
    return summary


def _credit_accounts(table):
    """Every credit account across all users.

    A Scan, not a GSI Query like the subscriptions job: the Accounts table
    holds a handful of items per user, and a sparse paymentDay index would
    need paymentDay omitted (not stored as NULL) on non-credit accounts.
    Worth revisiting once the table is large.
    """
    kwargs = {"FilterExpression": Attr("type").eq(AccountType.CREDIT.value)}
    while True:
        result = table.scan(**kwargs)
        yield from result.get("Items", [])
        if "LastEvaluatedKey" not in result:
            return
        kwargs["ExclusiveStartKey"] = result["LastEvaluatedKey"]


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    print(lambda_handler({"date": date.today().isoformat()}, None))
