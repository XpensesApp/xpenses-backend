# subscription.py
import enum
import os
import uuid
from datetime import date as date_
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Optional

import boto3
import msgspec

from transaction import Transaction, TransactionType


class SubscriptionStatus(str, enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class Subscription(msgspec.Struct):
    # Required
    email: str  # owner, matches User.email / Transaction.email
    title: Annotated[str, msgspec.Meta(min_length=1)]
    billingDay: Annotated[int, msgspec.Meta(ge=1, le=31)]  # day of month a pending Transaction is generated
    type: TransactionType  # expense/income, shared with Transaction (e.g. a salary is an income subscription)
    affectsBalance: bool
    status: SubscriptionStatus

    # Defaulted / optional
    subscriptionId: str = msgspec.field(default_factory=lambda: uuid.uuid4().hex)
    amount: Optional[Decimal] = None  # unset for variable-amount bills (e.g. electricity) until billed
    categories: list[str] = msgspec.field(default_factory=list)
    createdAt: datetime = msgspec.field(default_factory=datetime.utcnow)
    endDate: Optional[date_] = None

    def __post_init__(self) -> None:
        # msgspec.Meta doesn't support numeric bounds on Decimal, so check here instead.
        if self.amount is not None and self.amount <= 0:
            raise ValueError("amount must be greater than 0 when provided")
        # Subscriptions carry no accounts, so the Transaction they generate
        # could never satisfy a transfer's accountId/targetAccountId rule.
        if self.type == TransactionType.TRANSFER:
            raise ValueError("type transfer is not supported for subscriptions")


# ---------- (De)serialization ----------
def decode_subscription(data: bytes | str) -> Subscription:
    """Decode + validate a Subscription from JSON bytes or string."""
    return msgspec.json.decode(data, type=Subscription)


def parse_subscription(data: dict) -> Subscription:
    """Validate + convert an already-parsed dict (e.g. an API Gateway body) into a Subscription."""
    return msgspec.convert(data, type=Subscription)


def parse_subscription_item(item: dict) -> Subscription:
    """Validate + convert a raw DynamoDB item (as returned by boto3) into a Subscription.

    Unlike parse_subscription (for client-supplied JSON-shaped dicts), this
    coerces DynamoDB-native types boto3 hands back for Number attributes —
    e.g. billingDay comes back as Decimal, not int — before validating.
    """
    coerced = dict(item)
    if "billingDay" in coerced:
        coerced["billingDay"] = int(coerced["billingDay"])
    return parse_subscription(coerced)


def encode_subscription(subscription: Subscription) -> bytes:
    """Encode a Subscription to JSON bytes."""
    return msgspec.json.encode(subscription)


# ---------- DynamoDB ----------
# Table key schema:
#   PK (partition key): email (String)          -> query all of a user's subscriptions in one Query
#   SK (sort key):      subscriptionId (String) -> unique per subscription; no natural sort key
#                                                   needed for the "list a user's subscriptions"
#                                                   use case, unlike Transaction's date-prefixed sk.
#
# GSI "BillingDayStatusIndex" (ProjectionType: ALL):
#   PK: billingDay (Number, 1-31)
#   SK: status (String: "active"/"disabled")
#
#   The main table is partitioned by email, which doesn't help the daily batch
#   job that needs "every subscription across every user due today" — that's
#   a cross-user query, which is exactly what a GSI is for. The job queries
#   this index for billingDay == <today's day-of-month> AND status == "active",
#   then filters the (small) result set in code for endDate is null or
#   endDate >= today, and converts each remaining match into a pending
#   Transaction (carrying over `amount` if set; otherwise the Transaction is
#   created with amount=0, pending=True, per Transaction's amount/pending rule).
def get_subscriptions_table():
    """Return the DynamoDB Subscriptions table resource."""
    table_name = os.environ.get("SUBSCRIPTIONS_TABLE")
    if not table_name:
        raise RuntimeError("SUBSCRIPTIONS_TABLE environment variable is not set")

    dynamodb = boto3.resource("dynamodb")
    return dynamodb.Table(table_name)


def put_subscription(table, subscription: Subscription) -> None:
    """Write a subscription to DynamoDB."""
    item = msgspec.structs.asdict(subscription)
    # DynamoDB has no native date/datetime/enum type.
    item["createdAt"] = subscription.createdAt.isoformat()
    item["endDate"] = subscription.endDate.isoformat() if subscription.endDate else None
    item["type"] = subscription.type.value
    item["status"] = subscription.status.value
    table.put_item(Item=item)


def generate_transaction_from_subscription(subscription: Subscription, billing_date: date_) -> Transaction:
    """Build the pending Transaction a subscription generates for a given billing date.

    Shared by the (future) daily worker Lambda and local testing, so the
    conversion rule only lives in one place. The transactionId is deterministic
    (subscriptionId + date) rather than random: since Transaction.sk is
    "date#transactionId", reprocessing the same subscription/date (e.g. an SQS
    redelivery) overwrites the same item instead of creating a duplicate.
    """
    return Transaction(
        email=subscription.email,
        title=subscription.title,
        amount=subscription.amount if subscription.amount is not None else Decimal("0"),
        categories=list(subscription.categories),
        date=billing_date,
        type=subscription.type,
        affectsBalance=subscription.affectsBalance,
        pending=True,  # always pending: amount (if variable) and settlement happen later
        transactionId=f"{subscription.subscriptionId}-{billing_date.isoformat()}",
        subscriptionId=subscription.subscriptionId,
        billingPeriod=billing_date.strftime("%Y-%m"),
    )


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    from transaction import get_transactions_table, put_transaction

    TEST_EMAIL = "jane@test"
    SIMULATED_TODAY = date_(2026, 9, 20)  # billingDay=20 below is the one meant to fire

    test_subscriptions = [
        Subscription(  # fixed amount, active, bills today -> should convert
            email=TEST_EMAIL, title="Netflix", billingDay=20, type=TransactionType.EXPENSE,
            affectsBalance=True, status=SubscriptionStatus.ACTIVE, amount=Decimal("9990"),
            categories=["subscriptions", "entertainment"],
        ),
        Subscription(  # variable amount (unset), active, bills today -> should convert with amount=0
            email=TEST_EMAIL, title="Cuenta de la Luz", billingDay=20, type=TransactionType.EXPENSE,
            affectsBalance=True, status=SubscriptionStatus.ACTIVE, categories=["utilities"],
        ),
        Subscription(  # active, but billingDay doesn't match today -> should be skipped
            email=TEST_EMAIL, title="Salario", billingDay=30, type=TransactionType.INCOME,
            affectsBalance=True, status=SubscriptionStatus.ACTIVE, amount=Decimal("1500000"),
            categories=["salary"],
        ),
        Subscription(  # matches today, but disabled -> should be skipped
            email=TEST_EMAIL, title="Gimnasio (cancelado)", billingDay=20, type=TransactionType.EXPENSE,
            affectsBalance=True, status=SubscriptionStatus.DISABLED, amount=Decimal("29990"),
            categories=["health"],
        ),
        Subscription(  # matches today and active, but already expired -> should be skipped
            email=TEST_EMAIL, title="Seguro Auto (vencido)", billingDay=20, type=TransactionType.EXPENSE,
            affectsBalance=True, status=SubscriptionStatus.ACTIVE, amount=Decimal("45000"),
            categories=["insurance"], endDate=date_(2026, 1, 1),
        ),
    ]

    sub_table = get_subscriptions_table()
    txn_table = get_transactions_table()

    print(f"--- Creating {len(test_subscriptions)} test subscriptions for {TEST_EMAIL} ---")
    for sub in test_subscriptions:
        put_subscription(sub_table, sub)
        print(f"  created: {sub.title} (billingDay={sub.billingDay}, status={sub.status.value})")

    # Simulates what the daily worker Lambda does for one BillingDayStatusIndex
    # match: billingDay + status are what the GSI already filtered on, so only
    # endDate needs checking here.
    print(f"\n--- Simulating billing run for {SIMULATED_TODAY.isoformat()} ---")
    for sub in test_subscriptions:
        if sub.status != SubscriptionStatus.ACTIVE:
            print(f"  skip  {sub.title}: status={sub.status.value}")
            continue
        if sub.billingDay != SIMULATED_TODAY.day:
            print(f"  skip  {sub.title}: billingDay={sub.billingDay} != today={SIMULATED_TODAY.day}")
            continue
        if sub.endDate is not None and sub.endDate < SIMULATED_TODAY:
            print(f"  skip  {sub.title}: expired {sub.endDate.isoformat()}")
            continue

        txn = generate_transaction_from_subscription(sub, SIMULATED_TODAY)
        put_transaction(txn_table, txn)
        print(f"  billed {sub.title}: amount={txn.amount} pending={txn.pending} transactionId={txn.transactionId}")

