# reTerminal E1001 household budget display

This project combines a private SQLite household budget ledger, a narrow
authenticated MCP service hosted by Home Assistant OS, and a battery-powered
Seeed Studio reTerminal E1001 summary display. Household-specific data is set
after installation and is not stored in this repository.

## Current status

- The E1001 template has Home and Budget pages and expects future read-only
  Home Assistant budget sensors.
- The ledger stores money as integer cents, rejects duplicate request IDs with
  conflicting data, and uses audit-preserving reversals for undo.
- The Home Assistant app exposes four authenticated Streamable HTTP MCP tools.
- Members, categories, subcategories, monthly limits, timezone, allowed hosts,
  and the API token are Home Assistant app options.

## Configurable budget structure

The checked-in options contain generic examples only. Before first start,
replace them in the app's Home Assistant **Configuration** tab.

- Each top-level category requires a positive `monthly_budget`.
- A subcategory sets `parent` to the exact top-level category name and leaves
  its own `monthly_budget` blank.
- Category paths and member names must be unique, ignoring capitalization.
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

- `add_expense`
- `list_spending`
- `list_budget_categories`
- `undo_last_expense`

It exposes no arbitrary SQL, filesystem, shell, deletion, or budget-mutation
tool. Expenses over $500 require a second call with
`confirm_large_expense=true` after explicit confirmation.

## Install in Home Assistant

1. Open **Settings > Apps > App store**.
2. Open the three-dot menu, choose **Repositories**, and add
   `https://github.com/BTEngineer/budget-display`.
3. Refresh the store, open **Household Budget MCP**, and install it.
4. In **Configuration**, replace the generic members and categories, set the
   timezone, and add the LAN `host:port` used by the MCP client to
   `allowed_hosts`.
5. Set `api_token` to a unique random value of at least 32 characters. Do not
   reuse a Home Assistant token or another password.
6. Start the app and confirm its log says it is listening on port 8099.

Merge `hermes-config.example.yaml` into the MCP client's configuration, using
the same token via the `BUDGET_MCP_TOKEN` environment variable. The example
keeps an explicit four-tool allowlist and disables resources, prompts, and
parallel calls.

## Local development

Python 3.11 or newer is required.

```powershell
cd household_budget_mcp
python -m unittest discover -s tests -v
```

The E1001 template expects these future Home Assistant sensors:

- `sensor.household_budget_spent_month`
- `sensor.household_budget_remaining_month`
- `sensor.household_budget_person_1_month`
- `sensor.household_budget_person_2_month`

The checked-in ESPHome file contains placeholders only. Keep the E1001 on USB
power and awake during OTA work.
