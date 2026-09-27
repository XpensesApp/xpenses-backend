# app.py
"""PUT /subscriptions

Updates an existing subscription (full replace). Unprotected for now: the
caller supplies `email` and the original `subscriptionId` in the body to
identify the record, plus every field (existing values included) since this
replaces the item rather than patching individual attributes.
"""
import json
import traceback

import msgspec

from http_utils import json_response
from subscription import get_subscriptions_table, parse_subscription, put_subscription


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
            "email": TEST_EMAIL,
            "title": "Edited via local test",
            "billingDay": 12,
            "type": TransactionType.EXPENSE.value,
            "affectsBalance": True,
            "status": SubscriptionStatus.DISABLED.value,
            "subscriptionId": seed.subscriptionId,
        }
    )
    result = lambda_handler({"body": body}, None)
    print(result)
