"""Household Budget Home Assistant integration."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import BudgetClient
from .const import CONF_API_TOKEN, CONF_BASE_URL, DATA_CLIENT, DOMAIN
from .views import VIEWS


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    config = {**entry.data, **entry.options}
    client = BudgetClient(
        async_get_clientsession(hass),
        config[CONF_BASE_URL],
        config[CONF_API_TOKEN],
    )
    await client.config()
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    domain_data = hass.data.setdefault(DOMAIN, {})
    domain_data[DATA_CLIENT] = client
    domain_data["entry"] = entry

    if not domain_data.get("http_registered"):
        for view in VIEWS:
            hass.http.register_view(view())
        static_path = Path(__file__).parent / "frontend"
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    "/household_budget_static",
                    str(static_path),
                    False,
                )
            ]
        )
        domain_data["http_registered"] = True
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration after its connection options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    domain_data = hass.data.get(DOMAIN, {})
    domain_data.pop(DATA_CLIENT, None)
    domain_data.pop("entry", None)
    return True
