#!/usr/bin/with-contenv bashio
set -euo pipefail

export BUDGET_OPTIONS_PATH="/data/options.json"
export BUDGET_DB_PATH="/data/budget.db"
export BUDGET_BIND_HOST="0.0.0.0"
export BUDGET_PORT="8099"
export BUDGET_MQTT_HOST="$(bashio::services mqtt 'host')"
export BUDGET_MQTT_PORT="$(bashio::services mqtt 'port')"
export BUDGET_MQTT_USERNAME="$(bashio::services mqtt 'username')"
export BUDGET_MQTT_PASSWORD="$(bashio::services mqtt 'password')"

bashio::log.info "Starting Household Budget MCP and read-only Home Assistant sensors"
exec python3 -m budget_display.mcp_http
