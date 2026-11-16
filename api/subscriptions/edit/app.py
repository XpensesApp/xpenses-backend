# app.py
"""PUT /subscriptions

Updates an existing subscription (full replace). Protected by the Cognito
Lambda Authorizer — `email` comes from the verified token
(requestContext.authorizer.email), not the body, so a caller can only ever
edit their own subscriptions. The body must still include the original
`subscriptionId` to identify the record, plus every field (existing values
included) since this replaces the item rather than patching individual
attributes.

Changing `categories` cascades: every transaction linked to the
subscription (Transaction.subscriptionId), pending or settled, gets the new
categories, even if one had been changed by hand (see
subscription.cascade_categories). The response is the updated subscription,
as before.
"""
import json
import traceback

import msgspec

from http_utils import get_authenticated_email, json_response
from subscription import cascade_categories, get_subscriptions_table, parse_subscription, put_subscription
from transaction import get_transactions_table


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

    if "subscriptionId" not in body:
        return json_response(
            400, {"message": "subscriptionId is required to identify the subscription to edit"}
        )

    body["email"] = get_authenticated_email(event)

    try:
        subscription = parse_subscription(body)
    except msgspec.ValidationError as exc:
        return json_response(400, {"message": f"Invalid subscription: {exc}"})

    table = get_subscriptions_table()
    existing = table.get_item(
        Key={"email": subscription.email, "subscriptionId": subscription.subscriptionId}
    ).get("Item")
    if not existing:
        return json_response(404, {"message": "Subscription not found"})

    if subscription.categories != (existing.get("categories") or []):
        # Before saving the subscription: if this fails partway, the stored
        # categories still differ, so retrying the same request finishes it.
        updated = cascade_categories(
            get_transactions_table(), subscription.email, subscription.subscriptionId, subscription.categories
        )
        print(f"Category cascade for subscription {subscription.subscriptionId}: {updated} transactions updated")

    put_subscription(table, subscription)
    return json_response(200, msgspec.to_builtins(subscription))


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    from subscription import SubscriptionStatus
    from transaction import TransactionType

    TEST_EMAIL = "jane@test"

    # Seed a subscription directly so there's something to edit
    table = get_subscriptions_table()
    seed = parse_subscription(
        {
            "email": TEST_EMAIL,
            "title": "Seed for edit test",
            "billingDay": 10,
            "type": TransactionType.EXPENSE.value,
            "affectsBalance": True,
            "status": SubscriptionStatus.ACTIVE.value,
        }
    )
    put_subscription(table, seed)

    body = json.dumps(
        {
            "email": TEST_EMAIL,  # overwritten by the (simulated) authorizer context below anyway
            "title": "Edited via local test",
            "billingDay": 12,
            "type": TransactionType.EXPENSE.value,
            "affectsBalance": True,
            "status": SubscriptionStatus.DISABLED.value,
            "subscriptionId": seed.subscriptionId,
        }
    )
    event = {"body": body, "requestContext": {"authorizer": {"email": TEST_EMAIL}}}
    result = lambda_handler(event, None)
    print(result)
