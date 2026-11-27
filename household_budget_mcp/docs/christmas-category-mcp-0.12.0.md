# Christmas expense category in Budget MCP 0.12.0

Budget MCP 0.12.0 and Household Budget for Home Assistant 0.10.0 are
implemented in source. Source delivery does not establish deployment or live acceptance.
The [0.11.0 reference](christmas-spending-mcp-0.11.0.md) documents the
deployed attribution tools and gift allocation rules.

## Category and purchase entry

Configure **Christmas** as a top-level category with a blank `monthly_budget`.
A blank limit is represented as `null`. A supplied monthly limit for an ordinary
root must be positive with at most two decimal places. Christmas uses the
existing per-year budget, which can be unset. Keep the household's configured
annual value at rollout.

The Christmas root and its children are outside the monthly allowance, including
inactive categories on historical entries. Every entry appears in monthly cash
flow, and uncapped ordinary categories count toward the allowance. This release
reads the existing database tables, classifications and budget snapshots in
place. It excludes any stored Christmas monthly limit from the ordinary allowance.

A specific user instruction or deliberate manual selection in Home Assistant
supplies Christmas intent. A receipt, AI output, date, merchant or classification
suggestion supplies no intent. Ordinary suggestions exclude Christmas aliases,
business history and category-name matches. Receipt analysis excludes the
Christmas category and discards Christmas suggestions returned by the AI.

| Tool | When to send the strict boolean `explicitly_requested_christmas: true` |
| --- | --- |
| `prepare_expense` and `add_expense` | The expense is assigned to Christmas. |
| `prepare_split_expense` and `add_split_expense` | At least one allocation is assigned to Christmas. |
| `prepare_correction` and `correct_expense` | The correction changes the financial category to Christmas or one of its children. A correction retaining the original assignment inherits it. |
| `refund_expense` | An unlinked refund is assigned to Christmas. A linked refund inherits its verified original category. |

The flag applies to the Christmas root and its children. An undo follows
the original assignment. CLI `add` accepts `--explicitly-requested-christmas`.
Carry the flag through prepare and commit with the signed confirmation payload
when required. Exact retries reuse the financial request ID and required flag.

For a $70 mixed receipt, use one split with $40 ordinary and $30 Christmas.
Pass the verified `split_transaction_id` to `set_christmas_attribution` with
$20 of gifts, $10 of other Christmas costs, $40 ordinary and
`explicitly_requested: true`. Use a separate attribution request ID. For one
expense, use the verified `transaction_id`. A first assignment defaults to the
purchase's local year. An edit preserves its Christmas year if omitted.
For a categorized purchase, the gifts plus other Christmas costs must match the
financial Christmas amount. Existing ordinary-category attribution stays in the
annual report with a warning about its allowance treatment. Clearing a Christmas
category attribution returns the remaining amount to pending.

A January refund linked to a December Christmas purchase lowers January cash
outflow and leaves the January allowance intact. It reduces the original
Christmas year's total. A refund of an ordinary split allocation leaves the
Christmas figures intact. A partial Christmas return can have a known annual
cash amount and an unresolved gift breakdown.

## Monthly reporting

`list_spending` and `GET /api/v1/summary` return the same fields. Amounts are
signed integer cents. Positive amounts are outflows and refunds reduce them.

| Field | Basis |
| --- | --- |
| `spent_cents`, `by_member_cents`, `by_category_cents` | All monthly cash outflow, including the Christmas category. |
| `category_rows[].spent_cents` and `category_rows[].by_member_cents` | Cash flow for the configured row. Each root includes its children. |
| `category_rows[].outside_monthly_allowance` | True for Christmas and its children. The marker also appears in `list_budget_categories` and `GET /api/v1/config`. |
| `allowance_spent_cents` and `allowance_by_member_cents` | Ordinary allowance spending, including uncapped ordinary categories. |
| `outside_allowance_spent_cents` | Net Christmas category cash outflow, including returns. |
| `budget_cents` and `remaining_cents` | Ordinary monthly limit and balance. Both are null when the ordinary allowance is unset. |

`remaining_cents` now subtracts `allowance_spent_cents` from `budget_cents`.
A $500 allowance with $200 ordinary and $80 Christmas spending leaves $300
of allowance and records $280 of net cash outflow.

`get_budget_outlook` sets `spending_basis` to `monthly_allowance`. Its
`spent_cents` and `allowance_spent_cents` use the allowance, as do remaining,
daily rate, projection, comparison, pace change and category risk.
`cash_flow_spent_cents` and `outside_allowance_spent_cents` show the
corresponding cutoff cash flow.

## Annual completeness and Home Assistant

`get_christmas_summary(year)` reports known attribution for the requested year.
Pending activity has no verified Christmas year, so its counts span all dates.

| Field | Meaning |
| --- | --- |
| `pending_purchase_count` and `pending_purchase_cents` | Christmas category purchases awaiting attribution and their remaining amount after linked refunds. |
| `pending_unlinked_refund_count` and `pending_unlinked_refund_cents` | Christmas category refunds without verified purchase links and their positive absolute amount. |
| `spending_complete` and `comparison_complete` | Both are false when pending activity exists. Other attribution issues can also make figures incomplete. |
| `known_spent_cents` | Amount established by the requested year's existing attribution and linked returns. |
| `spent_cents`, `remaining_cents` and `overspent_cents` | Null when annual spending is incomplete. The latter two also require an annual limit. |

`list_christmas_attributions` continues to return paginated explicit details
and history for the selected year. Preserve warnings and the known-versus-complete
distinction when displaying recipient values.

Home Assistant 0.10.0 shows Christmas details after manual category selection
on an expense or receipt. The review supports one gift with equal recipient
shares, an unassigned gift, or other Christmas costs. MCP handles mixed
financial splits, multiple gifts and custom recipient shares.

The financial write sends `explicitly_requested_christmas: true` to
`/api/v1/expenses` or `/api/v1/receipts/{draft_id}/confirm`. The form verifies
the transaction ID, then uses a separate request ID to save details through
the administrator-only `/api/household_budget/christmas/attribution` endpoint.
This forwards `POST /api/v1/christmas/attributions` with
`explicitly_requested: true` and a verified Christmas category expense.
The annual view shows pending activity until assignment succeeds.

The form keeps one recovery record for up to seven days in the same signed-in
Home Assistant account, origin and browser tab. Closing the recovery panel
or reloading the page leaves ordinary entry available and offers **Review
recovery**. The saved request IDs and expense fields are reused. When the
expense is verified, retry sends only attribution. An uncertain expense over
$500 requires fresh acknowledgement before a financial retry. A verified
receipt conflict for a different request can be dismissed after checking
Recent expenses.

## MQTT and E1001

The existing `spent_month`, `remaining_month`, `budget_month`,
`percent_used_month` and numbered `person_{n}_month` sensor IDs report ordinary
allowance figures. E1001 derives the allowance from spent plus remaining.
New sensors `sensor.household_budget_cash_flow_spent_month` and
`sensor.household_budget_outside_allowance_spent_month` report total and
Christmas net cash outflow.

The six existing packed category slots display configured top-level cash-flow
rows. The intended household configuration uses five rows including Christmas.
Root rows include child totals, and the summary API carries child detail.
The Christmas row has a blank monthly-limit field. The app clears retained
budget, remaining and percentage entities for uncapped categories.

