"""One-off migration: give every existing transaction a `full_date_utc` (2026-10).

Transactions written before the field existed only have `date`, a Chilean
calendar date. Each one gets 00:00 Chilean time on that date, converted to
UTC: the same fallback a new transaction gets when no time is sent (see
transaction.py). For example 2026-10-08 -> 2026-10-08T03:00:00Z (summer time,
UTC-3) and 2026-07-15 -> 2026-07-15T04:00:00Z (winter time, UTC-4).

Dry run by default: prints what it would do and changes nothing. Pass
--apply to write. It only adds the attribute, so balances and every other
field are untouched. It never overwrites a value that's already there, so
it's safe to rerun and to run while the API is live. Run it AFTER deploying
the code that writes the field; before that, an edit would drop it again.

Usage (PowerShell, from the repo root, venv active):
  pip install tzdata==2026.5    # timezone data; Windows has none of its own
  $env:AWS_PROFILE = "personal"; $env:AWS_DEFAULT_REGION = "us-east-1"
  $env:TRANSACTIONS_TABLE = "xpenses-transactions-table-prod"
  python migrations/full_date_utc_2026_10.py                 # dry run, every user
  python migrations/full_date_utc_2026_10.py --email a@b.c   # dry run, one user
  python migrations/full_date_utc_2026_10.py --apply         # write
"""
import argparse
import os
import sys
from datetime import date

from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "layers", "models"))

from account import query_all_by_email  # noqa: E402
from transaction import format_utc, get_transactions_table, start_of_day_utc  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--email", action="append", help="only this user (repeatable; default: every user)")
    args = parser.parse_args()

    if not os.environ.get("TRANSACTIONS_TABLE"):
        sys.exit(
            "Missing environment variable: TRANSACTIONS_TABLE. Set it in this terminal or add it to .env, "
            "along with AWS_PROFILE for your credentials (see the usage at the top of this file)."
        )

    table = get_transactions_table()
    if args.email:
        items = [item for email in args.email for item in query_all_by_email(table, email)]
    else:
        items = _scan_all(table)
    missing = sorted((i for i in items if not i.get("full_date_utc")), key=lambda i: (i["email"], i["sk"]))

    print(f"{'APPLYING' if args.apply else 'DRY RUN (nothing is written; pass --apply)'}")
    print(f"{len(missing)} of {len(items)} transaction(s) have no full_date_utc\n")

    added = skipped = 0
    for item in missing:
        value = format_utc(start_of_day_utc(date.fromisoformat(item["date"])))
        print(f"{item['email']}  {item['date']}  {item.get('title', '')[:40]:40}  -> {value}")
        if not args.apply:
            continue
        try:
            table.update_item(
                Key={"email": item["email"], "sk": item["sk"]},
                UpdateExpression="SET full_date_utc = :value",
                # Don't recreate a transaction deleted meanwhile (update_item would
                # insert a stub) or overwrite a value the API wrote meanwhile.
                ConditionExpression="attribute_exists(sk) AND attribute_not_exists(full_date_utc)",
                ExpressionAttributeValues={":value": value},
            )
            added += 1
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            print("    skipped: deleted or given a full_date_utc meanwhile")
            skipped += 1

    if args.apply:
        print(f"\nDone: {added} added, {skipped} skipped.")


def _scan_all(table) -> list:
    items, kwargs = [], {}
    while True:
        result = table.scan(**kwargs)
        items.extend(result.get("Items", []))
        if "LastEvaluatedKey" not in result:
            return items
        kwargs["ExclusiveStartKey"] = result["LastEvaluatedKey"]


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    main()
