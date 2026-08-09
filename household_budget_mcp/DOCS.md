# Household Budget MCP Home Assistant app

This app hosts the household SQLite ledger and its narrow MCP interface on Home
Assistant OS.

For the versioned 13-command tool contract, see the
[Household Budget MCP 0.9.1 reference](../docs/household-budget-mcp-0.9.1.md).

## Release status

Version 0.9.2 is the current app package. It retains the version 0.9.1
thirteen-tool protocol while adding the stable Supervisor-internal hostname to
the default Host allowlist for the Home Assistant dashboard. Version 0.9.1
introduced exact large-expense confirmations, audit-preserving corrections,
atomic split expenses, read-only classification suggestions, server-calculated
budget outlooks, and the P1 hardening described below.

## Configuration

Set `api_token` to a randomly generated value containing at least 32
characters. Do not reuse a Home Assistant access token or another account
password.

`allowed_hosts` contains the exact `Host` headers permitted by the MCP server.
The generic defaults contain the external local hostname and the app's stable
internal Home Assistant hostname:

- `homeassistant.local:8099`
- `9930efe6-household-budget-mcp:8099`

Add the LAN address used by the MCP client before connecting it. Entries use
`host:port` format without `http://` or a path.

Replace the generic `members` and `categories` values before first start.
Top-level category rows require a positive `monthly_budget`. Subcategory rows
set `parent` to the exact top-level name and leave `monthly_budget` blank.
Configuration changes deactivate removed names for new expenses without
deleting their historical ledger entries. `household_timezone` controls which
local calendar month receives each expense.

Optional `classification_aliases` entries contain `term` and `category`.
Categories must be exact configured paths such as `Meals/Food`. Aliases affect
only `suggest_expense_classification`; they never choose a category for a write.
Terms match at lexical boundaries rather than arbitrary substrings.

## Hermes endpoint

The endpoint is `http://<home-assistant-address>:8099/mcp`. Configure the MCP
client with the same token as an `Authorization: Bearer ...` header. Keep the
thirteen-tool allowlist in `hermes-config.example.yaml`.

The same listener exposes `/api/v1` for the Household Budget custom integration.
It uses the same bearer token and Host allowlist. The API is intentionally
limited to configuration, summaries, recent expenses, validated expense/undo
operations, and the receipt draft lifecycle. It exposes no SQL, shell,
filesystem browsing, credential, or arbitrary Home Assistant operation.
Configure the custom integration with
`http://9930efe6-household-budget-mcp:8099` so Home Assistant uses Supervisor's
internal app DNS rather than routing back through its LAN hostname.

Receipt files are validated as JPEG, PNG, or PDF, capped at 12 MB, named by
their SHA-256 digest, and stored privately under `/data/receipts`. AI output is
stored as an untrusted draft. The Home Assistant integration must revalidate
and explicitly confirm the draft before it becomes an immutable ledger entry.
After confirmation records the transaction, the source file is deleted
immediately. Its digest, metadata, AI audit record, and ledger link remain in
SQLite for duplicate detection and accounting history.

For MCP expenses over $500, call `prepare_expense` first and show its exact
summary to the person. The returned token expires after ten minutes and is
cryptographically bound to every expense field. `add_expense` rejects a missing,
expired, tampered, or mismatched token. The Home Assistant JSON API retains its
separate human-review flow.
Split totals over $500 use `prepare_split_expense` and receive the same exact,
short-lived protection across the total and every allocation.

The confirmation decoder accepts the maximum token size produced by every
valid runtime field and allocation limit. Preparation also refuses payloads
that would exceed the bounded decoder ceiling rather than returning a token
that cannot be committed.

Confirmation is required only for the first large write. Exact retries are
resolved through request-ID idempotency before token validation, allowing safe
recovery from lost responses after a token expires. A changed amount,
allocation, timestamp, or other bound field remains a conflicting request and
is rejected without mutation.

Preparation always resolves a concrete `occurred_at` value. Even when the
caller omitted it from the prepare request, the returned timestamp must be
passed unchanged to the committing tool. This prevents a reviewed transaction
from crossing a day or month boundary before it is recorded.

Descriptions have one shared 1,000-character limit across preparation, token
validation, direct writes, corrections, splits, refunds, and classification.
This prevents a prepared payload from being rejected by the committing tool
solely because the two boundaries applied different limits.

## MCP tool surface

The MCP server exposes thirteen tools:

- `prepare_expense`
- `add_expense`
- `prepare_correction`
- `correct_expense`
- `prepare_split_expense`
- `add_split_expense`
- `list_spending`
- `list_budget_categories`
- `suggest_expense_classification`
- `get_budget_outlook`
- `undo_last_expense`
- `search_transactions`
- `refund_expense`

It does not expose arbitrary SQL, filesystem, shell, deletion, or budget
configuration tools.

## Version 0.9 operations

### Corrections

`correct_expense` targets a stable expense `transaction_id` and accepts only
the fields that need to change. In one immediate SQLite transaction, it creates
a reversal linked to the original and an active replacement linked back to the
original. It rejects refunded, already reversed, missing, and non-expense
targets. Exact retries are idempotent; conflicting reuse of the request ID is
rejected. No historical row is overwritten. A correction that resolves to the
same persisted fields is rejected without creating a reversal or replacement.

If the fully resolved replacement is over $500, `prepare_correction` is
mandatory. Its signed token binds the target transaction, request ID, resolved
member, category, amount, merchant, description, and occurrence timestamp.
Changing an omitted or explicit field after preparation invalidates the token.

Individual allocations belonging to a split expense are rejected by both
`prepare_correction` and `correct_expense`. Version 0.9.1 deliberately fails
closed here because replacing only one allocation would detach it from the
split's stated total and source request. A future group-aware split correction
requires a separately reviewed design.

`undo_last_expense` also fails closed when its selected expense is a split
allocation, whether selection occurs by member or internal entry ID. A future
undo operation must reverse the complete split atomically and preserve its
declared total.

### Split expenses

`add_split_expense` accepts a stated total plus 2 through 20 member/category
allocations. Every amount is validated as cents and the allocations must equal
the stated total exactly. All allocation entries share an opaque split ID and
commit atomically, so a failed member, category, amount, or balance check writes
nothing. Summaries count each allocation once and canonical search results carry
the source request ID and split ID.

### Classification suggestions

`suggest_expense_classification` is read-only. It ranks configured aliases,
exact prior business-name history, and low-confidence category-name matches.
Every result includes its reason, confidence, sample count, and last-use time
where applicable. The response explicitly requires the caller to provide an
exact category to a later write operation.

Configured aliases match complete lexical terms, preventing aliases embedded
inside larger words from producing false positives. Merchant history counts a
split group only once per category, so allocating one purchase across multiple
members does not inflate its classification evidence.

### Budget outlook

`get_budget_outlook` calculates spending pace, daily rate, month-end projection,
the prior month's spending through the comparable day, pace change, projected
remaining funds, and categories projected to exceed budget. It uses integer
cents and the configured household timezone. Hermes should present these
server-calculated results rather than recomputing them from conversational
memory.

Every `as_of` ledger query requires both the transaction occurrence time and
server recording time to be at or before the cutoff. Consequently, a
correction recorded later cannot retroactively add its replacement to an
earlier outlook while its balancing reversal remains outside that outlook.

## Transaction search and refunds

This section documents the implemented refund behavior and server-side
transaction recall contract for the Hermes Home budget journal.

Version 0.8.0 implements:

- `search_transactions`: authoritative, authenticated, bounded, paginated
  transaction recall across complete ledger history.
- `refund_expense`: an audit-preserving linked or unlinked refund.
- stable opaque transaction IDs and automatic migration of legacy entries;
- a distinct business-name field and canonical expense, refund, and reversal
  records;
- signed, filter-bound keyset cursors with deterministic ordering; and
- backward-compatible mutation responses that add a canonical `transaction`
  object without removing the established top-level fields.

The original plan item named `list_recent_expenses` was superseded by
`search_transactions`, which supports recent lookup plus complete-history
reconciliation and includes category and business-name filtering.

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
the requested direction. Pagination uses a stable, signed cursor position so
identical timestamps do not silently skip or duplicate records. Responses do
not expose database row IDs, SQL cursors, authorization data, stack traces, or
filesystem paths.

For a refund lookup, clients should normally request `operation_type: expense`
and include each expense's remaining refundable amount. For complete journal
reconciliation, a Hermes client should use explicit calendar boundaries,
`status: all`, ascending order, a bounded page size, and follow every cursor
before treating the period as complete.

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

### Hermes client contract

The server-side operations needed for conversational resolution are
implemented. During the separate Hermes rollout, Hermes should use any member,
category, or business-name clues to call
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

The version 0.9.1 server and `hermes-config.example.yaml` define the exact
thirteen-tool development allowlist. A live Hermes deployment must not adopt that
allowlist until this branch is reviewed and deployed. Resources, prompts, and
parallel tool calls remain disabled.

### Persistence and query implementation

Search, refunds, totals, and undo use the same authoritative SQLite ledger; no
Hermes-specific transaction table was added. Business name is stored as a
distinct field, so merchant filtering does not depend on parsing the
description. Startup migration adds the new canonical columns, classifies
legacy reversals, and assigns stable opaque transaction IDs to historical rows
without deleting ledger history.

The schema includes a unique transaction-ID index, an occurrence-time plus
transaction-ID search-order index, and a refund-relationship index. Search uses
keyset pagination rather than a mutable offset. Member, category,
operation/status, amount, ID, and literal business or description filters are
applied to the authoritative ledger query.

The existing one-reversal-per-expense relationship remains valid for undo,
while the refund relationship permits multiple partial refunds. Aggregate
queries count each credit once and continue producing the established monthly,
member, and parent/child category totals.

### Validation status

The version 0.9.2 source suite passes 74 automated tests. P1 regression
coverage includes large-correction preparation and mutation rejection, the
exact $500 correction boundary, concrete single/split occurrence timestamps,
month-boundary preservation, historical outlooks before and after a correction,
and fail-closed split-allocation correction. Existing coverage continues to
exercise atomic balanced splits, idempotency, classification, refunds, search,
authentication, and the complete thirteen-tool MCP contract.

P2 regression coverage verifies the shared 1,000-character prepare/commit
description boundary, lexical alias matching, one classification-history sample
per split source purchase and category, and no-op correction rejection without
audit-log noise.

Final hardening coverage includes maximum-size 20-allocation confirmation
round trips, fail-closed split undo by member and entry ID, exact large expense
and split retries with missing or expired tokens, and conflicting tokenless
retry rejection.

The 0.8.0 source delivery passed 53 automated tests plus Python, JavaScript,
JSON, lockfile, requirements-export, archive, and Git whitespace checks. The
automated suite covers:

- canonical expense, reversal, linked-refund, and unlinked-refund records;
- exact idempotent retries and conflicting request-ID reuse;
- full and partial refunds, multiple partial refunds, over-refund rejection,
  and refund-month accounting;
- search filter families, including top-level category descendants, literal
  business-name matching, time and amount bounds, identifiers, operation type,
  and status;
- stable filter-bound pagination and malformed or tampered cursor rejection;
- legacy schema migration and preservation of existing historical records;
- missing, invalid, and undersized bearer tokens plus Host-header rejection;
- unchanged totals, category behavior, large-expense confirmation, MCP
  resources/prompts, and existing client response compatibility.

Live Home Assistant migration/readback, backup restoration, Hermes end-to-end
reconciliation, token rotation, and live financial mutations are operational
validation steps rather than claims established by the source test suite. Live
financial mutations require separate approval and a documented reversal or
cleanup procedure.

### Delivery status and operational boundary

Version 0.9.1 was reviewed, merged to `main` through pull request #5, tagged,
and released. Version 0.9.2 is the packaging/configuration follow-up proposed
through pull request #6. Source delivery does not itself deploy or restart the
live Home Assistant app, alter Hermes configuration, rotate credentials, or
perform live financial mutations. Those remain separately controlled
operational actions.

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

Version 0.8.0 continues publishing the dashboard-oriented numeric sensors
introduced in 0.7 for total budget, percentage used, last update, and stable
per-top-level-category budget, spent, remaining, and percentage values. The
original numbered category strings remain unchanged for E1001 compatibility.

## Security

Every MCP request requires the bearer token. The server also rejects unlisted
HTTP Host values. The app requests no Home Assistant, Supervisor, host network,
device, configuration, or filesystem privileges beyond its private `/data`
volume and exposed TCP port.
