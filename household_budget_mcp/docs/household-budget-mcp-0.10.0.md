# Household Budget MCP 0.10.0 Reference

This document describes the complete authenticated 14-tool Household Budget
MCP contract for version 0.10.0. This version adds manual transaction
reconciliation without changing monetary ledger history. Source delivery does
not deploy the app or enable the new Hermes tool.

## Connection and authority boundary

- Transport: Streamable HTTP
- Endpoint: `http://<home-assistant-address>:8099/mcp`
- Authentication: `Authorization: Bearer <BUDGET_API_TOKEN>`
- Host protection: the request `Host` must be in `allowed_hosts`
- Parallel tool calls: disabled in the supplied Hermes configuration
- Resources and prompts: disabled

Use the explicit 14-tool allowlist in `hermes-config.example.yaml`. Do not give
Hermes direct SQLite, shell, filesystem, deletion, or budget-configuration
authority.

## Shared conventions

Money is supplied as a positive decimal string with at most two decimal places
and is stored as integer cents. Canonical responses use `currency: "USD"` and
decimal strings. Members and categories must match active app configuration;
categories with children require an exact `Parent/Child` path.

Every write requires a stable `request_id` of at most 200 characters. An exact
retry returns the original result. Conflicting reuse fails closed. Timestamps
must be ISO 8601 with a timezone; the server stores UTC and assigns the budget
month using `household_timezone`.

Text limits are 200 characters for `business_name`, 1,000 for `description`,
and 500 for search text. Split expenses contain 2 through 20 allocations.

Expenses, corrected replacements, or split totals over `$500.00` require the
matching preparation tool and its short-lived signed token. Exactly `$500.00`
does not. Exact retries are resolved before token validation, so a committed
large write can be retried after its token expires.

## Tool summary

| Tool | Type | Purpose |
|---|---|---|
| `prepare_expense` | Read/prepare | Sign an exact expense payload. |
| `add_expense` | Write | Record one immutable expense. |
| `prepare_correction` | Read/prepare | Sign an exact replacement expense. |
| `correct_expense` | Write | Reverse and replace an expense atomically. |
| `prepare_split_expense` | Read/prepare | Sign an exact split purchase. |
| `add_split_expense` | Write | Record all split allocations atomically. |
| `list_spending` | Read | Return monthly budget and spending totals. |
| `list_budget_categories` | Read | Return configured category paths. |
| `suggest_expense_classification` | Read | Rank category evidence without writing. |
| `get_budget_outlook` | Read | Calculate pace, projection, and category risk. |
| `undo_last_expense` | Write | Reverse an eligible expense. |
| `search_transactions` | Read | Search canonical transaction history. |
| `set_transaction_reconciliation` | Write | Audit a posted-card match or unmatch. |
| `refund_expense` | Write | Record a linked or explicitly unlinked refund. |

## Expense and correction tools

### `prepare_expense` and `add_expense`

Required parameters are `request_id`, `member`, `category`, and `amount`.
Optional parameters are `business_name`, `description`, and `occurred_at`.
`add_expense` also accepts `confirmation_token`.

Preparation returns the exact normalized expense and a token. If preparation
generated the timestamp, pass that returned `occurred_at` unchanged to
`add_expense`. A successful write returns the established ledger fields plus a
canonical `transaction` object and `duplicate`.

### `prepare_correction` and `correct_expense`

Required parameters are `request_id` and the original `transaction_id`.
Optional replacement fields are `member`, `category`, `amount`,
`business_name`, `description`, and `occurred_at`; omitted fields retain the
original value. `correct_expense` also accepts `confirmation_token`.

Correction atomically creates a linked reversal and a new replacement. It
never edits the original. Reversed, refunded, or split-allocation targets fail
closed. A correction that changes nothing is rejected. The replacement has a
new transaction ID and starts unreconciled.

## Split tools

### `prepare_split_expense` and `add_split_expense`

Required parameters are `request_id`, `total_amount`, and `allocations`. Each
allocation contains `member`, `category`, and `amount`. Optional parameters are
`business_name`, `description`, and `occurred_at`; the committing tool also
accepts `confirmation_token`.

Allocation amounts must equal `total_amount` exactly. All allocations commit
or none do. Individual allocations cannot be corrected, undone, or partially
reconciled. A reconciliation operation targets every allocation in the split.

## Read-only budget tools

### `list_spending`

Requires `month` in `YYYY-MM` form. It returns snapshotted budget, spent and
remaining cents, member totals, category totals, and category rows. Refunds are
credits; reversals balance the original expense month.

### `list_budget_categories`

Takes no parameters. It returns configured category paths and whether each path
accepts expenses.

### `suggest_expense_classification`

Optional parameters are `business_name`, `description`, and `limit` (default
5, maximum 10). At least one text field is required. Results are evidence only
and never authorize or perform a write.

### `get_budget_outlook`

Requires `month`; optional `as_of` sets an ISO 8601 cutoff. It returns
server-calculated pace, projection, comparison, and category risk values.

## Transaction history

### Canonical transaction record

Canonical records include stable transaction and request IDs, operation type,
member, full category, amount, currency, merchant, description, occurrence and
record times, active/reversed state, correction/reversal/split/refund links,
refunded and remaining refundable amounts, and:

```json
{
  "reconciled": false,
  "reconciled_at": null
}
```

`reconciled_at` is the server timestamp of the latest reconcile operation and
is null whenever the current state is unreconciled.

### `search_transactions`

All parameters are optional:

- `start_at`: inclusive occurrence-time lower bound
- `end_at`: exclusive occurrence-time upper bound
- `member`
- `category`: exact leaf or top-level category including descendants
- `business_name`: case-insensitive literal substring
- `description_query`: case-insensitive literal substring
- `minimum_amount` and `maximum_amount`: inclusive absolute amount bounds
- `transaction_id`
- `request_id`: also resolves a source split request ID
- `operation_type`: `expense`, `refund`, `reversal`, or `all` (default)
- `status`: `active`, `reversed`, or `all` (default `active`)
- `reconciliation_status`: `all` (default), `reconciled`, or `unreconciled`
- `sort_order`: `ascending` or `descending` (default)
- `limit`: 1 through 200 (default 50)
- `cursor`: opaque signed continuation from the preceding page

Results contain `transactions`, `count`, `has_more`, and `next_cursor`.
Ordering is deterministic by `occurred_at` then transaction ID. Cursors bind
the exact filters and sort order. The default reconciliation filter preserves
the legacy cursor digest, so a cursor issued immediately before the upgrade can
continue afterward. A non-default reconciliation filter is included in the
digest and cannot reuse an unfiltered cursor.

### `set_transaction_reconciliation`

Required parameters:

- `request_id`: unique reconciliation request ID
- `transaction_id`: stable expense or refund ID
- `reconciled`: boolean

The tool records a reconciliation operation and its ledger targets in one
immediate SQLite transaction. It never updates `ledger_entries`. A split target
expands to every allocation. Refunds are reconciled independently from their
original expense. Internal reversal entries are rejected.

The response contains `changed`, `duplicate`, `reconciled`, `reconciled_at`,
`transaction_ids`, and `split_transaction_id`. A new request that repeats the
current state is retained for audit and returns `changed: false`. An exact retry
returns the original outcome with `duplicate: true`; a different target or
state using the same request ID fails closed.

Existing transactions are not backfilled and therefore begin unreconciled.
The original transaction retains its reconciliation history after correction
or undo, while the corrected replacement begins unreconciled.

## Refund and undo tools

### `refund_expense`

Required parameters are `request_id` and `amount`. Optional parameters are
`expense_id`, `member`, `category`, `business_name`, `description`, and
`occurred_at`.

A linked refund targets a verified expense and inherits its member, category,
and merchant. Its amount cannot exceed `remaining_refundable_amount`. If no
original can be verified, omit `expense_id` and explicitly provide `member`,
`category`, and the exact amount. Refunds are distinct ledger records and are
reconciled separately.

### `undo_last_expense`

Requires `request_id` and `member`. It records a linked reversal without
deleting the selected expense. Split allocations and refunded expenses are not
eligible. Reversals are internal accounting operations and cannot be marked
reconciled.

## Persistence, compatibility, and rollout boundary

Reconciliation uses two additive tables: immutable operations and their ledger
targets. Startup validates the table columns, uniqueness, action constraint,
foreign keys, lookup index, and schema marker in a separate immediate
transaction. No historical ledger row is altered or automatically marked.
Missing audit tables fail closed if a migration marker or surviving audit data
shows prior initialization. Only empty partial schemas without a marker can be
completed. Transaction searches use a single read snapshot for ID selection,
canonical records and cursor construction, including during concurrent writes.

Version 0.9.3 can open and write an upgraded database because it ignores the
additive tables. Rollback should therefore restore the old application while
leaving healthy reconciliation tables in place. Restore an older database only
for actual integrity failure and only after accounting for newer writes.

The source suite contains 84 tests. It covers the prior financial, receipt,
API, MQTT, authentication, search, correction, split, and refund behavior plus
reconciliation migration, idempotency, filtering, pagination, concurrency,
rollback, and foreign keys. Live backup restoration, migration of a restored
production copy, deployment, Hermes allowlist activation, and the first real
reconciliation write remain separate operational steps.

## Deployment readiness review - 2026-10-23

This initial review is historical; the repair and verification follow-up below
supersedes its code findings and failing-test status.

Decision: **not ready for production deployment**. Review only; implementation,
live configuration, Hermes permissions, and financial records were not changed.
The earlier live Home Assistant update-entity readback reported installed and
latest available version 0.9.3. The local branch is
`codex/transaction_reconciliation`; HEAD is 0.9.3 and the 0.10.0 implementation
remains uncommitted. No deployed-image source hash comparison was performed.

### Findings requiring fixes

1. **P2 - Missing reconciliation targets are silently replaced with an empty
   table.** `ledger.py:949` creates the target table before checking the existing
   schema marker. Reproduction on a disposable database: record an expense,
   reconcile it, drop `transaction_reconciliation_targets`, then initialize.
   Startup succeeds and the same transaction becomes unreconciled; the operation
   records remain but their target links are lost. Refuse to recreate missing
   reconciliation tables when the marker or existing audit data indicates a
   previously initialized database. Cover populated partial-schema cases while
   retaining safe completion of genuinely unused schemas. This is a recovery
   failure case, not evidence of corruption in the live database.
2. **P2 - Filtered searches do not read one reconciliation snapshot.**
   `ledger.py:2091-2106` selects matching IDs and then reads canonical records
   using separate statements without an explicit read transaction. Reproduction:
   search for reconciled entries, commit an unreconcile using another connection
   between ID selection and canonical lookup, and observe a returned row with
   `reconciled: false`. A multirow response may mix states from different times.
   Hold a consistent read snapshot across selection, record hydration and cursor
   construction, and add a test with an interleaved writer.

### Verification performed

- Full local suite: **82 tests run, 80 pass, 2 fail**. All eight new
  reconciliation tests pass. Windows sandbox loopback restrictions initially
  blocked asyncio startup; the complete suite ran outside that sandbox using
  disposable databases and the existing component virtual environment, with
  `PYTHONPATH` pointing to `src`.
- Both failures also reproduce using the committed 0.9.3 implementation:
  `test_budget_outlook_calculates_projection_comparison_and_risk` and
  `test_new_write_and_read_tools_round_trip_through_mcp`. Their historical
  August `as_of` cutoffs precede the current recording timestamps, so the
  expected nonzero totals are zero. Freeze the fixtures' recording clock;
  do not change production cutoff semantics to make these tests pass.
- A disposable database created by the actual committed 0.9.3 ledger upgrades
  without changing ledger rows or monthly totals. A cursor produced by 0.9.3
  continues correctly after upgrade. Exact reconciliation retry after a later
  opposite operation returns the original result without changing current state.
- The actual 0.9.3 ledger can initialize and write the upgraded synthetic
  database, which 0.10.0 can then reopen. SQLite quick-check and foreign-key
  checks pass. This establishes synthetic compatibility, not production restore
  or deployed-container rollback acceptance.
- `uv lock --check --offline` passes. Exported hashed production dependencies
  match `requirements.lock`; `git diff --check` passes. No fresh vulnerability
  audit was performed.

### Remaining deployment gates

- Fix the two reconciliation findings and the two pre-existing test fixtures;
  rerun the complete suite.
- Build and smoke-test the Linux amd64 Home Assistant app image. Docker CLI
  exists locally, but its Linux engine is unavailable, so image construction
  and container startup were not tested.
- Obtain a fresh app backup and SQLite-consistent database backup through an
  approved operational procedure. Restore to a disposable environment; compare
  production-copy ledger digests, IDs, counts and monthly totals through
  migration and old-version rollback rehearsal. No private production data was
  accessed for this review.
- Deliver an exact reviewed release artifact through the app repository.
  Neither uncommitted local version strings nor historical ZIP files establish
  an installable 0.10.0 release.
- Deploy the app first, verify integrity and read-only behavior, then enable
  the new Hermes tool within the existing profile boundary. A real selected
  reconciliation write requires separate authorization; do not invent financial
  smoke-test records.

## Reconciliation fixes and suite verification - 2026-10-23

Both P2 findings from the initial review are fixed locally:

- Before creating either audit table, initialization reads the schema marker
  and checks for surviving audit rows. Missing tables with evidence of prior
  initialization raise `BudgetValidationError` instead of resetting apparent
  reconciliation state. Empty partial schemas without a marker still migrate.
- `search_transactions` opens an explicit read transaction before selection.
  Filtering, canonical records and cursor construction now share one SQLite
  snapshot while other connections can commit writes in WAL mode. Each later
  search sees newly committed state; snapshots do not span pagination requests.
- The two historical outlook fixtures freeze the recording clock at
  `2026-08-10T15:00:00+00:00`, before their explicit cutoff. Production monetary
  and cutoff semantics are unchanged.

Verification: **84 tests run, all pass** using the existing component virtual
environment and working-tree sources. The complete suite ran outside the
Windows sandbox because asyncio requires local loopback initialization; tests
used disposable databases, not Home Assistant or private production records.
New regression coverage includes either/both missing tables, marker presence,
surviving audit data without a marker, and interleaved reconcile/unreconcile
writes between selection and multirow hydration, including cursor creation.
The existing unused-partial-schema test now removes its marker to represent an
unfinished initial migration. `git diff --check` passes.

Deployment remains explicitly withheld. Production backup restoration,
production-copy migration and rollback rehearsal, Linux app image build and
startup, and exact release-artifact delivery remain separate unverified gates.
No live service, Hermes allowlist, financial record, Git commit or push was
changed by these fixes.
