# app.py
"""Daily billing job.

Triggered by an EventBridge scheduled rule (once a day). Converts every
ACTIVE subscription whose billingDay matches today's day-of-month into a
pending Transaction, skipping subscriptions that have already expired
(endDate in the past).

Idempotent: rerunning for the same day overwrites rather than duplicates,
since generate_transaction_from_subscription builds a deterministic
transactionId (subscriptionId + date) — safe to retry the whole job (e.g.
after a partial failure) or manually re-invoke for a past date to backfill.
"""
import traceback
from datetime import date

from boto3.dynamodb.conditions import Key

from subscription import (
    SubscriptionStatus,
    generate_transaction_from_subscription,
    get_subscriptions_table,
    parse_subscription_item,
)
from transaction import get_transactions_table, put_transaction


def lambda_handler(event, context):
    # Normally EventBridge invokes this with no meaningful payload, so `today`
    # defaults to the real date. An explicit {"date": "YYYY-MM-DD"} in the
    # event allows a manual backfill/re-run for a specific day.
    today = date.fromisoformat(event["date"]) if event and event.get("date") else date.today()

    sub_table = get_subscriptions_table()
    txn_table = get_transactions_table()

    created = 0
    skipped_expired = 0
    failed = 0

    exclusive_start_key = None
    while True:
        query_kwargs = {
            "IndexName": "BillingDayStatusIndex",
            "KeyConditionExpression": (
                Key("billingDay").eq(today.day) & Key("status").eq(SubscriptionStatus.ACTIVE.value)
            ),
        }
        if exclusive_start_key:
            query_kwargs["ExclusiveStartKey"] = exclusive_start_key

        result = sub_table.query(**query_kwargs)

        for item in result.get("Items", []):
            subscription_id = item.get("subscriptionId")
            try:
                subscription = parse_subscription_item(item)

                if subscription.endDate is not None and subscription.endDate < today:
                    skipped_expired += 1
                    continue

                transaction = generate_transaction_from_subscription(subscription, today)
                put_transaction(txn_table, transaction)
                created += 1
            except Exception:
                # One bad/malformed subscription shouldn't block the rest of the run.
                print(f"Failed to bill subscription {subscription_id}:")
                traceback.print_exc()
                failed += 1

        exclusive_start_key = result.get("LastEvaluatedKey")
        if not exclusive_start_key:
            break

    summary = {"date": today.isoformat(), "created": created, "skippedExpired": skipped_expired, "failed": failed}
    print(summary)
    return summary


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    from decimal import Decimal

    from subscription import TransactionType, parse_subscription, put_subscription

    TEST_EMAIL = "jane@test"
    SIMULATED_TODAY = "2026-09-25"

    sub_table = get_subscriptions_table()
    seeds = [
        parse_subscription(
            {
                "email": TEST_EMAIL,
                "title": "Netflix (local job test)",
                "billingDay": date.fromisoformat(SIMULATED_TODAY).day,
                "type": TransactionType.EXPENSE.value,
                "affectsBalance": True,
                "status": SubscriptionStatus.ACTIVE.value,
                "amount": "9990",
                "categories": ["subscriptions"],
            }
        ),
        parse_subscription(
            {
                "email": TEST_EMAIL,
                "title": "Cuenta de la Luz (local job test)",
                "billingDay": date.fromisoformat(SIMULATED_TODAY).day,
                "type": TransactionType.EXPENSE.value,
                "affectsBalance": True,
                "status": SubscriptionStatus.ACTIVE.value,
                "categories": ["utilities"],
            }
        ),
    ]
    for seed in seeds:
        put_subscription(sub_table, seed)
        print(f"seeded: {seed.title} (subscriptionId={seed.subscriptionId})")

    print()
    result = lambda_handler({"date": SIMULATED_TODAY}, None)
    print("\nresult:", result)
