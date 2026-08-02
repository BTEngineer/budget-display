"""UI configuration flow for Household Budget."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client import BudgetAPIError, BudgetClient
from .const import (
    CONF_AI_TASK_ENTITY,
    CONF_API_TOKEN,
    CONF_BASE_URL,
    DEFAULT_BASE_URL,
    DOMAIN,
)


class HouseholdBudgetConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            client = BudgetClient(
                async_get_clientsession(self.hass),
                user_input[CONF_BASE_URL],
                user_input[CONF_API_TOKEN],
            )
            try:
                await client.config()
            except (BudgetAPIError, OSError):
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title="Household Budget", data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BASE_URL, default=DEFAULT_BASE_URL): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.URL)
                    ),
                    vol.Required(CONF_API_TOKEN): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                    vol.Required(CONF_AI_TASK_ENTITY): EntitySelector(
                        EntitySelectorConfig(domain="ai_task")
                    ),
                }
            ),
            errors=errors,
        )
