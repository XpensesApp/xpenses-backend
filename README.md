# Xpenses Backend

Serverless expense tracker backend: AWS Lambda + DynamoDB + API Gateway, Python, deployed with AWS SAM (`template.yaml`). Resource names and env vars are resolved per-stage (`dev`/`prod`) via `Mappings` + `!FindInMap` — DynamoDB tables and the Cognito pool are created/owned outside the stack and only referenced.

## Entities

### `User` — `api/auth/sync/models.py`
| field | type | notes |
|---|---|---|
| `email` | str | unique identifier (table PK) |
| `name` | str? | |
| `createdAt` | datetime | defaults to now |

### `Transaction` — `layers/models/transaction.py`
| field | type | notes |
|---|---|---|
| `email` | str | owner (table PK), matches `User.email` |
| `title` | str | non-empty |
| `amount` | Decimal | magnitude only; sign comes from `type` |
| `categories` | list[str] | free-form tags, no separate entity |
| `date` | date | |
| `type` | `TransactionType` enum | `EXPENSE` / `INCOME` |
| `affectsBalance` | bool | |
| `pending` | bool | |
| `transactionId` | str | auto (uuid4), part of the sort key |
| `sk` | str | table SK, auto-derived as `"<date>#<transactionId>"` |
| `accountId`, `installments`, `paymentDay`, `subscriptionId`, `billingPeriod` | optional | |

**Table key schema:** PK = `email`, SK = `sk`. A single `Query` on `email` returns all of a user's transactions, naturally sortable/filterable by date since `sk` is date-prefixed — no GSI needed for the main use case.

### `Subscription` — `layers/models/subscription.py`
| field | type | notes |
|---|---|---|
| `email` | str | owner (table PK), matches `User.email` |
| `title` | str | non-empty |
| `billingDay` | int (1-31) | day of month a pending `Transaction` is generated |
| `type` | `TransactionType` enum | reused from `transaction.py` — a salary is an income subscription |
| `affectsBalance` | bool | |
| `status` | `SubscriptionStatus` enum | `ACTIVE` / `DISABLED` |
| `subscriptionId` | str | auto (uuid4), table SK |
| `amount` | Decimal? | unset = variable/unknown (e.g. electricity) until billed |
| `categories` | list[str] | defaults to `[]` |
| `createdAt` | datetime | defaults to now |
| `endDate` | date? | optional expiration |

**Table key schema:** PK = `email`, SK = `subscriptionId` (plain, not composite — no in-partition ordering is needed for "list a user's subscriptions"). GSI `BillingDayStatusIndex` (PK=`billingDay`, SK=`status`, projects all attributes) serves the cross-user daily-billing lookup — `Query(billingDay=today, status=active)` — independent of the base table's own keys.

`generate_transaction_from_subscription(subscription, billing_date)` (in `subscription.py`) converts a subscription into its pending `Transaction`: carries over `amount` (or `Decimal("0")` if unset), always `pending=True`, and builds a **deterministic** `transactionId` (`f"{subscriptionId}-{date}"`) so reprocessing the same subscription/date overwrites rather than duplicates — this is the idempotency mechanism the daily billing job relies on.

## Endpoints

**`POST /auth/sync`** — verifies a Cognito ID token (Bearer header) against the pool's JWKS, then looks up the user by email in the Users table and creates it if it doesn't exist yet.

**Transactions CRUD** (`api/transactions/{add,get,edit,delete}`) and **Subscriptions CRUD** (`api/subscriptions/{add,get,edit,delete}`) — both unprotected for now (no auth), open CORS (`*`) on every route:
- `POST /transactions` / `POST /subscriptions` — create
- `GET /transactions?email=` / `GET /subscriptions?email=` — list a user's records, newest first
- `PUT /transactions` (needs original `transactionId` + `date`) / `PUT /subscriptions` (needs original `subscriptionId`) — full-replace update
- `DELETE /transactions?email=&transactionId=&date=` / `DELETE /subscriptions?email=&subscriptionId=` — delete

## Daily Billing Job

`api/billing/daily/app.py` (`DailyBillingFunction`) runs on an EventBridge schedule (`cron(0 6 * * ? *)` — 06:00 UTC, adjust in `template.yaml` to taste). Each run: `Query`s `BillingDayStatusIndex` for `billingDay == today`, `status == active` (paginated), skips anything already `endDate`-expired, and writes a pending `Transaction` for everything else via `generate_transaction_from_subscription`. Safe to re-invoke for the same day (idempotent) or a past day for backfill — pass `{"date": "YYYY-MM-DD"}` as the event payload to override "today". Returns/logs a `{date, created, skippedExpired, failed}` summary; a single malformed subscription is caught and counted in `failed` rather than aborting the whole run.

## Getting a Test Token

`api/auth/sync/get_test_token.py` — **local-only dev tool, never deployed**. The Cognito pool supports native (non-Google) sign-in alongside Google, so a dedicated test user (`test@xpenses.dev`) logs in with a real username+password via Cognito's `USER_AUTH` flow and gets back a real ID token — same issuer/audience/claims a Google-authenticated user would get, so it passes `verify_token()` and works against any protected endpoint. Requires your own AWS credentials to run (only whoever has access to this account can mint a token this way):

```bash
cd api/auth/sync
AWS_PROFILE=personal python get_test_token.py
```

Needs `COGNITO_APP_CLIENT_SECRET` and `TEST_USER_PASSWORD` in `.env` (the app client has a secret, so Cognito requires a `SECRET_HASH` on every auth call). Deliberately not exposed as a deployed endpoint — every current route is unprotected, so a token-minting endpoint on this same API would be reachable by anyone. When a dev/prod split exists, a real `POST` endpoint for this is planned, but gated to the dev stage only and still requiring real username+password (not free token issuance for arbitrary emails).

## Shared Lambda Layer

`layers/models/` (`transaction.py`, `subscription.py`, `http_utils.py`) is a real Lambda Layer (`ModelsLayer` in `template.yaml`), attached to each transactions/subscriptions function via `Layers:`. `sam build` nests it under `python/` automatically so it lands on `/opt/python` at runtime.

Locally, that folder isn't on `sys.path` by default, so running any Lambda's `if __name__ == "__main__"` block directly needs `PYTHONPATH` pointed at it:

```bash
# from inside e.g. api/transactions/add/
PYTHONPATH=../../../layers/models python app.py
```

PowerShell:
```powershell
$env:PYTHONPATH = "..\..\..\layers\models"; python app.py
```

Each `__main__` block runs against a fixed test user (`jane@test`) and your real `.env` (`USERS_TABLE`, `TRANSACTIONS_TABLE`, `SUBSCRIPTIONS_TABLE`, `COGNITO_*`), loaded via `python-dotenv`.
