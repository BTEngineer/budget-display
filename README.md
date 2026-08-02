# reTerminal E1001 household budget display

This project combines a private SQLite household budget ledger, a narrow
authenticated MCP service hosted by Home Assistant OS, and a battery-powered
Seeed Studio reTerminal E1001 summary display. Household-specific data is set
after installation and is not stored in this repository.

## Current status

- The E1001 template has a Home page plus a Budget page. Pressing the green
  button toggles between them, while the side buttons refresh the current page.
- The selected page persists through deep sleep, so scheduled wakes refresh
  whichever page is already visible.
- The ledger stores money as integer cents, rejects duplicate request IDs with
  conflicting data, and uses audit-preserving reversals for undo.
- The development release exposes twelve authenticated Streamable HTTP MCP
  tools, including safe correction, atomic splits, classification suggestions,
  and server-calculated budget outlooks.
- Members, categories, subcategories, classification aliases, monthly limits,
  timezone, allowed hosts, and the API token are Home Assistant app options.
- The app publishes read-only current-month totals through Home Assistant's
  internal MQTT service for the E1001 budget-only display.
- Version 0.7 adds an authenticated JSON API, private receipt drafts, numeric
  dashboard sensors, and a phone-first Home Assistant custom card. Receipt AI
  results remain drafts until a person explicitly confirms them.

## Configurable budget structure

The checked-in options contain generic examples only. Before first start,
replace them in the app's Home Assistant **Configuration** tab.

- Each top-level category requires a positive `monthly_budget`.
- A subcategory sets `parent` to the exact top-level category name and leaves
  its own `monthly_budget` blank.
- Category paths and member names must be unique, ignoring capitalization.
- Optional classification aliases map household merchant terms to exact
  categories. They only influence read-only suggestions and never auto-post.
- When a month is first queried, its limits are snapshotted. Later default
  changes therefore do not rewrite historical months.
- Removing a configured member or category prevents new expenses under that
  name without deleting historical ledger entries.

## Ledger guarantees

- Money is stored as integer cents.
- Source message IDs provide idempotency against duplicate delivery.
- Expenses are immutable; undo creates a linked reversal.
- Unknown members, unknown or ambiguous categories, invalid money, and
  timestamps without a timezone are rejected.
- Month assignment uses the configured household timezone.
- SQLite uses foreign keys, write-ahead logging, and immediate write
  transactions for serialized writes.

## Narrow MCP boundary

The app serves Streamable HTTP on port 8099 and persists its database at
`/data/budget.db`. Every request requires a dedicated bearer token and an
allowed HTTP Host value. It exposes only:

- `prepare_expense`
- `add_expense`
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

It exposes no arbitrary SQL, filesystem, shell, deletion, or budget-mutation
tool. Transaction search is bounded and cursor-paginated. Refunds may be linked
to a verified expense or explicitly recorded as unlinked when no original can
be found. Expenses over $500 require `prepare_expense` followed by
`add_expense` with the resulting short-lived signed token. The token is bound
to the exact request ID, amount, member, category, merchant, description, and
timestamp; changing any value invalidates it.
Split totals over $500 use the equivalent `prepare_split_expense` flow.

`correct_expense` atomically records a linked reversal and replacement, never
an in-place edit. `add_split_expense` records all allocations or none and
requires their amounts to equal the stated total. Classification and outlook
tools are read-only; a category suggestion never authorizes a ledger write.

The app also consumes Home Assistant's internal `mqtt` service and publishes
retained discovery/state messages for total spent, total remaining, and each
configured member. Supervisor supplies the MQTT credentials at runtime; they
are never stored in this repository or the app options.

## Install in Home Assistant

1. Open **Settings > Apps > App store**.
2. Open the three-dot menu, choose **Repositories**, and add
   `https://github.com/BTEngineer/budget-display`.
3. Refresh the store, open **Household Budget MCP**, and install it.
4. In **Configuration**, replace the generic members and categories, optionally
   define classification aliases, set the timezone, and add the LAN `host:port`
   used by the MCP client to `allowed_hosts`.
5. Set `api_token` to a unique random value of at least 32 characters. Do not
   reuse a Home Assistant token or another password.
6. Start the app and confirm its log says it is listening on port 8099.

Merge `hermes-config.example.yaml` into the MCP client's configuration, using
the same token via the `BUDGET_MCP_TOKEN` environment variable. The example
keeps an explicit twelve-tool allowlist and disables resources, prompts, and
parallel calls.

## Local development

Python 3.11 or newer is required.

```powershell
cd household_budget_mcp
python -m unittest discover -s tests -v
```

## Home Assistant phone dashboard

The local implementation is under `custom_components/household_budget`. After
review, copy that directory into Home Assistant's `/config/custom_components`,
restart Home Assistant, and add the **Household Budget** integration. Configure
the local app URL, its dedicated API token, and the existing OpenAI AI Task
entity. Then register this dashboard module resource:

```text
/household_budget_static/household-budget-card.js
```

Use `home_assistant/household-budget-dashboard.preview.yaml` as the dashboard
starting point. The custom card never receives the app token or OpenAI key.
Receipt uploads accept JPEG, PNG, or PDF up to 12 MB, request the rear camera on
supported phones, and always require a review screen before ledger insertion.
After the reviewed transaction is committed, the source receipt file is deleted
immediately; its SHA-256 digest and audit metadata remain for duplicate detection.
The browser endpoints are restricted to authenticated Home Assistant
administrators by default; broader household-user access should be a separate,
explicitly reviewed change.

The E1001 template expects these Home Assistant sensors, which the app creates
through MQTT discovery:

- `sensor.household_budget_spent_month`
- `sensor.household_budget_remaining_month`
- `sensor.household_budget_person_1_month`
- `sensor.household_budget_person_2_month`
- `sensor.household_budget_category_1_month` through
  `sensor.household_budget_category_6_month`

The checked-in ESPHome file contains placeholders only. Its Home page shows the
Home Assistant date and time plus the E1001 battery level. Its Budget page
subscribes only to the budget sensors listed above and shows spent and remaining
totals, then up to six configured category rows with budget, first-member,
second-member, and combined spending. Child categories are indented and their
parent row is the aggregate, so the overall total is not double-counted.

The green button wakes the display and toggles Home/Budget exactly once. A
second green-button press toggles back. The side buttons refresh the current
page, and the selected page is retained across deep sleep. The Home page does
not yet display weather, occupancy, doors, person detection, locks, or other
home status. Keep the E1001 on USB power and awake during OTA work.
