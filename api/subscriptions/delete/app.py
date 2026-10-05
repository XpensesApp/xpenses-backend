# app.py
"""DELETE /subscriptions?subscriptionId=...

Deletes a subscription. Protected by the Cognito Lambda Authorizer — `email`
comes from the verified token (requestContext.authorizer.email), not the
query string, so a caller can only ever delete their own subscriptions.
"""
import traceback

from botocore.exceptions import ClientError

from http_utils import get_authenticated_email, json_response
from subscription import get_subscriptions_table


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
    subscription_id = params.get("subscriptionId")
    if not subscription_id:
        return json_response(400, {"message": "Missing required query parameter: subscriptionId"})

    table = get_subscriptions_table()
    try:
        table.delete_item(
            Key={"email": email, "subscriptionId": subscription_id},
            ConditionExpression="attribute_exists(subscriptionId)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return json_response(404, {"message": "Subscription not found"})
        raise

    return json_response(200, {"message": "Subscription deleted"})


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    from subscription import SubscriptionStatus, parse_subscription, put_subscription
    from transaction import TransactionType

    TEST_EMAIL = "jane@test"

    # Seed a subscription directly so there's something to delete
    table = get_subscriptions_table()
    seed = parse_subscription(
        {
            "email": TEST_EMAIL,
            "title": "Seed for delete test",
            "billingDay": 1,
            "type": TransactionType.EXPENSE.value,
            "affectsBalance": True,
            "status": SubscriptionStatus.ACTIVE.value,
        }
    )
    put_subscription(table, seed)

    event = {
        "queryStringParameters": {"subscriptionId": seed.subscriptionId},
        "requestContext": {"authorizer": {"email": TEST_EMAIL}},
    }
    result = lambda_handler(event, None)
    print(result)
