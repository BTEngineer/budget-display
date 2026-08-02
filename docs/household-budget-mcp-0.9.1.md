# Household Budget MCP 0.9.1 Reference

This document describes the authenticated Household Budget MCP tool surface in
version 0.9.1. It covers the complete 13-tool contract implemented on the
`codex/budget-mcp-improvements` development branch.

Version 0.9.1 is not deployed by this document. Version 0.8.0 remains the
published baseline until the development pull request is reviewed and merged.

## Connection

- Transport: Streamable HTTP
- Endpoint: `http://<home-assistant-address>:8099/mcp`
- Authentication: `Authorization: Bearer <BUDGET_API_TOKEN>`
- Host protection: the request `Host` must appear in the add-on's
  `allowed_hosts` configuration.
- Parallel tool calls: disabled in the supplied Hermes configuration.
- Resources and prompts: disabled; the server exposes tools only.

Use the explicit 13-tool allowlist in `hermes-config.example.yaml`. Do not give
Hermes direct SQLite, shell, or filesystem access.

## Shared conventions

### Money

Send monetary values as positive decimal strings such as `"8.00"`. Values may
contain at most two decimal places. The ledger stores integer cents and returns
`currency: "USD"` with canonical decimal amounts.

### Request IDs and retries

Every write requires a stable, unique `request_id`. Use the source message or
event ID. Repeating the same request ID and exact payload returns the existing
result with `duplicate: true`. Reusing it for different data fails closed.

For large expenses and splits, confirmation is required only for the first
mutation. An exact retry may succeed after its token expires or without
resending the token. A conflicting retry never bypasses confirmation or writes
new data.

### Members and categories

Members and categories must match active add-on configuration. A top-level
category with children does not accept expenses directly; use an exact path
such as `Meals/Food`.

### Timestamps

`occurred_at` values use ISO 8601 and must include a timezone. The server stores
UTC and assigns the budget month using `household_timezone`.

Preparation tools always return a concrete UTC `occurred_at`. Pass it unchanged
to the corresponding commit tool, including when it was omitted from the
prepare request.

### Text limits

- `request_id`: 200 characters
- `business_name`: 200 characters
- expense or refund `description`: 1,000 characters
- search text: 500 characters
- split allocations: 2 through 20

## Confirmation threshold

An expense, corrected replacement, or split total over `$500.00` requires a
short-lived signed confirmation token. Exactly `$500.00` does not.

Tokens expire after ten minutes and bind the exact resolved payload. Changing a
request ID, target, member, category, amount, merchant, description, timestamp,
split total, or allocation invalidates the token. Maximum-size valid split
payloads are supported by the bounded token decoder.

## Tool summary

| Tool | Type | Purpose |
|---|---|---|
| `prepare_expense` | Read/prepare | Sign an exact expense payload. |
| `add_expense` | Write | Record one immutable expense. |
| `prepare_correction` | Read/prepare | Sign an exact corrected replacement. |
| `correct_expense` | Write | Reverse and replace an expense atomically. |
| `prepare_split_expense` | Read/prepare | Sign an exact split purchase. |
| `add_split_expense` | Write | Record all split allocations atomically. |
| `list_spending` | Read | Return monthly budget and spending totals. |
| `list_budget_categories` | Read | Return valid configured categories. |
| `suggest_expense_classification` | Read | Rank category suggestions without writing. |
| `get_budget_outlook` | Read | Calculate pace, projection, and category risk. |
| `undo_last_expense` | Write | Reverse a member's latest eligible expense. |
| `search_transactions` | Read | Search canonical transaction history. |
| `refund_expense` | Write | Record a linked or explicitly unlinked refund. |

## Expense tools

### `prepare_expense`

Required parameters:

- `request_id`
- `member`
- `category`
- `amount`

Optional parameters:

- `business_name` (default `""`)
- `description` (default `""`)
- `occurred_at`

Returns the normalized expense summary, concrete occurrence timestamp,
`confirmation_token`, and `expires_at`.

Example:

```json
{
  "request_id": "telegram-4821",
  "member": "Member 1",
  "category": "Everyday",
  "amount": "650.00",
  "business_name": "Furniture Store",
  "description": "Desk"
}
```

### `add_expense`

Accepts the same fields as `prepare_expense`, plus optional
`confirmation_token`. Expenses over `$500.00` require the exact token and
concrete timestamp returned by preparation.

The result includes the canonical transaction, amount in cents, and
`duplicate`. The expense is immutable after insertion.

Large-expense flow:

1. Call `prepare_expense`.
2. Present the returned normalized summary for explicit approval.
3. Call `add_expense` with the unchanged fields, returned `occurred_at`, and
   `confirmation_token`.

## Correction tools

Corrections never update or delete an existing row. They atomically create a
linked reversal and a replacement expense.

### `prepare_correction`

Required parameters:

- `request_id`: ID for the correction operation
- `transaction_id`: canonical expense transaction to correct

Optional replacement fields:

- `member`
- `category`
- `amount`
- `business_name`
- `description`
- `occurred_at`

Omitted fields resolve to the original expense values. The result contains the
fully resolved replacement and its signed token.

### `correct_expense`

Accepts the same target and replacement fields as `prepare_correction`, plus
optional `confirmation_token`. A resolved replacement over `$500.00` requires
the exact prepared token.

The response contains `original`, `reversal`, `replacement`, and `duplicate`.
It rejects:

- missing or non-expense targets;
- refunded or already reversed expenses;
- individual split allocations;
- a correction that changes no persisted field;
- conflicting request-ID reuse.

## Split-expense tools

Each allocation object requires:

- `member`
- `category`
- `amount`

Allocation amounts must add up exactly to `total_amount`.

### `prepare_split_expense`

Required parameters:

- `request_id`
- `total_amount`
- `allocations` (2 through 20 objects)

Optional parameters:

- `business_name`
- `description`
- `occurred_at`

Returns the normalized split, concrete timestamp, signed token, and expiry.

### `add_split_expense`

Accepts the same fields as `prepare_split_expense`, plus optional
`confirmation_token`. A total over `$500.00` requires the prepared token.

All allocations commit or none do. The response contains
`split_transaction_id`, `total_amount`, canonical `allocations`, and
`duplicate`.

Individual split allocations cannot be corrected or undone. A future
group-aware operation must preserve the complete source total atomically.

Example allocation payload:

```json
{
  "request_id": "telegram-4822",
  "total_amount": "600.00",
  "allocations": [
    {"member": "Member 1", "category": "Everyday", "amount": "300.00"},
    {"member": "Member 2", "category": "Occasional", "amount": "300.00"}
  ]
}
```

## Budget and category reads

### `list_spending`

Required parameter:

- `month`: `YYYY-MM`

Returns monthly budget, spent, and remaining cents plus totals by member and
top-level category. The first query for a month snapshots configured recurring
budgets so later default changes do not rewrite that month.

### `list_budget_categories`

Takes no parameters. Returns active category names, parents, and
`accepts_expenses`. Use it before a write when a category is unknown or
ambiguous.

### `get_budget_outlook`

Required parameter:

- `month`: `YYYY-MM`

Optional parameter:

- `as_of`: ISO 8601 timestamp with timezone

Returns deterministic elapsed-day pace, daily rate, projected month-end spend,
projected remaining funds, prior-month same-point comparison, pace change, and
categories at risk.

Historical calculations require both transaction occurrence and server record
time to be at or before `as_of`, so a later correction cannot alter an earlier
outlook asymmetrically.

## Classification

### `suggest_expense_classification`

Optional parameters:

- `business_name`
- `description`
- `limit` (1 through 10; default 5)

At least one text field is required. Suggestions use configured aliases, exact
prior merchant history, and low-confidence category words.

Aliases match lexical boundaries. Merchant history counts one source purchase
per category, even if a split contains repeated allocations for different
members. Results include category, confidence, reason, sample count, and
last-use evidence.

This tool never authorizes a write. The response always requires an explicit
category in a later write operation.

## Undo

### `undo_last_expense`

Required parameters:

- `request_id`
- `member`

Creates a linked negative reversal for the member's latest active, unrefunded
ordinary expense. It never deletes the original.

The tool fails closed if the selected entry belongs to a split. It also rejects
missing eligible expenses and conflicting request-ID reuse.

## Transaction search

### `search_transactions`

All parameters are optional:

- `start_at`: inclusive ISO 8601 lower bound
- `end_at`: exclusive ISO 8601 upper bound
- `member`
- `category`: exact leaf or top-level category including descendants
- `business_name`: case-insensitive literal substring
- `description_query`: case-insensitive literal substring
- `minimum_amount`
- `maximum_amount`
- `transaction_id`
- `request_id`: also resolves source split request IDs
- `operation_type`: `expense`, `refund`, `reversal`, or `all` (default)
- `status`: `active`, `reversed`, or `all` (default `active`)
- `sort_order`: `ascending` or `descending` (default `descending`)
- `limit`: 1 through 200 (default 50)
- `cursor`: opaque signed cursor returned by the previous page

Results contain canonical transactions, `count`, `has_more`, and
`next_cursor`. Continue with the same filters and `next_cursor` until
`has_more` is false for complete reconciliation. Cursors are bound to the exact
filter and sort state; malformed, tampered, or cross-query cursors fail closed.

Canonical records include stable transaction and request IDs, operation type,
member, category, amount, merchant, description, occurrence and record times,
status, correction/reversal/split links, and refund state.

## Refunds

### `refund_expense`

Required parameters:

- `request_id`
- `amount`

Optional parameters:

- `expense_id`: canonical `transaction_id` of the original expense
- `member`
- `category`
- `business_name`
- `description`
- `occurred_at`

For a linked refund, use `search_transactions` to identify exactly one original
expense and pass its `transaction_id` as `expense_id`. Supplied member,
category, or merchant values must match that expense. The refund cannot exceed
`remaining_refundable_amount`.

When no original can be verified, omit `expense_id` and explicitly provide
`member`, `category`, and the exact amount. The result identifies the refund as
unlinked. Refunds are immutable negative ledger entries and may occur in a
later month than the purchase.

## Errors and safe caller behavior

Treat tool errors as final until the payload is corrected or a person provides
missing information. In particular:

- Do not guess an unknown member or category.
- Do not select a parent category that requires a child.
- Do not weaken or synthesize a confirmation token.
- Do not retry a conflicting request ID with altered data.
- Do not replace a failed split correction or undo with several unrelated
  writes.
- Do not recompute budget outlook values conversationally when the server can
  calculate them.

## Version 0.9.1 validation boundary

The development suite contains 74 automated tests. It covers confirmation
binding and expiry, exact retry behavior, maximum-size split tokens, correction
and split integrity, historical outlook cutoffs, classification boundaries,
search pagination, refunds, authentication, and the complete 13-tool MCP
contract.

Automated tests and configuration readback do not prove a live Hermes client,
Home Assistant deployment, or physical display behavior. Perform a test-instance
deployment and end-to-end MCP smoke test before production promotion.
