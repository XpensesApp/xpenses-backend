# transaction.py
import enum
import os
import uuid
from datetime import date as date_
from decimal import Decimal
from typing import Annotated, Optional

import boto3
import msgspec


class TransactionType(str, enum.Enum):
    EXPENSE = "expense"
    INCOME = "income"


class Transaction(msgspec.Struct):
    # Required
    email: str  # matches User.email in api/auth/sync/models.py
    title: Annotated[str, msgspec.Meta(min_length=1)]
    amount: Decimal  # magnitude only; `type` gives direction (bounds checked in __post_init__)
    categories: list[str]
    date: date_
    type: TransactionType
    affectsBalance: bool
    pending: bool

    # Defaulted / optional
    transactionId: str = msgspec.field(default_factory=lambda: uuid.uuid4().hex)
    sk: str = ""  # DynamoDB sort key; computed in __post_init__ if not already set
    accountId: Optional[str] = None
    installments: Optional[Annotated[int, msgspec.Meta(ge=1)]] = None
    paymentDay: Optional[Annotated[int, msgspec.Meta(ge=1, le=31)]] = None
    subscriptionId: Optional[str] = None
    billingPeriod: Optional[str] = None

    def __post_init__(self) -> None:
        # msgspec.Meta doesn't support numeric bounds on Decimal, so check here instead.
        # A pending transaction from a variable-amount subscription may not have
        # an amount yet (set later when the user pays it), so 0 is allowed only
        # while pending; a settled (non-pending) transaction must be > 0.
        if self.pending:
            if self.amount < 0:
                raise ValueError("amount must be >= 0 when pending is true")
        elif self.amount <= 0:
            raise ValueError("amount must be greater than 0")

        # Only derive sk on fresh creation; preserve it as-is when rehydrating
        # an item that already has one (e.g. read back from DynamoDB).
        if not self.sk:
            self.sk = build_sk(self.date, self.transactionId)


# ---------- (De)serialization ----------
def decode_transaction(data: bytes | str) -> Transaction:
    """Decode + validate a Transaction from JSON bytes or string."""
    return msgspec.json.decode(data, type=Transaction)


def parse_transaction(data: dict) -> Transaction:
    """Validate + convert an already-parsed dict (e.g. an API Gateway body) into a Transaction."""
    return msgspec.convert(data, type=Transaction)


def encode_transaction(transaction: Transaction) -> bytes:
    """Encode a Transaction to JSON bytes."""
    return msgspec.json.encode(transaction)


# ---------- DynamoDB ----------
# Table key schema:
#   PK (partition key): email (String)  -> query all of a user's transactions in one Query
#   SK (sort key):      sk (String)     -> "<date ISO-8601>#<transactionId>"
#                                           date prefix keeps results sortable/queryable by
#                                           date range; transactionId suffix guarantees
#                                           uniqueness for same-day transactions.
def build_sk(date: date_ | str, transaction_id: str) -> str:
    """Build the table's sort key value from a date (date or ISO string) and transactionId."""
    iso_date = date if isinstance(date, str) else date.isoformat()
    return f"{iso_date}#{transaction_id}"


def get_transactions_table():
    """Return the DynamoDB Transactions table resource."""
    table_name = os.environ.get("TRANSACTIONS_TABLE")
    if not table_name:
        raise RuntimeError("TRANSACTIONS_TABLE environment variable is not set")

    dynamodb = boto3.resource("dynamodb")
    return dynamodb.Table(table_name)


def put_transaction(table, transaction: Transaction) -> None:
    """Write a transaction to DynamoDB."""
    item = msgspec.structs.asdict(transaction)
    # DynamoDB has no native date/enum type; Decimal (amount) is already Dynamo-compatible.
    item["date"] = transaction.date.isoformat()
    item["type"] = transaction.type.value
    table.put_item(Item=item)
