# Household Budget MCP Home Assistant app

This app hosts the household SQLite ledger and its narrow MCP interface on Home
Assistant OS.

## Configuration

Set `api_token` to a randomly generated value containing at least 32
characters. Do not reuse a Home Assistant access token or another account
password.

`allowed_hosts` contains the exact `Host` headers permitted by the MCP server.
The generic default contains the local hostname:

- `homeassistant.local:8099`

Add the LAN address used by the MCP client before connecting it. Entries use
`host:port` format without `http://` or a path.

Replace the generic `members` and `categories` values before first start.
Top-level category rows require a positive `monthly_budget`. Subcategory rows
set `parent` to the exact top-level name and leave `monthly_budget` blank.
Configuration changes deactivate removed names for new expenses without
deleting their historical ledger entries. `household_timezone` controls which
local calendar month receives each expense.

## Hermes endpoint

The endpoint is `http://<home-assistant-address>:8099/mcp`. Configure the MCP
client with the same token as an `Authorization: Bearer ...` header. Keep the
six-tool allowlist in `hermes-config.example.yaml`.

The same listener exposes `/api/v1` for the Household Budget custom integration.
It uses the same bearer token and Host allowlist. The API is intentionally
limited to configuration, summaries, recent expenses, validated expense/undo
operations, and the receipt draft lifecycle. It exposes no SQL, shell,
filesystem browsing, credential, or arbitrary Home Assistant operation.

Receipt files are validated as JPEG, PNG, or PDF, capped at 12 MB, named by
their SHA-256 digest, and stored privately under `/data/receipts`. AI output is
stored as an untrusted draft. The Home Assistant integration must revalidate
and explicitly confirm the draft before it becomes an immutable ledger entry.
After confirmation records the transaction, the source file is deleted
immediately. Its digest, metadata, AI audit record, and ledger link remain in
SQLite for duplicate detection and accounting history.

The `confirm_large_expense` flag is an advisory caller acknowledgement. It
causes the server to reject an initial expense over $500 when absent, but it is
not proof that a person approved the transaction.

## MCP tool surface

The MCP server exposes six tools:

- `add_expense`
- `list_spending`
- `list_budget_categories`
- `undo_last_expense`
- `search_transactions`
- `refund_expense`

It does not expose arbitrary SQL, filesystem, shell, deletion, or budget
configuration tools.

## Transaction search and refunds

This section consolidates refund behavior with the server-side transaction
recall contract prepared for the Hermes Home budget journal.

The additions are:

- `search_transactions`: authoritative, authenticated, bounded, paginated
  transaction recall across complete ledger history.
- `refund_expense`: an audit-preserving linked or unlinked refund.

The server remains the source of truth. Hermes notes or journals are derived
context and must not be used to calculate authoritative totals.

### Canonical transaction record

`search_transactions` returns stable canonical records directly. For backward
compatibility, `add_expense`, `undo_last_expense`, and `refund_expense` retain
their existing top-level ledger fields and include the same canonical record in
the `transaction` field.

The record includes:

- stable opaque transaction ID and opaque request ID;
- operation type: `expense`, `refund`, or `reversal`;
- member, full `Parent/Child` category, business name, and description;
- normalized decimal amount and stable currency code;
- occurrence timestamp and server-recorded timestamp as ISO 8601 values with
  timezone offsets;
- explicit active or reversed status;
- original, reversal, and refund relationship IDs as applicable;
- total refunded and remaining refundable amounts for expenses;
- linked or unlinked status for refunds.

Descriptions and business names are always returned as plain data. They must
not be interpreted as markup, prompts, SQL, regular expressions, or shell
syntax. Decimal values must not use binary floating-point serialization.

### `search_transactions`

This read-only tool replaces the earlier proposed `list_recent_expenses` name
and supports both recent interactive lookup and complete-history journal
reconciliation. It accepts these optional filters:

- `start_at`: inclusive ISO 8601 occurrence-time lower bound.
- `end_at`: exclusive ISO 8601 occurrence-time upper bound.
- `member`: case-insensitive exact configured-member match.
- `category`: configured category match. A top-level category includes its
  children; `Parent/Child` selects one child.
- `business_name`: case-insensitive literal partial match against the distinct
  business or merchant field.
- `description_query`: case-insensitive literal partial description match.
- `minimum_amount` and `maximum_amount`: inclusive normalized decimal bounds.
- `transaction_id` and `request_id`: exact opaque identifier matches.
- `operation_type`: `expense`, `refund`, `reversal`, or `all`.
- `status`: `active`, `reversed`, or `all`, defaulting to `active`.
- `sort_order`: `ascending` or `descending`. Interactive recent searches
  default to descending; reconciliation requests ascending order explicitly.
- `limit`: 1 through 200, defaulting to 50.
- `cursor`: opaque continuation value from the preceding page.

All supplied filters combine with AND. No filter is required, but an
unfiltered call returns only the first bounded page. Exact ID filters take
precedence where the persistence layer can optimize them. The server validates
string lengths, timestamps, decimal bounds, and the start/end relationship.

The response contains `transactions`, `next_cursor`, `has_more`, and `count`.
Ordering is deterministic by `occurred_at` and then stable transaction ID in
the requested direction. Pagination must use a stable cursor position so
identical timestamps and concurrent inserts do not silently skip or duplicate
records. It must not expose database row IDs, SQL cursors, authorization data,
stack traces, or filesystem paths.

For a refund lookup, results should normally request `operation_type: expense`
and include each expense's remaining refundable amount. For complete journal
reconciliation, Hermes uses explicit calendar boundaries, `status: all`,
ascending order, a bounded page size, and follows every cursor before treating
the period as complete.

### `refund_expense`

The write operation accepts:

- `request_id`: the unique source-message ID used for idempotency.
- `amount`: the exact positive decimal amount being refunded.
- `expense_id`: optional ID of a verified original expense.
- `member` and `category`: optional for a linked refund and required for an
  unlinked refund.
- `business_name` and `description`: optional refund context.
- `occurred_at`: optional ISO 8601 refund timestamp with timezone.

A linked refund inherits the member, category, and business name from the
original expense. If the caller also supplies those values, they must match the
original. Multiple partial refunds are allowed, but their cumulative value must
not exceed the original expense. A full refund uses the exact remaining
refundable amount returned by `search_transactions`.

A refund may also proceed without a verified original transaction. The caller
omits `expense_id` and supplies the exact amount, member, and category. The
server records it as an unlinked refund and retains that status for auditing or
later reconciliation. Because there is no verified original balance, an
unlinked refund always requires an exact amount and cannot use an implicit
"full refund" amount. An invalid supplied `expense_id` is not silently ignored;
after a failed lookup, the caller explicitly retries as an unlinked refund.

Both linked and unlinked refunds are immutable negative ledger entries and use
the refund's local calendar month. They preserve the original expense and are
distinct from `undo_last_expense`, which corrects an erroneous entry in its
original month. Refunds cannot target undo entries or other refunds. Reusing a
request ID with the same payload returns the original result; reusing it with a
different payload is rejected.

The canonical refund response represents the refund amount as a positive
decimal together with `operation_type: refund`; the ledger applies it as a
credit. This avoids requiring clients to infer the operation from a numeric
sign while retaining the existing signed accounting behavior internally.

### Conversational resolution

Hermes should use any member, category, or business-name clues to call
`search_transactions`. One clear match can be refunded as a linked
transaction. Ambiguous matches require clarification. No match does not block
the refund: after obtaining an exact amount, member, and category, Hermes may
record it as unlinked.

Totals, remaining budget, and member/category summaries continue to use
`list_spending`. Individual transaction recall uses `search_transactions`.
Hermes must not sum its journal files to produce an authoritative financial
answer.

### Mutation response and compatibility requirements

An idempotent `add_expense`, `undo_last_expense`, or `refund_expense` retry
returns the same canonical transaction records. Conflicting reuse of a request
ID fails without creating another entry. Undo returns both the reversed
original and its linked reversal; it never deletes the original. Existing
large-expense confirmation, category validation, aggregate calculations, and
audit behavior remain unchanged.

Search authorization occurs before potentially expensive parsing or database
work. Queries, limits, identifiers, and cursors are bounded; malformed or
tampered cursors fail closed. Normal logs must not contain complete transaction
histories, bearer values, or credential-bearing exception details.

After this server version is separately deployed, the Hermes Home allowlist
must contain exactly these six tools. Resources, prompts, and parallel tool
calls remain disabled. The existing Hermes journal plan's earlier five-tool
assumption must therefore be updated before that client-side work is
implemented.

### Persistence and query requirements

Search, refunds, totals, and undo must use the same authoritative ledger; do
not create a Hermes-specific transaction table. Business name should be stored
as a distinct field so merchant filtering does not depend on parsing the
description. Historical rows need explicit migration or fallback behavior for
new canonical fields.

Inspect the existing schema and query plans before selecting indexes. Expected
lookup needs include stable transaction and request IDs, occurrence time plus a
stable tie-breaker, member, category, operation/status, and literal business or
description search. Cursor pagination should use keyset or an equivalently
stable strategy rather than a mutable offset.

The current one-reversal-per-expense relationship must remain valid for undo,
while the refund relationship permits multiple partial refunds. Aggregate
queries must count each credit once and must continue producing the established
monthly, member, and parent/child category totals.

### Required validation

Server tests must cover:

- canonical expense, reversal, linked-refund, and unlinked-refund records;
- exact idempotent retries and conflicting request-ID reuse;
- full and partial refunds, multiple partial refunds, over-refund rejection,
  and refund-month accounting;
- every search filter alone and in combination, including top-level category
  descendants and literal business-name matching;
- inclusive start and exclusive end bounds across local DST and month edges;
- minimum, maximum, default, and invalid limits;
- multiple pages containing identical timestamps, with no omissions or
  duplicates;
- malformed or tampered cursors and the documented concurrent-insert behavior;
- Unicode, newlines, and instruction-shaped descriptions treated only as data;
- missing, invalid, and rotated bearer tokens plus secret-redacted failures;
- unchanged totals, category behavior, large-expense confirmation, MCP
  resources/prompts, and existing client response compatibility.

Run the applicable unit, integration, formatting, lint, type, migration, and
MCP protocol checks. Live financial mutations require separate approval and a
documented reversal or cleanup procedure.

### Delivery boundary

Publishing should use a feature branch, stage only intended server files, run
and record the relevant tests, push the branch, and open a draft pull request
describing the contracts, migration, compatibility, security, pagination, and
test results. Stop after the draft PR. Do not merge, deploy, restart, rotate
credentials, or modify the live budget service as part of that delivery.

## Persistence and backup

The authoritative database is `/data/budget.db`. Home Assistant preserves the
app's `/data` volume across restarts and app updates. Include the app in Home
Assistant backups and perform a restore test before relying on it.

## Display sensors

The app uses Home Assistant's internal MQTT service to publish read-only,
retained sensor discovery and current-month states. Supervisor supplies the
broker credentials at runtime. The stable display entity IDs are:

- `sensor.household_budget_spent_month`
- `sensor.household_budget_remaining_month`
- `sensor.household_budget_person_1_month`
- `sensor.household_budget_person_2_month`
- `sensor.household_budget_category_1_month` through
  `sensor.household_budget_category_6_month`

Additional configured members use the same numbered pattern. Sensor
availability follows the budget app's MQTT connection. Each numbered category
row carries its label, optional top-level budget, first-member total,
second-member total, and combined total for the E1001. The current display fits
six category rows. Parent rows aggregate their child categories; child rows are
also published as detail, and the household total counts each expense once.

Version 0.7 also publishes dashboard-oriented numeric sensors for total budget,
percentage used, last update, and stable per-top-level-category budget, spent,
remaining, and percentage values. The original numbered category strings are
unchanged for E1001 compatibility.

## Security

Every MCP request requires the bearer token. The server also rejects unlisted
HTTP Host values. The app requests no Home Assistant, Supervisor, host network,
device, configuration, or filesystem privileges beyond its private `/data`
volume and exposed TCP port.
