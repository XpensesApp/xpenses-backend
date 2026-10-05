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
| `type` | `TransactionType` enum | `EXPENSE` / `INCOME` / `TRANSFER` |
| `affectsBalance` | bool | |
| `pending` | bool | |
| `transactionId` | str | auto (uuid4), part of the sort key |
| `sk` | str | table SK, auto-derived as `"<date>#<transactionId>"` |
| `accountId`, `installments`, `paymentDay`, `subscriptionId`, `billingPeriod` | optional | |
| `targetAccountId` | str? | destination account; required iff `type` is `TRANSFER` |

**Transfers:** a single `TRANSFER` transaction moves `amount` from `accountId` to `targetAccountId` (both required, must differ) instead of recording an expense + an income. It's an outflow for `accountId`, an inflow for `targetAccountId`, and nets to zero on the user's overall balance. `targetAccountId` is rejected on non-transfer types.

**Table key schema:** PK = `email`, SK = `sk`. A single `Query` on `email` returns all of a user's transactions, naturally sortable/filterable by date since `sk` is date-prefixed — no GSI needed for the main use case.

### `Subscription` — `layers/models/subscription.py`
| field | type | notes |
|---|---|---|
| `email` | str | owner (table PK), matches `User.email` |
| `title` | str | non-empty |
| `billingDay` | int (1-31) | day of month a pending `Transaction` is generated |
| `type` | `TransactionType` enum | reused from `transaction.py` — a salary is an income subscription; `TRANSFER` is rejected (subscriptions have no accounts) |
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

**Transactions CRUD** (`api/transactions/{add,get,edit,delete}`) — **protected** by `CognitoAuthorizer` (see below). `email` comes from the verified token, not the request — dropped from the contract entirely:
- `POST /transactions` — create
- `GET /transactions` — list the caller's own records, newest first
- `PUT /transactions` (needs original `transactionId` + `date`) — full-replace update
- `DELETE /transactions?transactionId=&date=` — delete

**Subscriptions CRUD** (`api/subscriptions/{add,get,edit,delete}`) — **protected** by `CognitoAuthorizer` too, same rule as Transactions:
- `POST /subscriptions` — create
- `GET /subscriptions` — list the caller's own records, newest first
- `PUT /subscriptions` (needs original `subscriptionId`) — full-replace update
- `DELETE /subscriptions?subscriptionId=` — delete

Open CORS (`*`) on every route.

## Cognito Lambda Authorizer

`api/auth/authorizer/app.py` (`CognitoAuthorizerFunction`) is a `TOKEN`-type REST API authorizer, wired onto every Transactions and Subscriptions route (`XpensesApi`'s `Auth.Authorizers.CognitoAuthorizer` — no `DefaultAuthorizer`, so `/auth/sync` stays on its own inline check unless a route opts in via `Events.*.Properties.Auth.Authorizer`).

It reuses the exact same verification `auth/sync` does inline (`cognito_auth.py` in the shared layer — extracted from `auth/sync/app.py` specifically so this wouldn't be a third copy). On success it returns an Allow policy plus `context: {email}`, which lands on the downstream Lambda's event at `requestContext.authorizer.email` (read via `http_utils.get_authenticated_email(event)`). On failure it raises the exact string `"Unauthorized"` — the API Gateway TOKEN-authorizer contract that produces a 401 (any other exception message/type produces a 500 instead). That 401's body is customized via `GatewayResponseDefault4xx` in `template.yaml`, **not** a dedicated `GatewayResponseUnauthorized` (`ResponseType: UNAUTHORIZED`) — confirmed empirically that `UNAUTHORIZED` genuinely never applies to this failure mode, while `DEFAULT_4XX` does (an earlier test on `DEFAULT_4XX` gave a false negative from testing only seconds after patching, before the change had propagated). Safe to put an auth-specific message on that catch-all since our own Lambda-level 400/404 responses never go through `GatewayResponse` at all. The real per-request reason (expired, bad signature, wrong issuer, ...) still only goes to CloudWatch (`auth-authorizer-{stage}`), never the client — genuinely dynamic per-request messages aren't achievable here regardless, short of migrating the REST API to an HTTP API (v2). `Identity.ReauthorizeEvery: 300` caches a decision for 5 minutes per token; the returned policy's `Resource` is wildcarded to the whole API stage so that cache applies across routes, not just whichever one triggered the check.

**Ownership, not just authentication:** a valid token alone isn't what makes these routes safe — every handler calls `http_utils.get_authenticated_email(event)` and uses *that* (never a client-supplied value) as the DynamoDB partition key for every read/write. `edit`/`delete` build their lookup key from the token's email plus the client-supplied resource id, so trying to edit/delete someone else's `transactionId`/`subscriptionId` just misses (`404`, not their data) — there's no separate "does this belong to you?" check, ownership is enforced by construction: the key can only ever resolve to your own partition.

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

`layers/models/` (`transaction.py`, `subscription.py`, `http_utils.py`, `cognito_auth.py`) is a real Lambda Layer (`ModelsLayer` in `template.yaml`), attached to each transactions/subscriptions function via `Layers:`. `sam build` nests it under `python/` automatically so it lands on `/opt/python` at runtime.

Locally, that folder isn't on `sys.path` by default, so running any Lambda's `if __name__ == "__main__"` block directly needs `PYTHONPATH` pointed at it:

```bash
# from inside e.g. api/transactions/add/
PYTHONPATH=../../../layers/models python app.py
```

PowerShell:
```powershell
$env:PYTHONPATH = "..\..\..\layers\models"; python app.py
```

Each `__main__` block runs against a fixed test user (`jane@test`) and your real `.env` (`USERS_TABLE`, `TRANSACTIONS_TABLE`, `SUBSCRIPTIONS_TABLE`, `COGNITO_*`), loaded via `python-dotenv`. For the (now-protected) Transactions Lambdas, the `__main__` block simulates the authorizer's output directly (`event["requestContext"]["authorizer"]["email"]`) rather than needing a real token — use `api/auth/sync/get_test_token.py` when you need to exercise the real deployed/protected route end-to-end instead.
