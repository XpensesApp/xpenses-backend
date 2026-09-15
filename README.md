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

## Endpoints

**`POST /auth/sync`** — verifies a Cognito ID token (Bearer header) against the pool's JWKS, then looks up the user by email in the Users table and creates it if it doesn't exist yet.

**Transactions CRUD** (`api/transactions/{add,get,edit,delete}`) — unprotected for now (no auth), open CORS (`*`) on every route:
- `POST /transactions` — create
- `GET /transactions?email=` — list a user's transactions, newest first
- `PUT /transactions` — full-replace update (body needs the original `transactionId` + `date`)
- `DELETE /transactions?email=&transactionId=&date=` — delete

## Shared Lambda Layer

`layers/models/` (`transaction.py`, `http_utils.py`) is a real Lambda Layer (`ModelsLayer` in `template.yaml`), attached to each transactions function via `Layers:`. `sam build` nests it under `python/` automatically so it lands on `/opt/python` at runtime.

Locally, that folder isn't on `sys.path` by default, so running any Lambda's `if __name__ == "__main__"` block directly needs `PYTHONPATH` pointed at it:

```bash
# from inside e.g. api/transactions/add/
PYTHONPATH=../../../layers/models python app.py
```

PowerShell:
```powershell
$env:PYTHONPATH = "..\..\..\layers\models"; python app.py
```

Each `__main__` block runs against a fixed test user (`jane@test`) and your real `.env` (`USERS_TABLE`, `TRANSACTIONS_TABLE`, `COGNITO_*`), loaded via `python-dotenv`.
