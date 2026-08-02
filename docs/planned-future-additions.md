# Planned future budget additions

This document records deliberately deferred capabilities. Neither feature is
implemented, exposed through MCP, enabled in Hermes, or included in the Home
Assistant app configuration today. Each requires a separate design review
before code or live financial data changes.

## 1. Recurring commitments and upcoming bills

### Goal

Represent expected subscriptions, utilities, debt payments, and other planned
commitments separately from posted ledger expenses. This would let the budget
answer "what is still due?" and produce a more useful safe-to-spend value
without pretending an expected bill has already cleared.

### Proposed boundary

- Store recurrence definitions and expected occurrences in dedicated tables;
  never insert them directly into `ledger_entries`.
- Start with a read-only MCP tool such as `list_upcoming_commitments`.
- Require an explicit source request and normal expense validation when an
  expected occurrence is posted to the authoritative ledger.
- Link the posted expense to its expected occurrence for audit and duplicate
  detection, while retaining both records.
- Support fixed and estimated amounts, due-date windows, paused schedules, and
  an explicit skipped state.
- Calculate "remaining after commitments" separately from current ledger
  remaining, with clear labels for posted versus expected money.

### Required safety and acceptance tests

- Month-end, leap-year, daylight-saving, and variable billing-date behavior.
- No double posting after duplicate messages, restarts, or delayed delivery.
- Amount changes do not rewrite previously posted expenses.
- Paused, skipped, or deleted schedules preserve historical occurrences.
- Forecast responses identify estimates and never present them as cleared
  transactions.
- Backup and restore preserve recurrence state and ledger links.

## 2. Statement reconciliation and staged imports

### Goal

Compare bank or card statement rows with the local ledger, identify likely
matches and duplicates, and stage missing transactions for explicit review.

### Proposed boundary

- Upload and parse statement files through a dedicated authenticated API, not
  as arbitrary MCP filesystem access.
- Treat every imported row and parser result as untrusted draft data.
- Store content hashes, source account aliases, statement periods, and import
  audit metadata without storing online-banking credentials.
- Expose only bounded read-only MCP reconciliation queries at first.
- Match using normalized date windows, exact cents, merchant text, and existing
  split/refund relationships; always return match evidence and confidence.
- Require explicit confirmation for each item or a separately reviewed bounded
  batch before creating ledger entries.
- Keep imported statement rows and confirmed ledger records linked; never
  delete or overwrite ledger history to make a statement balance.

### Required safety and acceptance tests

- Duplicate files, overlapping statement periods, and repeated statement rows.
- Ambiguous one-to-many and many-to-one matches, including split expenses.
- Pending versus posted bank rows and merchant-name changes.
- Refunds, partial refunds, reversals, corrections, and cross-month posting.
- CSV formula injection, malformed encodings, oversized files, hostile PDF
  content, and bounded parser resource use.
- Atomic confirmation, exact request idempotency, failure recovery, and backup
  restoration.
- A dry-run report that proves no ledger mutation occurred before approval.

## Sequencing recommendation

Implement recurring commitments first because it is fully local and introduces
no untrusted bulk file parser. Reconciliation should follow only after the
correction and split models have been exercised with real household workflows,
and after an explicit import threat model and cleanup procedure are approved.
