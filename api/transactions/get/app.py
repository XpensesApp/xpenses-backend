# app.py
"""GET /transactions?from=YYYY-MM-DD&to=YYYY-MM-DD&limit=N&nextToken=...

Lists the authenticated user's transactions within a date range, newest
first, one page at a time. Protected by the Cognito Lambda Authorizer —
`email` comes from the verified token (requestContext.authorizer.email).

All query parameters are optional:
- `to` defaults to today (UTC); `from` defaults to one calendar month before `to`.
  Both bounds are inclusive. The resolved range is echoed back in the response.
- `limit` is the page size (default DEFAULT_LIMIT, max MAX_LIMIT).
- `nextToken` is the opaque cursor from a previous response; pass it with the
  same from/to to get the next page. `nextToken` is null on the last page.
"""
import base64
import binascii
import calendar
import traceback
from datetime import date, datetime, timedelta, timezone

from boto3.dynamodb.conditions import Key

from http_utils import get_authenticated_email, json_response
from transaction import get_transactions_table

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class BadRequest(Exception):
    pass


def lambda_handler(event, context):
    # Catch-all so an unexpected crash still returns CORS headers instead of
    # a raw Lambda error the browser reports as an opaque "Failed to fetch".
    try:
        return _handle(event)
    except BadRequest as exc:
        return json_response(400, {"message": str(exc)})
    except Exception:
        traceback.print_exc()
        return json_response(500, {"message": "Internal server error"})


def _handle(event):
    email = get_authenticated_email(event)
    params = event.get("queryStringParameters") or {}

    date_from, date_to = _parse_date_range(params)
    limit = _parse_limit(params.get("limit"))

    # sk is "<date>#<transactionId>", so every sk for day D sorts after the
    # bare string "D" and before the bare string "D+1" (no sk is ever just a
    # date). BETWEEN is inclusive, but the upper bound itself can't match an
    # item, so this covers exactly date_from..date_to.
    sk_low, sk_high = date_from.isoformat(), (date_to + timedelta(days=1)).isoformat()
    query_kwargs = {
        "KeyConditionExpression": Key("email").eq(email) & Key("sk").between(sk_low, sk_high),
        "ScanIndexForward": False,  # newest first
        "Limit": limit,
        # Clients refetch right after a write (e.g. to see a recalculated card
        # statement); an eventually consistent read could still return the old state.
        "ConsistentRead": True,
    }
    if params.get("nextToken"):
        # Only the sk is carried in the token; the partition key is always the
        # caller's own email, so a tampered token can't page into another user.
        start_sk = _decode_token(params["nextToken"])
        if not sk_low <= start_sk < sk_high:
            raise BadRequest("nextToken does not belong to this date range")
        query_kwargs["ExclusiveStartKey"] = {"email": email, "sk": start_sk}

    table = get_transactions_table()
    result = table.query(**query_kwargs)

    last_key = result.get("LastEvaluatedKey")
    # json_response serializes with default=str, which handles the Decimal amounts
    return json_response(
        200,
        {
            "transactions": result.get("Items", []),
            "dateRange": {"from": date_from.isoformat(), "to": date_to.isoformat()},
            "nextToken": _encode_token(last_key["sk"]) if last_key else None,
        },
    )


def _parse_date_range(params: dict) -> tuple[date, date]:
    try:
        date_to = date.fromisoformat(params["to"]) if params.get("to") else datetime.now(timezone.utc).date()
        date_from = date.fromisoformat(params["from"]) if params.get("from") else _one_month_before(date_to)
    except ValueError:
        raise BadRequest("from/to must be dates in YYYY-MM-DD format")

    if date_from > date_to:
        raise BadRequest("from must be on or before to")
    return date_from, date_to


def _one_month_before(d: date) -> date:
    """Same day of the previous month, clamped to that month's length (e.g. Mar 31 -> Feb 28)."""
    year, month = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _parse_limit(raw: str | None) -> int:
    if raw is None:
        return DEFAULT_LIMIT
    try:
        limit = int(raw)
    except ValueError:
        raise BadRequest("limit must be an integer")
    if not 1 <= limit <= MAX_LIMIT:
        raise BadRequest(f"limit must be between 1 and {MAX_LIMIT}")
    return limit


def _encode_token(sk: str) -> str:
    return base64.urlsafe_b64encode(sk.encode()).decode()


def _decode_token(token: str) -> str:
    try:
        # validate=True rejects non-alphabet characters instead of silently dropping them
        return base64.b64decode(token.encode(), altchars=b"-_", validate=True).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise BadRequest("Invalid nextToken")


if __name__ == "__main__":
    import json

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    TEST_EMAIL = "jane@test"

    # Walk every page of the default (last month) range, 2 items at a time
    params = {"limit": "2"}
    while True:
        event = {"queryStringParameters": params, "requestContext": {"authorizer": {"email": TEST_EMAIL}}}
        result = lambda_handler(event, None)
        print(result)
        next_token = json.loads(result["body"]).get("nextToken")
        if not next_token:
            break
        params = {**params, "nextToken": next_token}
