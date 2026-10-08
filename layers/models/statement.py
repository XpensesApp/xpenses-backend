# statement.py
"""Monthly credit card statements.

A card purchase is an `expense` on a credit account (accountId = the card),
split across `installments` monthly statements (unset = 1). Each month, on
the card's `paymentDay`, a card gets ONE pending `transfer` from the user's
default account into the card: paying the card. Settling it (PUT with the
amount actually paid, pending=false and the account it was paid from) moves
the money.

Schedule rules:
- A card's due date in a month is `paymentDay`, clamped to the month's length
  (paymentDay 31 -> Feb 28/29).
- A purchase dated before a due date is first billed on that due date; one
  dated on the due date itself or later goes to the next month. Installment k
  (1-based) is billed on the k-th due date from there.

Amounts are a closed form over the card's history, not a running total, so
any statement can be recalculated at any time from the stored transactions:

  amountDue(D) = installments due on dates in [since, D]
               - payments dated in [since, D)

`since` is the date of the card's first statement (stored on every
statement of the chain, inherited by each new one): installments due before
it are assumed paid outside the app. Payments are settled, balance-affecting
transfers into the card (paid statements included) and incomes on the card
(refunds), so partial payments and overpayments carry forward on their own.
A payment dated after a statement's date counts toward the next statement.

Breakdown of the statement for D: `lines` are the installments due after the
latest *settled* statement before D (or from `since`), and previousBalance is
the rest of amountDue (what was still owed before those; negative = credit).

Lifecycle (plan_statement / sync_card_statement):
- Only the statement for the card's most recent due date is ever created or
  recalculated. Settled statements are history and never change: edits to
  purchases they covered surface in the next statement's previousBalance.
- An older statement still pending when the next is created is rolled over:
  deleted in the same atomic write, its amount already included in the new
  one, so a card has at most one pending statement.
- A pending statement is recalculated after every transaction write that
  touches the card (sync_cards_touched_by, called by the transaction API) and
  by the daily job; it's deleted if nothing is due anymore.
"""
import calendar
import traceback
from datetime import date
from decimal import ROUND_DOWN, Decimal
from typing import NamedTuple, Optional

import msgspec

from account import (
    AccountType,
    TransactionConflict,
    account_refs,
    get_accounts_table,
    query_all_by_email,
    replace_transaction,
    swap_transactions,
)
from transaction import (
    CardStatement,
    StatementLine,
    Transaction,
    TransactionType,
    build_sk,
    get_transactions_table,
)


# ---------- Schedule ----------
def due_date(year: int, month: int, payment_day: int) -> date:
    """The card's due date in a given month, clamped to the month's length."""
    return date(year, month, min(payment_day, calendar.monthrange(year, month)[1]))


def _add_months(year: int, month: int, months: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + months
    return index // 12, index % 12 + 1


def latest_due_date(payment_day: int, today: date) -> date:
    """The most recent due date on or before `today`."""
    this_month = due_date(today.year, today.month, payment_day)
    if this_month <= today:
        return this_month
    return due_date(*_add_months(today.year, today.month, -1), payment_day)


def first_due_date(purchase_date: date, payment_day: int) -> date:
    """The due date a purchase is first billed on."""
    same_month = due_date(purchase_date.year, purchase_date.month, payment_day)
    if purchase_date < same_month:
        return same_month
    return due_date(*_add_months(purchase_date.year, purchase_date.month, 1), payment_day)


def installment_amount(total: Decimal, installments: int, number: int) -> Decimal:
    """Amount of installment `number` of `installments`.

    Equal installments rounded down to the purchase amount's precision (whole
    pesos for "120000"), with the rounding remainder on the last one, so the
    installments always add up to exactly `total`.
    """
    quantum = Decimal(1).scaleb(min(total.as_tuple().exponent, 0))
    base = (total / installments).quantize(quantum, rounding=ROUND_DOWN)
    return total - base * (installments - 1) if number == installments else base


def installment_due_date(purchase_date: date, number: int, payment_day: int) -> date:
    """The due date installment `number` (1-based) of a purchase is billed on."""
    first = first_due_date(purchase_date, payment_day)
    return due_date(*_add_months(first.year, first.month, number - 1), payment_day)


# ---------- Statement ----------
def statement_transaction_id(card_account_id: str, statement_date: date) -> str:
    """Deterministic, so the same card/date can never get two statements."""
    return f"statement-{card_account_id}-{statement_date.isoformat()}"


class StatementPlan(NamedTuple):
    """What it takes to make a card's statement for one date match the data.

    action:
      "create"  - insert `statement`, deleting the `roll_over` items in the same write
      "update"  - replace the pending `existing` item with `statement`
      "delete"  - delete the pending `roll_over` items (incl. `existing`): nothing is due anymore
      "settled" - `existing` is already paid; settled statements never change
      "none"    - already up to date, or nothing to bill
    """

    action: str
    statement: Optional[Transaction] = None
    existing: Optional[dict] = None
    roll_over: tuple = ()


def plan_statement(card: dict, transactions: list[dict], statement_date: date) -> StatementPlan:
    """Plan the card's statement for `statement_date` (its most recent due date) out of the user's stored items."""
    card_id, day = card["accountId"], statement_date.isoformat()
    chain = sorted(
        (item for item in transactions if (item.get("statement") or {}).get("accountId") == card_id),
        key=lambda item: item["date"],
    )
    existing = next((item for item in chain if item["date"] == day), None)
    if existing is not None and not existing.get("pending"):
        return StatementPlan("settled", existing=existing)
    if any(item["date"] > day for item in chain):
        # A newer statement already exists (e.g. paymentDay moved earlier): never bill backwards
        return StatementPlan("none", existing=existing)

    earlier = [item for item in chain if item["date"] < day]
    since = min((_since(item) for item in chain), default=statement_date)
    settled = [item for item in earlier if not item.get("pending")]
    lines_after = date.fromisoformat(settled[-1]["date"]) if settled else None
    stale_pending = tuple(item for item in earlier if item.get("pending"))

    statement = _build(card, transactions, statement_date, since, lines_after)
    if existing is None:
        if statement is not None:
            return StatementPlan("create", statement=statement, roll_over=stale_pending)
        # Nothing due: an older pending statement is fully covered by now
        return StatementPlan("delete", roll_over=stale_pending) if stale_pending else StatementPlan("none")

    if statement is None:
        return StatementPlan("delete", existing=existing, roll_over=(existing,))
    if _same_statement(existing, statement):
        return StatementPlan("none", existing=existing)

    # Keep what the user may have set on the pending statement; only the numbers are recalculated
    data = {k: v for k, v in existing.items() if k not in ("statement", "installments", "paymentDay")}
    updated = msgspec.convert({**data, "amount": statement.amount}, type=Transaction)
    updated.statement = statement.statement
    updated.statement.revision = int(existing["statement"].get("revision") or 0) + 1
    return StatementPlan("update", statement=updated, existing=existing)


def _build(card, transactions, statement_date, since, lines_after) -> Optional[Transaction]:
    card_id = card["accountId"]
    payment_day = int(card["paymentDay"])

    total_due = Decimal("0")
    lines = []
    for item in transactions:
        if not _is_purchase(item, card_id):
            continue
        purchase_date = date.fromisoformat(item["date"])
        installments = int(item.get("installments") or 1)
        for number in range(1, installments + 1):
            billed_on = installment_due_date(purchase_date, number, payment_day)
            if billed_on > statement_date:
                break
            if billed_on < since:
                continue
            amount = installment_amount(Decimal(item["amount"]), installments, number)
            total_due += amount
            if lines_after is None or billed_on > lines_after:
                lines.append(
                    StatementLine(
                        transactionId=item["transactionId"],
                        title=item["title"],
                        date=purchase_date,
                        installment=number,
                        installments=installments,
                        amount=amount,
                    )
                )
    lines.sort(key=lambda line: (line.date, line.transactionId, line.installment))

    paid = sum(
        (
            Decimal(item["amount"])
            for item in transactions
            if since.isoformat() <= item["date"] < statement_date.isoformat() and _is_payment(item, card_id)
        ),
        Decimal("0"),
    )
    amount_due = total_due - paid
    installments_due = sum((line.amount for line in lines), Decimal("0"))
    if not lines and amount_due <= 0:
        return None  # nothing new and nothing owed

    return Transaction(
        email=card["email"],
        title=f"Pago {card['name']} {statement_date.isoformat()}",
        amount=max(amount_due, Decimal("0")),  # a credit larger than what's due leaves nothing to pay now
        categories=[],
        date=statement_date,
        type=TransactionType.TRANSFER,
        affectsBalance=True,
        pending=True,
        transactionId=statement_transaction_id(card_id, statement_date),
        accountId=None,  # default account until the user says which account paid it
        targetAccountId=card_id,
        billingPeriod=statement_date.strftime("%Y-%m"),
        statement=CardStatement(
            accountId=card_id,
            installmentsDue=installments_due,
            previousBalance=amount_due - installments_due,
            amountDue=amount_due,
            lines=lines,
            since=since,
        ),
    )


def _since(item: dict) -> date:
    """A stored statement's chain start; statements written before `since` existed fall back to their own date."""
    return date.fromisoformat(item["statement"].get("since") or item["date"])


def _same_statement(existing: dict, statement: Transaction) -> bool:
    """Whether the stored statement already has exactly the computed numbers."""
    stored = existing["statement"]
    computed = statement.statement

    def lines(raw):
        return [(l["transactionId"], int(l["installment"]), int(l["installments"]), Decimal(l["amount"])) for l in raw]

    return (
        Decimal(existing["amount"]) == statement.amount
        and Decimal(stored["installmentsDue"]) == computed.installmentsDue
        and Decimal(stored["previousBalance"]) == computed.previousBalance
        and Decimal(stored["amountDue"]) == computed.amountDue
        and stored.get("since") == computed.since.isoformat()
        and lines(stored.get("lines", [])) == lines(msgspec.to_builtins(computed.lines, builtin_types=(Decimal,)))
    )


def _is_purchase(item: dict, card_id: str) -> bool:
    return (
        item.get("type") == TransactionType.EXPENSE.value
        and item.get("accountId") == card_id
        and item.get("affectsBalance")
        and not item.get("pending")
        and not item.get("statement")
    )


def _is_payment(item: dict, card_id: str) -> bool:
    """Settled money into the card: a transfer to it (a paid statement included) or an income on it (a refund)."""
    if not item.get("affectsBalance") or item.get("pending"):
        return False
    if item.get("type") == TransactionType.TRANSFER.value:
        return item.get("targetAccountId") == card_id
    return item.get("type") == TransactionType.INCOME.value and item.get("accountId") == card_id


def parse_statement_item(data: dict) -> CardStatement:
    """Rebuild a CardStatement from its stored map (DynamoDB hands Number attributes back as Decimal)."""
    coerced = dict(data)
    coerced["revision"] = int(data.get("revision") or 0)
    coerced["lines"] = [
        {**line, "installment": int(line["installment"]), "installments": int(line["installments"])}
        for line in data.get("lines", [])
    ]
    return msgspec.convert(coerced, type=CardStatement)


# ---------- Sync (reads + writes) ----------
def sync_card_statement(card: dict, today: date, transactions_table, transactions: Optional[list] = None) -> str:
    """Make the card's statement for its most recent due date match the stored data; returns the action taken.

    `transactions` may be a preloaded copy of the user's items (the daily job
    shares one per user); it's only trusted on the first attempt. On a
    conflict (a concurrent write changed the statement or a transaction it was
    computed from), everything is re-read and recomputed, up to 3 times.
    """
    statement_date = latest_due_date(int(card["paymentDay"]), today)
    key = {"email": card["email"], "sk": build_sk(statement_date, statement_transaction_id(card["accountId"], statement_date))}
    stored = transactions_table.get_item(Key=key, ConsistentRead=True).get("Item")
    if stored is not None and not stored.get("pending"):
        return "settled"  # cheap exit: no history read needed

    for attempt in range(3):
        if transactions is None or attempt:
            transactions = query_all_by_email(transactions_table, card["email"])
        plan = plan_statement(card, transactions, statement_date)
        try:
            if plan.action == "create":
                swap_transactions(list(plan.roll_over), [plan.statement])
            elif plan.action == "update":
                replace_transaction(plan.existing, plan.statement)
            elif plan.action == "delete":
                swap_transactions(list(plan.roll_over), [])
            return plan.action
        except TransactionConflict:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


def sync_cards_touched_by(email: str, items: list, today: Optional[date] = None) -> dict:
    """Recalculate the statement of every credit card the given transaction items touch.

    Called by the transaction API after each create/edit/delete with the old
    and/or new version of the item. Returns {cardId: action}, "failed" for a
    card that couldn't be recalculated.

    Best effort: the transaction itself is already saved by then, so a
    failure here is logged instead of raised (failing the request would make
    a client retry, duplicating the transaction). The daily statements job
    recalculates every pending statement anyway.
    """
    touched = set().union(*(account_refs(item) for item in items if item))
    if not touched:
        return {}
    try:
        cards = [
            account
            for account in query_all_by_email(get_accounts_table(), email)
            if account["accountId"] in touched
            and account.get("type") == AccountType.CREDIT.value
            and account.get("paymentDay") is not None
            and not account.get("isDefault")
        ]
    except Exception:
        traceback.print_exc()
        return {}
    today = today or date.today()
    transactions_table = get_transactions_table()
    actions = {}
    for card in cards:
        try:
            actions[card["accountId"]] = sync_card_statement(card, today, transactions_table)
        except Exception:
            print(f"Failed to recalculate the statement of {email} / {card['accountId']}:")
            traceback.print_exc()
            actions[card["accountId"]] = "failed"
    return actions
