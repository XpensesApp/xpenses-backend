"""One-off migration to the account/transfer/credit card statement model (2026-10).

Dry run by default: prints what it would do and changes nothing. Pass
--apply to write. Every write goes through the same atomic helpers as the
API (account.py), so account balances and transactionCounts stay in sync.

Per user, in order:
  1. Default account: created if missing (statements are paid from it until settled).
  2. Two-movement transfers -> one transfer. A settled expense on account A
     plus a settled income on account B, same date and amount, A != B, and
     the only such pair for that date/amount, is merged into one `transfer`
     A -> B (both deleted and the transfer created in a single atomic write).
     This also turns a card paid the old way (expense on a debit account +
     income on the card) into a real payment into the card. REVIEW THE DRY
     RUN: two unrelated movements that happen to match would be merged too.
  3. Credit card statements: for each credit account with a paymentDay, the
     statement for its most recent due date (as of --date) is created, or
     recalculated if it's still pending, with the same code as the daily
     statements job. Earlier periods are NOT generated: installments due
     before the first statement are assumed already paid outside the app.
  4. Report only (never changed): credit accounts without paymentDay, likely
     duplicates (same date, amount and title on more than one transaction),
     and expenses whose title looks like a card payment (old style, no
     matching income).
  5. Verify: compares every account's stored balance/transactionCount with a
     rebuild from the full history. Mismatches can be repaired with
     `python layers/models/account.py <email>`.

Usage (PowerShell, from the repo root, venv active):
  $env:AWS_PROFILE = "personal"; $env:AWS_DEFAULT_REGION = "us-east-1"
  $env:TRANSACTIONS_TABLE = "xpenses-transactions-table-prod"; $env:ACCOUNTS_TABLE = "xpenses-accounts-table-prod"
  python migrations/credit_cards_2026_10.py                 # dry run, every user
  python migrations/credit_cards_2026_10.py --email a@b.c   # dry run, one user
  python migrations/credit_cards_2026_10.py --apply         # write
"""
import argparse
import os
import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "layers", "models"))

from account import (  # noqa: E402
    AccountType,
    TransactionConflict,
    account_refs,
    balance_effect,
    ensure_default_account,
    get_accounts_table,
    query_all_by_email,
    swap_transactions,
)
from statement import latest_due_date, plan_statement, sync_card_statement  # noqa: E402
from transaction import (  # noqa: E402
    DEFAULT_ACCOUNT_ID,
    Transaction,
    TransactionType,
    get_transactions_table,
    transaction_to_item,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--email", action="append", help="only this user (repeatable; default: every user)")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today(), help="as-of date, YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    missing = [name for name in ("TRANSACTIONS_TABLE", "ACCOUNTS_TABLE") if not os.environ.get(name)]
    if missing:
        sys.exit(
            f"Missing environment variable(s): {', '.join(missing)}. Set them in this terminal or add them "
            "to .env, along with AWS_PROFILE for your credentials (see the usage at the top of this file)."
        )

    transactions_table, accounts_table = get_transactions_table(), get_accounts_table()
    emails = args.email or sorted(_all_emails(transactions_table) | _all_emails(accounts_table))

    print(f"{'APPLYING' if args.apply else 'DRY RUN (nothing is written; pass --apply)'}, as of {args.date}\n")
    problems = 0
    for email in emails:
        print(f"=== {email}")
        problems += migrate_user(email, transactions_table, accounts_table, args.date, args.apply)
        print()
    print("Done." if not problems else f"Done, with {problems} item(s) to review above.")


def migrate_user(email, transactions_table, accounts_table, as_of, apply) -> int:
    problems = 0
    accounts = {a["accountId"]: a for a in query_all_by_email(accounts_table, email)}
    items = query_all_by_email(transactions_table, email)

    # 1. Default account
    if DEFAULT_ACCOUNT_ID not in accounts:
        print("[default] default account missing -> create")
        if apply:
            ensure_default_account(accounts_table, email)
            accounts = {a["accountId"]: a for a in query_all_by_email(accounts_table, email)}

    # 2. Two-movement transfers
    pairs = find_transfer_pairs(items)
    for expense, income in pairs:
        transfer = transfer_from_pair(expense, income)
        print(
            f"[transfer] {expense['date']} {Decimal(expense['amount'])}: "
            f"'{expense['title']}' (expense on {_name(accounts, transfer.accountId)}) + "
            f"'{income['title']}' (income on {_name(accounts, transfer.targetAccountId)}) "
            f"-> transfer {_name(accounts, transfer.accountId)} -> {_name(accounts, transfer.targetAccountId)}"
        )
        if apply:
            try:
                swap_transactions([expense, income], [transfer])
            except TransactionConflict:
                print("    ! skipped: one of them changed meanwhile; rerun the migration")
                problems += 1
    if apply and pairs:
        items = query_all_by_email(transactions_table, email)
    elif pairs:
        # Dry run: plan the statements as if the pairs were already merged
        paired = {item["sk"] for pair in pairs for item in pair}
        items = [i for i in items if i["sk"] not in paired] + [
            transaction_to_item(transfer_from_pair(e, i)) for e, i in pairs
        ]

    # 3. Credit card statements
    for card in (a for a in accounts.values() if a.get("type") == AccountType.CREDIT.value):
        if card.get("paymentDay") is None:
            print(f"[card] ! {card['name']} ({card['accountId']}) has no paymentDay: no statements until one is set")
            problems += 1
            continue
        if card.get("isDefault"):
            print(f"[card] ! {card['name']} is the default account: statements are paid from the default account, so it's skipped")
            problems += 1
            continue

        statement_date = latest_due_date(int(card["paymentDay"]), as_of)
        plan = plan_statement(card, items, statement_date)
        if plan.action in ("settled", "none"):
            state = "already paid" if plan.action == "settled" else "up to date" if plan.existing else "nothing due"
            print(f"[card] {card['name']}: statement {statement_date} {state}")
            continue
        if plan.action == "delete":
            print(f"[card] {card['name']}: nothing due on {statement_date} -> delete {len(plan.roll_over)} pending statement(s)")
        else:
            details = plan.statement.statement
            print(
                f"[card] {card['name']}: statement {statement_date} -> {plan.action} pending {plan.statement.amount} "
                f"({len(details.lines)} cuota(s) = {details.installmentsDue}, previous balance {details.previousBalance})"
                + (f", rolling over {len(plan.roll_over)} unpaid older one(s)" if plan.roll_over else "")
            )
            for line in details.lines:
                print(f"    {line.date} {line.title[:40]:40} {line.installment}/{line.installments} {line.amount}")
        if apply:
            try:
                sync_card_statement(card, as_of, transactions_table)
            except TransactionConflict:
                print("    ! skipped: kept changing meanwhile; rerun the migration")
                problems += 1

    # 4. Report only
    problems += report_suspects(items, accounts)

    # 5. Verify (against what's stored now; in a dry run that's the pre-migration state)
    problems += verify(email, transactions_table, accounts_table)
    return problems


def find_transfer_pairs(items):
    """(expense, income) pairs that look like one transfer recorded as two movements."""
    groups = defaultdict(lambda: ([], []))
    for item in items:
        if (
            item.get("type") not in (TransactionType.EXPENSE.value, TransactionType.INCOME.value)
            or not item.get("affectsBalance")
            or item.get("pending")
            or item.get("statement")
            or item.get("subscriptionId")
            or int(item.get("installments") or 1) != 1
        ):
            continue
        expenses, incomes = groups[(item["date"], Decimal(item["amount"]))]
        (expenses if item["type"] == TransactionType.EXPENSE.value else incomes).append(item)

    pairs = []
    for expenses, incomes in groups.values():
        # Only unambiguous matches: exactly one of each, on different accounts
        if len(expenses) == 1 and len(incomes) == 1 and _account(expenses[0]) != _account(incomes[0]):
            pairs.append((expenses[0], incomes[0]))
    return sorted(pairs, key=lambda pair: pair[0]["date"])


def transfer_from_pair(expense, income) -> Transaction:
    titles = [expense["title"]] if expense["title"] == income["title"] else [expense["title"], income["title"]]
    categories = list(dict.fromkeys(list(expense.get("categories") or []) + list(income.get("categories") or [])))
    return Transaction(
        email=expense["email"],
        title=" / ".join(titles),
        amount=Decimal(expense["amount"]),
        categories=categories,
        date=date.fromisoformat(expense["date"]),
        type=TransactionType.TRANSFER,
        affectsBalance=True,
        pending=False,
        accountId=_account(expense),
        targetAccountId=_account(income),
    )


def report_suspects(items, accounts) -> int:
    problems = 0
    regular = [i for i in items if not i.get("statement")]

    seen = defaultdict(list)
    for item in regular:
        seen[(item["date"], Decimal(item["amount"]), item["title"].strip().lower())].append(item)
    for (day, amount, _), dupes in seen.items():
        if len(dupes) > 1:
            where = ", ".join(_name(accounts, _account(d)) for d in dupes)
            print(f"[review] possible duplicate: {day} {amount} '{dupes[0]['title']}' x{len(dupes)} on {where}")
            problems += 1

    card_names = [a["name"].lower() for a in accounts.values() if a.get("type") == AccountType.CREDIT.value]
    for item in regular:
        title = item["title"].lower()
        is_card = accounts.get(_account(item), {}).get("type") == AccountType.CREDIT.value
        if (
            item.get("type") == TransactionType.EXPENSE.value
            and not is_card
            and ("tarjeta" in title or any(name and name in title for name in card_names))
        ):
            print(
                f"[review] looks like a card payment recorded as an expense: {item['date']} "
                f"{Decimal(item['amount'])} '{item['title']}' on {_name(accounts, _account(item))}. "
                "Recreate it as a transfer into the card (or settle the card's statement instead)."
            )
            problems += 1
    return problems


def verify(email, transactions_table, accounts_table) -> int:
    items = query_all_by_email(transactions_table, email)
    balances, counts = defaultdict(Decimal), defaultdict(int)
    for item in items:
        for account_id, delta in balance_effect(item).items():
            balances[account_id] += delta
        for account_id in account_refs(item):
            counts[account_id] += 1

    problems = 0
    accounts = {a["accountId"]: a for a in query_all_by_email(accounts_table, email)}
    for account_id in sorted(set(accounts) | set(counts)):
        account = accounts.get(account_id)
        if account is None:
            print(f"[verify] ! transactions reference missing account '{account_id}'")
            problems += 1
            continue
        expected = (Decimal(account.get("openingBalance") or 0) + balances[account_id], counts[account_id])
        stored = (Decimal(account.get("balance") or 0), int(account.get("transactionCount") or 0))
        if stored != expected:
            print(f"[verify] ! {account['name']}: stored balance/count {stored}, history says {expected}")
            problems += 1
    if not problems:
        print(f"[verify] {len(accounts)} account(s) consistent with {len(items)} transaction(s)")
    return problems


def _account(item) -> str:
    return item.get("accountId") or DEFAULT_ACCOUNT_ID


def _name(accounts, account_id) -> str:
    account = accounts.get(account_id)
    return f"{account['name']} ({account_id})" if account else f"'{account_id}'"


def _all_emails(table) -> set:
    emails, kwargs = set(), {"ProjectionExpression": "email"}
    while True:
        result = table.scan(**kwargs)
        emails.update(item["email"] for item in result.get("Items", []))
        if "LastEvaluatedKey" not in result:
            return emails
        kwargs["ExclusiveStartKey"] = result["LastEvaluatedKey"]


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    main()
