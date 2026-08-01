#!/usr/bin/with-contenv bashio
set -euo pipefail

export BUDGET_OPTIONS_PATH="/data/options.json"
export BUDGET_DB_PATH="/data/budget.db"
export BUDGET_BIND_HOST="0.0.0.0"
export BUDGET_PORT="8099"

bashio::log.info "Starting authenticated Household Budget MCP server on port 8099"
exec python3 -m budget_display.mcp_http
