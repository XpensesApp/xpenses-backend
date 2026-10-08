# account.py
"""Accounts, and their running balances kept in sync with the Transactions table.

An Account is user-defined (name, type, ...) plus two server-managed
counters: `balance` (openingBalance + the effect of every settled
transaction) and `transactionCount` (how many transactions reference it, so
deletion can be blocked without scanning the user's history).

Every user also has an undeletable default account (accountId
DEFAULT_ACCOUNT_ID), which transactions without an accountId fall back to.

Every write that touches a transaction (create / replace / delete) goes
through the functions below, which use a single DynamoDB TransactWriteItems
call: the transaction item and all affected accounts are written atomically,
and the write is rejected if it references an account that doesn't exist.
"""
import enum
import os
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Optional

import boto3
import msgspec
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from transaction import DEFAULT_ACCOUNT_ID, Transaction, TransactionType, transaction_to_item

DEFAULT_ACCOUNT_NAME = "General"

# Attributes that determine a transaction's effect on accounts. Edits/deletes
# require these to still hold the values that were read, so two concurrent
# requests can't both apply an account change computed from the same stale item.
BALANCE_FIELDS = ("amount", "type", "accountId", "targetAccountId", "affectsBalance", "pending")
# ...plus a card statement's revision, bumped on every recalculation, so a
# recalculation from stale history can't overwrite a newer one. Absent on
# every other transaction, which always satisfies the check.
GUARDED_FIELDS = BALANCE_FIELDS + ("statement.revision",)

# Never taken from a client request body; the server owns them.
SERVER_MANAGED_FIELDS = ("balance", "transactionCount", "isDefault", "createdAt", "updatedAt")

_serializer = TypeSerializer()


class AccountType(str, enum.Enum):
    DEBIT = "debit"
    CREDIT = "credit"
    CASH = "cash"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Account(msgspec.Struct):
    # Required
    email: str  # owner, matches User.email / Transaction.email
    name: Annotated[str, msgspec.Meta(min_length=1)]
    type: AccountType

    # Defaulted / optional
    accountId: str = msgspec.field(default_factory=lambda: uuid.uuid4().hex)
    isSavings: bool = False
    paymentDay: Optional[Annotated[int, msgspec.Meta(ge=1, le=31)]] = None  # credit accounts only
    openingBalance: Decimal = Decimal("0")  # balance before any recorded transaction

    # Server-managed
    balance: Decimal = Decimal("0")  # openingBalance + effect of every settled transaction
    transactionCount: int = 0  # transactions referencing this account (as accountId or targetAccountId)
    isDefault: bool = False
    createdAt: datetime = msgspec.field(default_factory=_utcnow)
    updatedAt: datetime = msgspec.field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if self.paymentDay is not None and self.type != AccountType.CREDIT:
            raise ValueError("paymentDay is only allowed for credit accounts")


class TransactionConflict(Exception):
    """The write was rejected: the transaction already exists (create), or it
    was changed/deleted by another request since it was read (replace/delete)."""


class AccountNotFound(Exception):
    """A transaction referenced an accountId the user doesn't have."""

    def __init__(self, account_id: str):
        super().__init__(f"Account '{account_id}' does not exist")
        self.account_id = account_id


class AccountIsPreferred(Exception):
    """The account can't be deleted while it's the user's preferred account."""


class AccountNotDeletable(Exception):
    """The account to delete doesn't exist, or transactions still reference it."""


# ---------- (De)serialization ----------
def parse_account(data: dict) -> Account:
    """Validate + convert an already-parsed dict (e.g. an API Gateway body) into an Account."""
    return msgspec.convert(data, type=Account)


def parse_account_item(item: dict) -> Account:
    """Validate + convert a raw DynamoDB item into an Account (Number attributes come back as Decimal)."""
    coerced = dict(item)
    for field in ("paymentDay", "transactionCount"):
        if coerced.get(field) is not None:
            coerced[field] = int(coerced[field])
    return parse_account(coerced)


def account_from_request(body: dict, email: str, account_id: Optional[str] = None) -> Account:
    """Build an Account from client input, dropping anything the server owns.

    account_id=None generates a fresh id (create); clients can never pick
    their own, so they can't claim DEFAULT_ACCOUNT_ID.
    """
    data = {k: v for k, v in body.items() if k not in SERVER_MANAGED_FIELDS and k != "accountId"}
    data["email"] = email
    if account_id is not None:
        data["accountId"] = account_id
    return parse_account(data)


def account_to_item(account: Account) -> dict:
    """Convert an Account into the plain dict stored in DynamoDB."""
    item = msgspec.structs.asdict(account)
    item["type"] = account.type.value
    item["createdAt"] = account.createdAt.isoformat()
    item["updatedAt"] = account.updatedAt.isoformat()
    return item


# ---------- DynamoDB ----------
# Table key schema:
#   PK (partition key): email (String)     -> one Query returns all of a user's accounts
#   SK (sort key):      accountId (String) -> uuid hex, or DEFAULT_ACCOUNT_ID for the default account
def _accounts_table_name() -> str:
    table_name = os.environ.get("ACCOUNTS_TABLE")
    if not table_name:
        raise RuntimeError("ACCOUNTS_TABLE environment variable is not set")
    return table_name


def _transactions_table_name() -> str:
    table_name = os.environ.get("TRANSACTIONS_TABLE")
    if not table_name:
        raise RuntimeError("TRANSACTIONS_TABLE environment variable is not set")
    return table_name


def get_accounts_table():
    """Return the DynamoDB Accounts table resource."""
    return boto3.resource("dynamodb").Table(_accounts_table_name())


def ensure_default_account(table, email: str) -> None:
    """Create the user's default account if it's missing; a no-op otherwise.

    Every attribute is if_not_exists, so it's safe to call on every login and
    never resets a balance or a name the user changed.
    """
    defaults = account_to_item(
        Account(email=email, name=DEFAULT_ACCOUNT_NAME, type=AccountType.CASH, accountId=DEFAULT_ACCOUNT_ID)
    )
    names, values, sets = {}, {":true": True}, ["#isDefault = :true"]
    names["#isDefault"] = "isDefault"
    for i, field in enumerate(f for f in defaults if f not in ("email", "accountId", "isDefault")):
        names[f"#f{i}"] = field  # aliased: `name` and `type` are DynamoDB reserved words
        values[f":f{i}"] = defaults[field]
        sets.append(f"#f{i} = if_not_exists(#f{i}, :f{i})")
    table.update_item(
        Key={"email": email, "accountId": DEFAULT_ACCOUNT_ID},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


# ---------- Preferred account ----------
# The account the frontend preselects for new transactions. A per-user
# setting, stored as `preferredAccountId` on the user's default account item
# (which always exists and is never deleted), so changing it is a single-item
# write and never leaves two accounts flagged. Unset means the default
# account itself. It's only a preference: server-side fallbacks (a transaction
# without accountId, subscription bills, card statements) keep using the
# default account.
PREFERRED_ATTRIBUTE = "preferredAccountId"


def preferred_account_id(accounts: list[dict]) -> str:
    """The user's preferred accountId, given all their account items.

    Falls back to the default account when unset (or, defensively, when it
    points at an account that no longer exists).
    """
    ids = {account["accountId"] for account in accounts}
    default = next((account for account in accounts if account["accountId"] == DEFAULT_ACCOUNT_ID), {})
    preferred = default.get(PREFERRED_ATTRIBUTE)
    return preferred if preferred in ids else DEFAULT_ACCOUNT_ID


def account_view(item: dict, preferred_id: str) -> dict:
    """An account item as the API returns it: with isPreferred, without the internal pointer."""
    view = {k: v for k, v in item.items() if k != PREFERRED_ATTRIBUTE}
    view["isPreferred"] = item["accountId"] == preferred_id
    return view


def set_preferred_account(email: str, account_id: str) -> None:
    """Make `account_id` the user's preferred account (DEFAULT_ACCOUNT_ID resets it).

    The default account item must exist (ensure_default_account). Raises
    AccountNotFound if the account doesn't exist; the existence check and the
    write are one transaction, so this can't race with deleting that account.
    """
    default_key = _serialize({"email": email, "accountId": DEFAULT_ACCOUNT_ID})
    if account_id == DEFAULT_ACCOUNT_ID:
        ops = [
            {
                "Update": {
                    "TableName": _accounts_table_name(),
                    "Key": default_key,
                    "UpdateExpression": "REMOVE #preferred",
                    "ConditionExpression": "attribute_exists(accountId)",
                    "ExpressionAttributeNames": {"#preferred": PREFERRED_ATTRIBUTE},
                }
            }
        ]
    else:
        ops = [
            {
                "ConditionCheck": {
                    "TableName": _accounts_table_name(),
                    "Key": _serialize({"email": email, "accountId": account_id}),
                    "ConditionExpression": "attribute_exists(accountId)",
                }
            },
            {
                "Update": {
                    "TableName": _accounts_table_name(),
                    "Key": default_key,
                    "UpdateExpression": "SET #preferred = :id",
                    "ConditionExpression": "attribute_exists(accountId)",
                    "ExpressionAttributeNames": {"#preferred": PREFERRED_ATTRIBUTE},
                    "ExpressionAttributeValues": _serialize({":id": account_id}),
                }
            },
        ]
    try:
        boto3.client("dynamodb").transact_write_items(TransactItems=ops)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        reasons = exc.response.get("CancellationReasons") or []
        if account_id != DEFAULT_ACCOUNT_ID and reasons and reasons[0].get("Code") == "ConditionalCheckFailed":
            raise AccountNotFound(account_id) from exc
        raise TransactionConflict() from exc


def delete_account(email: str, account_id: str) -> None:
    """Delete an account that no transaction references and that isn't the preferred one.

    Raises AccountIsPreferred, or AccountNotDeletable if it doesn't exist or
    still has transactions. Both conditions are checked in the same
    transaction as the delete, so neither a new transaction on the account
    nor making it preferred can slip in between.
    """
    ops = [
        {
            "Delete": {
                "TableName": _accounts_table_name(),
                "Key": _serialize({"email": email, "accountId": account_id}),
                # transactionCount is kept atomically with every transaction write
                "ConditionExpression": "attribute_exists(accountId) AND transactionCount = :zero",
                "ExpressionAttributeValues": _serialize({":zero": 0}),
            }
        },
        {
            "ConditionCheck": {
                "TableName": _accounts_table_name(),
                "Key": _serialize({"email": email, "accountId": DEFAULT_ACCOUNT_ID}),
                "ConditionExpression": "attribute_not_exists(#preferred) OR #preferred <> :id",
                "ExpressionAttributeNames": {"#preferred": PREFERRED_ATTRIBUTE},
                "ExpressionAttributeValues": _serialize({":id": account_id}),
            }
        },
    ]
    try:
        boto3.client("dynamodb").transact_write_items(TransactItems=ops)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        reasons = exc.response.get("CancellationReasons") or []
        if len(reasons) > 1 and reasons[1].get("Code") == "ConditionalCheckFailed":
            raise AccountIsPreferred() from exc
        raise AccountNotDeletable() from exc


# ---------- Transaction effects ----------
def balance_effect(item: dict) -> dict[str, Decimal]:
    """How a stored transaction item moves balances: {accountId: signed delta}.

    Only settled (pending=false), balance-affecting transactions count.
    A transfer moves `amount` from accountId to targetAccountId.
    """
    if not item or not item.get("affectsBalance") or item.get("pending"):
        return {}

    amount = Decimal(item["amount"])
    # Older items may predate the default-account fallback
    account_id = item.get("accountId") or DEFAULT_ACCOUNT_ID
    effects: dict[str, Decimal] = defaultdict(Decimal)
    if item["type"] == TransactionType.TRANSFER.value:
        effects[account_id] -= amount
        effects[item["targetAccountId"]] += amount
    else:
        effects[account_id] += amount if item["type"] == TransactionType.INCOME.value else -amount
    return {acc: delta for acc, delta in effects.items() if delta}


def account_refs(item: dict) -> set[str]:
    """Accounts a stored transaction item references, whether or not it moves their balance."""
    if not item:
        return set()
    refs = {item.get("accountId") or DEFAULT_ACCOUNT_ID}
    if item.get("targetAccountId"):
        refs.add(item["targetAccountId"])
    return refs


def _account_changes(removed: list[dict], added: list[dict]) -> dict[str, tuple[Decimal, int]]:
    """Per account, the (balance delta, transactionCount delta) of replacing the `removed` items with the `added` ones.

    Merged per account because a TransactWriteItems call can't touch the same
    item twice, and e.g. editing only the amount hits the same account on
    both sides. Accounts referenced by `added` are always included (even with
    no change) so their existence gets checked.
    """
    balances: dict[str, Decimal] = defaultdict(Decimal)
    counts: dict[str, int] = defaultdict(int)
    for items, sign in ((removed, -1), (added, 1)):
        for item in items:
            for acc, delta in balance_effect(item).items():
                balances[acc] += sign * delta
            for acc in account_refs(item):
                counts[acc] += sign

    new_refs = set().union(*(account_refs(item) for item in added))
    return {acc: (balances[acc], counts[acc]) for acc in set(balances) | set(counts) | new_refs}


# ---------- Atomic transaction writes ----------
def create_transaction(transaction: Transaction) -> None:
    """Insert a new transaction and apply it to its accounts.

    Raises TransactionConflict if a transaction with the same key already
    exists, AccountNotFound if it references a missing account.
    """
    item = transaction_to_item(transaction)
    put = {
        "Put": {
            "TableName": _transactions_table_name(),
            "Item": _serialize(item),
            "ConditionExpression": "attribute_not_exists(sk)",
        }
    }
    _transact([put], transaction.email, [], [item])


def replace_transaction(existing_item: dict, transaction: Transaction) -> None:
    """Overwrite an existing transaction, moving accounts from its old effect to the new one.

    Raises TransactionConflict if the stored item no longer matches
    `existing_item`, AccountNotFound if the new version references a missing account.
    """
    new_item = transaction_to_item(transaction)
    put = {"Put": {"TableName": _transactions_table_name(), "Item": _serialize(new_item)}}
    put["Put"].update(_unchanged_condition(existing_item))
    _transact([put], transaction.email, [existing_item], [new_item])


def delete_transaction(existing_item: dict) -> None:
    """Delete a transaction and reverse its effect on its accounts.

    Raises TransactionConflict if the stored item no longer matches `existing_item`.
    """
    _transact([_delete_op(existing_item)], existing_item["email"], [existing_item], [])


def swap_transactions(existing_items: list[dict], transactions: list[Transaction]) -> None:
    """Delete `existing_items` and insert `transactions` in one atomic write, moving accounts accordingly.

    All items must belong to the same user. Raises TransactionConflict if any
    existing item changed meanwhile or any new one already exists, and
    AccountNotFound if a new one references a missing account.
    """
    new_items = [transaction_to_item(transaction) for transaction in transactions]
    puts = [
        {
            "Put": {
                "TableName": _transactions_table_name(),
                "Item": _serialize(item),
                "ConditionExpression": "attribute_not_exists(sk)",
            }
        }
        for item in new_items
    ]
    email = (existing_items or new_items)[0]["email"]
    _transact([_delete_op(item) for item in existing_items] + puts, email, existing_items, new_items)


def _delete_op(existing_item: dict) -> dict:
    delete = {
        "Delete": {
            "TableName": _transactions_table_name(),
            "Key": _serialize({"email": existing_item["email"], "sk": existing_item["sk"]}),
        }
    }
    delete["Delete"].update(_unchanged_condition(existing_item))
    return delete


def _transact(transaction_ops: list[dict], email: str, removed: list[dict], added: list[dict]) -> None:
    now = _utcnow().isoformat()
    ops, account_ids = list(transaction_ops), []
    for account_id, (balance_delta, count_delta) in _account_changes(removed, added).items():
        key = _serialize({"email": email, "accountId": account_id})
        # attribute_exists everywhere: a missing account must fail the write,
        # never be silently created by ADD.
        if balance_delta or count_delta:
            ops.append(
                {
                    "Update": {
                        "TableName": _accounts_table_name(),
                        "Key": key,
                        "UpdateExpression": "ADD balance :balance, transactionCount :count SET updatedAt = :now",
                        "ConditionExpression": "attribute_exists(accountId)",
                        "ExpressionAttributeValues": _serialize(
                            {":balance": balance_delta, ":count": count_delta, ":now": now}
                        ),
                    }
                }
            )
        else:
            ops.append(
                {
                    "ConditionCheck": {
                        "TableName": _accounts_table_name(),
                        "Key": key,
                        "ConditionExpression": "attribute_exists(accountId)",
                    }
                }
            )
        account_ids.append(account_id)

    try:
        boto3.client("dynamodb").transact_write_items(TransactItems=ops)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "TransactionCanceledException":
            raise
        # One reason per op, in order; the transaction ops come first.
        reasons = exc.response.get("CancellationReasons") or []
        for account_id, reason in zip(account_ids, reasons[len(transaction_ops):]):
            if reason.get("Code") == "ConditionalCheckFailed":
                raise AccountNotFound(account_id) from exc
        # The transaction's own condition failed, or a concurrent write clashed
        raise TransactionConflict() from exc


def _unchanged_condition(existing_item: dict) -> dict:
    """Condition kwargs requiring the stored item to still exist with the same GUARDED_FIELDS."""
    parts = ["attribute_exists(sk)"]
    names, values = {}, {}
    for i, field in enumerate(GUARDED_FIELDS):
        # Every path segment aliased: `type` is a DynamoDB reserved word
        segments = field.split(".")
        name = ".".join(f"#f{i}_{j}" for j in range(len(segments)))
        names.update({f"#f{i}_{j}": segment for j, segment in enumerate(segments)})
        value = existing_item
        for segment in segments:
            value = value.get(segment) if isinstance(value, dict) else None
        if value is None:
            # Unset optionals are stored as NULL (or may be absent on older items)
            parts.append(f"(attribute_not_exists({name}) OR attribute_type({name}, :nullType))")
            values[":nullType"] = "NULL"
        else:
            parts.append(f"{name} = :f{i}")
            values[f":f{i}"] = value
    return {
        "ConditionExpression": " AND ".join(parts),
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": _serialize(values),
    }


def _serialize(data: dict) -> dict:
    """Plain Python dict -> low-level DynamoDB attribute-value map (needed by the client API)."""
    return {key: _serializer.serialize(value) for key, value in data.items()}


# ---------- Maintenance ----------
def recompute_accounts(email: str, transactions_table, accounts_table) -> dict[str, tuple[Decimal, int]]:
    """Rebuild a user's balances and transactionCounts from their full transaction history.

    Also creates the default account, plus a placeholder account (named after
    its id, type debit) for any accountId a transaction references that has
    no account yet, so existing transactions stay valid. For backfilling data
    written before accounts existed, or repairing drift. Not atomic with live
    traffic, so run it when the user is idle.
    """
    ensure_default_account(accounts_table, email)

    balances: dict[str, Decimal] = defaultdict(Decimal)
    counts: dict[str, int] = defaultdict(int)
    for item in query_all_by_email(transactions_table, email):
        for account_id, delta in balance_effect(item).items():
            balances[account_id] += delta
        for account_id in account_refs(item):
            counts[account_id] += 1

    existing = {item["accountId"]: item for item in query_all_by_email(accounts_table, email)}
    for account_id in set(counts) - set(existing):
        placeholder = account_to_item(
            Account(email=email, name=account_id, type=AccountType.DEBIT, accountId=account_id)
        )
        accounts_table.put_item(Item=placeholder, ConditionExpression="attribute_not_exists(accountId)")
        existing[account_id] = placeholder

    result = {}
    now = _utcnow().isoformat()
    for account_id, item in existing.items():
        balance = Decimal(item.get("openingBalance") or 0) + balances[account_id]
        accounts_table.update_item(
            Key={"email": email, "accountId": account_id},
            UpdateExpression="SET balance = :balance, transactionCount = :count, updatedAt = :now",
            ExpressionAttributeValues={":balance": balance, ":count": counts[account_id], ":now": now},
        )
        result[account_id] = (balance, counts[account_id])
    return result


def query_all_by_email(table, email: str) -> list[dict]:
    """Every item of a user's partition in `table`, following LastEvaluatedKey.

    Strongly consistent: callers recompute from what was just written (e.g. a
    statement recalculation right after the transaction that triggered it),
    and an eventually consistent read could miss that write.
    """
    items, kwargs = [], {"KeyConditionExpression": Key("email").eq(email), "ConsistentRead": True}
    while True:
        result = table.query(**kwargs)
        items.extend(result.get("Items", []))
        if "LastEvaluatedKey" not in result:
            return items
        kwargs["ExclusiveStartKey"] = result["LastEvaluatedKey"]


if __name__ == "__main__":
    # Local maintenance CLI (needs your own AWS credentials + TRANSACTIONS_TABLE/ACCOUNTS_TABLE):
    #   python account.py jane@test other@user   -> recompute those users
    #   python account.py --all                  -> recompute every user with transactions
    import sys

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    from transaction import get_transactions_table

    txn_table = get_transactions_table()
    acc_table = get_accounts_table()

    args = sys.argv[1:]
    if args == ["--all"]:
        emails, kwargs = set(), {"ProjectionExpression": "email"}
        while True:
            result = txn_table.scan(**kwargs)
            emails.update(item["email"] for item in result.get("Items", []))
            if "LastEvaluatedKey" not in result:
                break
            kwargs["ExclusiveStartKey"] = result["LastEvaluatedKey"]
    elif args:
        emails = set(args)
    else:
        sys.exit("usage: python account.py <email> [<email> ...] | --all")

    for email in sorted(emails):
        summary = recompute_accounts(email, txn_table, acc_table)
        print(email, {acc: f"balance={bal} transactions={n}" for acc, (bal, n) in summary.items()})
