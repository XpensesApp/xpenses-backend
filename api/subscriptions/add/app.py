# app.py
"""POST /subscriptions

Creates a new subscription. Protected by the Cognito Lambda Authorizer —
`email` comes from the verified token (requestContext.authorizer.email), not
from the request body, so a caller can only ever create subscriptions for
themselves. Any "email" in the body is ignored/overwritten.
"""
import json
import traceback

import msgspec

from http_utils import get_authenticated_email, json_response
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
        body["email"] = get_authenticated_email(event)
        subscription = parse_subscription(body)
    except (json.JSONDecodeError, msgspec.ValidationError) as exc:
        return json_response(400, {"message": f"Invalid subscription: {exc}"})

    table = get_subscriptions_table()
    put_subscription(table, subscription)

    return json_response(201, msgspec.to_builtins(subscription))


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    from subscription import SubscriptionStatus, encode_subscription
    from transaction import TransactionType

    TEST_EMAIL = "jane@test"

    payload = encode_subscription(
        parse_subscription(
            {
                "email": TEST_EMAIL,  # overwritten by the (simulated) authorizer context below anyway
                "title": "Local test subscription",
                "billingDay": 15,
                "type": TransactionType.EXPENSE.value,
                "affectsBalance": True,
                "status": SubscriptionStatus.ACTIVE.value,
            }
        )
    )
    event = {
        "body": payload.decode(),
        "requestContext": {"authorizer": {"email": TEST_EMAIL}},
    }
    result = lambda_handler(event, None)
    print(result)
