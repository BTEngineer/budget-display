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
four-tool allowlist in `hermes-config.example.yaml`.

The `confirm_large_expense` flag is an advisory caller acknowledgement. It
causes the server to reject an initial expense over $500 when absent, but it is
not proof that a person approved the transaction.

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

Additional configured members use the same numbered pattern. Sensor
availability follows the budget app's MQTT connection.

## Security

Every MCP request requires the bearer token. The server also rejects unlisted
HTTP Host values. The app requests no Home Assistant, Supervisor, host network,
device, configuration, or filesystem privileges beyond its private `/data`
volume and exposed TCP port.
