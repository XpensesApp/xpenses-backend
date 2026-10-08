# Handoff — Latest Backend Updates

Newest first. Subscriptions have their own reference in `subscription_handoff.md`. Every request below is in `postman-collection.json`.

---

# 2026-10-08 — Preferred account (built, **not deployed yet**)

Users can pick which account is **preselected when creating a transaction**. This is separate from the built-in "General" account, which doesn't change.

## TL;DR: frontend action items

1. **Preselect the preferred account** in the new-transaction form: the account with `isPreferred: true` in `GET /accounts` (also returned as `preferredAccountId` at the top level). Exactly one account always has it; until the user picks one, it's General.
2. **Let the user choose it,** e.g. a "Usar como cuenta predeterminada" action on each account: `PUT /accounts/preferred` with `{"accountId": "<id>"}`. To go back to General, send `"default"`.
3. **After setting it,** update `isPreferred` locally (the response tells you the new `preferredAccountId`) or refetch `GET /accounts`.
4. **Always send the selected `accountId`** when creating a transaction. If you omit it, the backend still uses General, not the preferred account.
5. **Deleting the preferred account fails with `409`.** Ask the user to choose another preferred account first, or offer "make another account preferred and delete".

## `isDefault` vs `isPreferred`

| | `isDefault` (General) | `isPreferred` |
|---|---|---|
| what | the built-in fallback account, `accountId: "default"` | the user's choice for new transactions |
| set by | the backend (created on login) | the user, via `PUT /accounts/preferred` |
| which account | always General | any account, including a credit card; General until chosen |
| used for | transactions sent without `accountId`, subscription bills, the source of card statements | preselecting the account in the frontend form |
| deletable | never | not while preferred; choose another one first |

## API changes

**Account shape:** a new field on every account returned by `GET`, `POST` and `PUT /accounts`:

```ts
type Account = {
  // ...all existing fields
  isPreferred: boolean; // true on exactly one account (General until the user picks another)
};
```

**`GET /accounts`** adds a top-level `preferredAccountId`:

```json
{
  "accounts": [
    { "accountId": "default", "name": "General", "isDefault": true, "isPreferred": false, "...": "..." },
    { "accountId": "6", "name": "Cuenta RUT", "isDefault": false, "isPreferred": true, "...": "..." }
  ],
  "preferredAccountId": "6"
}
```

**`PUT /accounts/preferred`** (new):

```json
{ "accountId": "6" }
```

| response | when |
|---|---|
| `200 {"preferredAccountId": "6"}` | set (sending `"default"` resets it to General) |
| `400 {"message": "accountId is required"}` | missing/empty `accountId` |
| `404 {"message": "Account '<id>' does not exist"}` | no such account |
| `409 {"message": "Accounts changed meanwhile; reload them and retry"}` | concurrent change |

**`DELETE /accounts?accountId=`** has one new response:

| response | when |
|---|---|
| `409 {"message": "This is your preferred account for new transactions; choose another preferred account first"}` | the account is the preferred one |

The check and the delete happen in one atomic write, so a "set preferred" racing a "delete" can never leave the preference pointing at a deleted account.

## Postman

In **Accounts**, after "Create Account (to delete)":

1. **Set Preferred Account (to delete)**: makes the throwaway account preferred.
2. **Delete Preferred Account (rejected)**: expects the `409`.
3. **Set Preferred Account**: moves the preference to `{{accountId}}`, which releases the throwaway.
4. **List Accounts (preferred)**: checks that exactly `{{accountId}}` has `isPreferred` and that `preferredAccountId` matches.

The existing "Delete Account" then deletes the throwaway successfully.

---

# 2026-10-08 — Credit card system

Card purchases can be split into installments ("cuotas"), and each credit card gets **one pending payment per month** (its "statement"), like a subscription. The statement is kept up to date automatically.

| | status |
|---|---|
| Monthly statements created by the daily job; prod data migrated (CMR statement for 2026-10-04 = 143.887, pending; "Retiro" + "Abono" merged into one transfer) | **deployed** (2026-10-08) |
| Statement **recalculated on every transaction write**, amounts as a closed formula (`since`/`revision` fields), consistent reads on `GET /transactions` and `GET /accounts`, `400` when deleting a pending statement, **changing a transaction's date** with `PUT` | built, **not deployed yet** |

Everything below describes the system once both parts are deployed.

## TL;DR: frontend action items

1. **Installment purchases:** create the purchase as usual (an `expense` with `accountId` = the card) and set `installments` to the number of cuotas. Leave it unset (or `1`) for a single payment. Send the **full** purchase amount; the backend splits it.
2. **Show the statement:** it's a regular transaction in `GET /transactions` with a non-null `statement` field: a `transfer` from the default account into the card, dated on the card's due date. While unpaid it's `pending: true`.
3. **Pay it:** `PUT` the statement with `amount` = what was actually paid (less or more than due is fine), `pending: false`, and `accountId` = the account it was paid from.
4. **After every transaction write, refresh the right data:** see [After a write: what to refetch](#after-a-write-what-to-refetch).
5. **Never try to delete a pending statement** (`400`). To skip a month, leave it pending; it rolls into the next one.
6. **On `409`:** reload the transaction and retry; something changed it in between.

## How it works

**Purchases.** A purchase is a full `expense` on the card at purchase time, so the card's `balance` (from `GET /accounts`) shows the whole debt: `-120000` for a 120.000 TV in 12 cuotas.

**Due dates.** The card's `paymentDay`, clamped to the month's length (31 → Feb 28/29). A purchase made **before** a due date is first billed on that due date; one made **on or after** it goes to the next month. Cuota *k* is billed on the *k*-th due date from there.

**Cuotas.** The amount ÷ installments, rounded down to whole pesos (or to the amount's decimals), with the remainder on the last one, so they always add up exactly. 100.000 in 3 → 33.333, 33.333, 33.334.

**What a statement charges.** For the statement due on date D:

> **amount due = every cuota due from the card's first statement up to D − every payment made from the card's first statement up to the day before D**

Payments are: paid statements, any other settled `transfer` into the card, and refunds (`income` on the card). So partial payments, overpayments and late payments all carry over by themselves. Cuotas due before the card's first statement are assumed paid outside the app.

**The breakdown** splits that amount in two:
- `lines`: the cuotas billed since the last **paid** statement
- `previousBalance`: whatever was still owed before them (negative = credit from an overpayment)

**Example** (card with `paymentDay` 5):

| | cuotas billed | payments | amount |
|---|---|---|---|
| TV 120.000 in 12 cuotas bought 2026-09-10, Farmacia 5.000 on 2026-10-01 | | | |
| **Statement 2026-10-05** | TV 1/12 10.000 + Farmacia 5.000 = 15.000 | | **15.000** |
| You pay 6.000 | | 6.000 | |
| **Statement 2026-11-05** | `lines`: TV 2/12 10.000 · `previousBalance`: 9.000 | | **19.000** |
| Not paid → **statement 2026-12-05** replaces it | `lines`: TV 2/12, TV 3/12 = 20.000 · `previousBalance`: 9.000 | | **29.000** |

**Lifecycle.**
- **At most one pending statement per card.** When the next due date arrives and the previous statement is still unpaid, it's deleted and the new one (which already includes its amount) takes its place, in a single atomic write.
- **Paid statements never change.** If you edit a purchase from a month you already paid, the difference shows up in the next statement's `previousBalance`.
- **A payment dated after a statement's due date counts toward the next month**, not the current statement. Pay the current one by settling it.

**When statements are created and recalculated.** Right after every create/edit/delete of a transaction touching the card (as `accountId` or `targetAccountId`, in its old or new version), before the API responds:
- **Created** as soon as a purchase makes something due, e.g. a purchase dated before the card's latest due date.
- **Recalculated** when purchases or payments change.
- **Deleted** if nothing is due anymore.

A daily job (03:15 Chile time) does the same for every card as a safety net, and creates each month's statement on its due date.

## The statement transaction

```json
{
  "title": "Pago CMR 2026-11-05",
  "amount": "19000",
  "categories": [],
  "date": "2026-11-05",
  "type": "transfer",
  "pending": true,
  "affectsBalance": true,
  "accountId": "default",
  "targetAccountId": "5",
  "transactionId": "statement-5-2026-11-05",
  "billingPeriod": "2026-11",
  "statement": {
    "accountId": "5",
    "installmentsDue": "10000",
    "previousBalance": "9000",
    "amountDue": "19000",
    "lines": [
      { "transactionId": "…", "title": "TV", "date": "2026-09-10", "installment": 2, "installments": 12, "amount": "10000" }
    ],
    "since": "2026-10-05",
    "revision": 3
  }
}
```

| field | meaning |
|---|---|
| `amount` | what to pay now: `amountDue`, but never below 0. After settling: what was actually paid |
| `statement.accountId` | the card |
| `statement.lines` | cuotas billed since the last paid statement (`installment` of `installments`) |
| `statement.installmentsDue` | sum of `lines` |
| `statement.previousBalance` | still owed from before; negative = credit |
| `statement.amountDue` | `installmentsDue + previousBalance`; can be negative, then `amount` is `"0"` |
| `statement.since` | the card's first statement date |
| `statement.revision` | increases on every recalculation; handy to detect a change |

- `statement` is **server-managed**: ignored on `POST`, and on `PUT` a statement's `type`, `targetAccountId` and `statement` always come from the stored item.
- **While pending:** you can set `title`, `categories` and `accountId` (the account you'll pay from), and they survive recalculations. `amount` can't be overridden; it's always the computed one. Set the real amount when settling.
- `installment`/`installments`/`revision` are numbers; amounts are decimal strings like everywhere else.
- Every other transaction has `"statement": null`.

## After a write: what to refetch

Every create/edit/delete updates balances and card statements **before** the API responds, and the read endpoints are strongly consistent, so a refetch right after the response always sees the new state.

| after | update locally | refetch |
|---|---|---|
| `POST /transactions` | insert the response (it's the saved transaction) | `GET /accounts`; plus the card statement if it touched a card |
| `PUT /transactions` | replace with the response | `GET /accounts`; plus the card statement if the old **or** new version touched a card |
| `DELETE /transactions` | remove the item | `GET /accounts`; plus the card statement if it touched a card |
| settling a statement (`PUT`) | replace with the response | `GET /accounts` |

- **Why `GET /accounts` every time:** any transaction write can move up to two balances, and it's a single small call returning all accounts.
- **"Touched a card"** means `accountId` or `targetAccountId` is an account with `type: "credit"`. For an edit, check both the old and the new version, e.g. moving a purchase off a card changes that card's statement.
- **Refetching the statement:** it may have been created, changed or deleted, so refetch rather than patch it locally. It's the transaction dated on the card's latest due date:

  ```ts
  // Same rule as the backend (which uses the UTC date): this month's paymentDay
  // if already reached, otherwise last month's, clamped to the month's length.
  export function latestDueDate(paymentDay: number, now = new Date()): string {
    const dueIn = (year: number, month: number) => {
      const lastDay = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
      return new Date(Date.UTC(year, month, Math.min(paymentDay, lastDay)));
    };
    const today = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
    let due = dueIn(today.getUTCFullYear(), today.getUTCMonth());
    if (due > today) due = dueIn(today.getUTCFullYear(), today.getUTCMonth() - 1);
    return due.toISOString().slice(0, 10);
  }

  // GET /transactions?from=<due>&to=<due> → the item with statement?.accountId === card.accountId
  // (none = nothing due on that card right now)
  ```

  If the transactions page you're showing already covers that date, refetching that page is enough.
- **A purchase dated after the due date** (the usual case for today's purchases) doesn't change the current statement. It's billed next month, so the refetch just returns the same statement.
- **If a recalculation fails on the server**, your write still succeeds (the transaction is saved) and the statement is fixed by the nightly job. You don't need to handle this case.

**Suggested card screen:**
- **Debt:** `balance` from `GET /accounts`.
- **Pending statement:** its `amount`, with `lines` listed as "TV · cuota 2/12 · 10.000" and `previousBalance` shown separately when it isn't 0.
- **Pay button:** amount prefilled with `amount`, plus a source-account picker. Submits a `PUT` with `pending: false`.

## Errors

| response | when | what to do |
|---|---|---|
| `400` "A pending card statement can't be deleted…" | `DELETE` on a pending statement | don't offer delete on pending statements |
| `400` "Account '<id>' does not exist" | the transaction references an unknown account | refresh accounts |
| `409` "…changed or deleted by another request; reload it and retry" | concurrent change (e.g. a recalculation just updated the statement you're settling) | reload the transaction, then retry |
| `400` "A card statement's date is the card's due date and can't be changed" | `PUT` on a statement with a different `date` | don't offer date editing on statements |

## Also fixed

- **Changing a transaction's date with `PUT /transactions` works.** It used to fail with `404 "Transaction not found"` for any transaction, because the date is part of the stored key. Send the new `date` with the same `transactionId`, as you already do; no frontend change needed. The backend finds the transaction by its id and moves it, all in one atomic write. The response carries the new `date` (and `sk`), so replace the item in your list by `transactionId`. If the move puts a card purchase before or after the card's due date, the pending statement is recalculated as usual. Statements themselves can't change date (`400`).
- `POST`/`PUT /transactions` ignore a client-sent `sk`; the key always comes from `date` + `transactionId`.
- `GET /transactions` and `GET /accounts` use strongly consistent reads, so a refetch right after a write never returns the old state.

## Postman

Run **Auth → Accounts → Transactions** in order; the card flow is in Transactions:

1. **Create Installment Purchase**: a 120.000 TV in 12 cuotas dated today. Its first cuota is due next month, so it doesn't touch the current statement.
2. **Create Backdated Card Purchase**: 30.000 in 3 cuotas dated the day before the card's latest due date, so the statement is created **immediately** with 1/3 = 10.000.
3. **Get Card Statement**: fetches it by its due date, exactly like the frontend recipe above.
4. **Edit Backdated Card Purchase (recalculates)**: 30.000 → 60.000; then **Get Card Statement (recalculated)** shows 20.000 and a higher `revision`.
5. **Delete Pending Statement (rejected)**: expects the `400`.
6. **Pay Card Statement**: settles it from `{{accountId}}`.

Steps 2–5 need the live recalculation deployed; before that, the statement only appears when the daily job runs.

## When the recalculation is deployed

The existing CMR statement (2026-10-04, 143.887, pending) is rewritten once with the same amount and the same 16 lines; it just gains `since` and `revision`. Nothing visible changes. If that bill was already paid outside the app, settle it with the real amount and source account; that also records the payment on the CMR balance.

---

# 2026-10-05 — Accounts, transfers, paging

All of this section is **deployed to prod** (`xpenses-backend-prod`).

---

## TL;DR: frontend action items

1. **Call `POST /auth/sync` after every login** (not just the first). It now also creates the user's default account.
2. **Replace `mockAccounts` with `GET /accounts`.** Accounts are real backend entities now, with live balances.
3. **Only send `accountId` / `targetAccountId` values that come from `GET /accounts`.** Unknown ids are rejected with `400`. Omit `accountId` to use the default account.
4. **`GET /transactions` no longer returns everything.** It returns the **last month, 50 per page**. Use `from`/`to` for other ranges, and `nextToken` for more pages.
5. **Show balances from `GET /accounts`**, never by summing transactions (you no longer get all of them).
6. **Handle the new `409`s** on transaction/account writes (reload + retry).
7. **Transfers are one transaction now:** `type: "transfer"` + `targetAccountId`, instead of an expense + an income.

---

## 1. Accounts (new)

Accounts moved from frontend mocks to the backend. Full CRUD under `/accounts`, protected by the same Cognito authorizer as transactions.

### Shape

```ts
type AccountType = "debit" | "credit" | "cash";

type Account = {
  email: string;
  accountId: string;        // stable reference used by transactions; generated by the backend
  name: string;
  type: AccountType;
  isSavings: boolean;       // default false
  paymentDay: number | null; // 1-31, only allowed when type is "credit"
  openingBalance: string;   // decimal string, default "0"
  // server-managed (ignored if you send them):
  balance: string;          // decimal string, can be negative; = openingBalance + settled transactions
  transactionCount: number; // transactions referencing this account
  isDefault: boolean;
  createdAt: string;        // ISO-8601 UTC
  updatedAt: string;
};
```

Compared to the frontend mock type: `id` → `accountId`, plus `openingBalance`, `balance`, `transactionCount`, `isDefault`, `createdAt`, `updatedAt`. `isSavings`/`paymentDay` are always present (`false`/`null` when unset).

Decimal strings (`balance`, `openingBalance`, `amount`) should be parsed as decimals, not floats. DynamoDB drops trailing zeros (`"650"`, not `"650.00"`).

### Endpoints

| | |
|---|---|
| `POST /accounts` | Create. Body: `name`, `type`, optional `isSavings`, `paymentDay`, `openingBalance`. `201` with the Account (`balance` starts at `openingBalance`). `400` on invalid fields. |
| `GET /accounts` | `{"accounts": [...]}`: all accounts in one response (no paging), default account first, then oldest first. |
| `PUT /accounts` | Full replace of the editable fields. Send `accountId` + **all** editable fields. Changing `openingBalance` shifts `balance` by the same difference; use that to match a real bank balance. `404` if missing, `409` on a concurrent change. |
| `DELETE /accounts?accountId=` | `400` for the default account. `409` while `transactionCount > 0`: move or delete its transactions first (pending ones count too). `404` if missing. |

### Default account

- Every user has exactly one account with `accountId: "default"` and `isDefault: true`. `POST /auth/sync` creates it as **"General" / `cash`**.
- It can be renamed/edited, but **never deleted**.
- Any transaction created **without** an `accountId` lands there, including the pending bills the daily subscription job generates.
- `POST /auth/sync` ensures it on **every** call, idempotently: it never resets a renamed default account or its balance.

### How balances move

Every transaction create/update/delete updates its accounts in the **same atomic write**, so after any successful response `GET /accounts` already reflects it. Only transactions with `affectsBalance: true` **and** `pending: false` move a balance:

| transaction `type` | effect |
|---|---|
| `expense` | `-amount` on `accountId` |
| `income` | `+amount` on `accountId` |
| `transfer` | `-amount` on `accountId`, `+amount` on `targetAccountId` |

Settling a pending transaction (`PUT` with `pending: false`) applies its effect at that moment. A credit card account (e.g. CMR) carrying debt shows a **negative** balance.

---

## 2. Transactions — changes

### Transfers (new `type`)

```json
{
  "title": "Move to savings",
  "amount": "200000",
  "categories": [],
  "date": "2026-10-05",
  "type": "transfer",
  "affectsBalance": true,
  "pending": false,
  "accountId": "6",
  "targetAccountId": "1"
}
```

- `type` is now `"expense"` | `"income"` | `"transfer"`.
- A transfer needs `targetAccountId` (must differ from `accountId`). `targetAccountId` on any other type is a `400`.
- Exclude transfers from expense/income totals: they net to zero overall.
- Subscriptions can't be transfers (`400`).

### `accountId` is always set now

- If you omit `accountId`, the transaction goes to `"default"`, and responses always include a non-null `accountId`.
- `accountId` and `targetAccountId` must belong to the user's existing accounts, otherwise `400 {"message": "Account '<id>' does not exist"}` and nothing is written.

### `GET /transactions` — date range + paging (breaking)

```
GET /transactions?from=2026-09-01&to=2026-09-30&limit=50&nextToken=...
```

| param | default |
|---|---|
| `to` | today (UTC), inclusive |
| `from` | one calendar month before `to`, inclusive (Mar 31 → Feb 28) |
| `limit` | 50 (max 200) |
| `nextToken` | cursor from the previous page |

Response:

```json
{
  "transactions": [ ... ],
  "dateRange": { "from": "2026-09-05", "to": "2026-10-05" },
  "nextToken": "MjAyNi0wOS0xNSM..."
}
```

- `nextToken` is `null` on the last page. When sending it back, send the **same** `from`/`to`; if you relied on the defaults, pass `dateRange.from`/`dateRange.to` explicitly so the range doesn't shift mid-paging.
- The last page can come back empty with `nextToken: null`.
- Future-dated transactions (e.g. upcoming installments) only appear if `to` is later than today.
- `400` on bad dates, `from` after `to`, a bad `limit`, or a `nextToken` from a different range.

### New error responses

| endpoint | new response |
|---|---|
| `POST /transactions` | `400` unknown account; `409` if you supplied a `transactionId` that already exists |
| `PUT /transactions` | `400` unknown account; `409` "changed or deleted by another request; reload it and retry" |
| `DELETE /transactions` | `409` same as above |

---

## 3. Subscriptions / daily billing

- Billed transactions now get `accountId: "default"` (still `pending: true`). When the user pays one, `PUT` it with the real amount, `pending: false` and the account it was paid from.
- The billing job **never overwrites** an existing transaction anymore. Before, rerunning it for a day could reset a bill the user had already settled back to pending.

---

## 4. Data migration (already done in prod)

- Accounts `"1"`–`"7"` were created for `jeanlopezcortes@gmail.com` with the **same ids, names and types as the frontend mocks**, so all existing transactions stay valid with no rewrite. CMR is `credit` with `paymentDay: 5`; Banco Estado ahorro vivienda has `isSavings: true`.
- Balances and transaction counts were rebuilt from the full history, starting from `openingBalance: "0"`:

| accountId | name | balance | transactions |
|---|---|---|---|
| `1` | Banco Estado ahorro vivienda | 1143000 | 1 |
| `2` | Banco de Chile cta corriente | 0 | 0 |
| `3` | Copec Pay debito | 2000000 | 2 |
| `4` | Mach debito | 1335572 | 2 |
| `5` | CMR | -146067 | 17 |
| `6` | Cuenta RUT | 277904 | 12 |
| `7` | Efectivo | 0 | 0 |
| `default` | General | 0 | 0 |

- **To do:** set each account's real `openingBalance` with `PUT /accounts` so `balance` matches the bank. New accounts created from now on get backend-generated ids (32-char hex), not `"1"`–`"7"`.

---

## 5. Backend-internal (no API impact)

- **New Accounts table** `xpenses-accounts-table-prod` (PK `email`, SK `accountId`), created outside the stack like the other tables.
- **Repo layout:** `api/` now only holds endpoint Lambdas. The authorizer moved to `authorizer/` and the daily billing job to `billing/`. That move goes live with the next `sam build` + `sam deploy`, with no behavior change.
- **Backfill/repair CLI:** `python layers/models/account.py <email> | --all` rebuilds balances and counts from transaction history.

---

## Postman

`postman-collection.json` covers all of the above. Create a Postman environment with `baseUrl` (the stack's `ApiUrl` output) and `authToken` (a Cognito ID token from `api/auth/sync/get_test_token.py`, valid for 60 minutes). Then run the folders in order:

1. **Auth** → Sync User (creates the default account)
2. **Accounts** → creates `{{accountId}}`, lists, updates, and demonstrates the delete rules
3. **Transactions** → create (on `{{accountId}}`), transfer (`{{accountId}}` → `default`), list + next page, update, delete
4. **Subscriptions**

A full run of Transactions leaves `{{accountId}}` at `openingBalance − 200` and the default account at `+200`: the expense is created, updated and deleted (net 0), and only the transfer remains.
