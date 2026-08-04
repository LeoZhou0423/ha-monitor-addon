"""Config flow for HA Monitor integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_ALERTS_FILE,
    CONF_COOLDOWN,
    CONF_ENTITIES,
    CONF_HR_HIGH,
    CONF_HR_LOW,
    CONF_STALE_MIN,
    CONF_TEMP_HIGH,
    CONF_TEMP_LOW,
    CONF_WATCHDOG_ENABLED,
    CONF_WEBHOOK_SECRET,
    CONF_WEBHOOK_URL,
    DEFAULT_ALERTS_FILE,
    DEFAULT_COOLDOWN,
    DEFAULT_HR_HIGH,
    DEFAULT_HR_LOW,
    DEFAULT_STALE_MIN,
    DEFAULT_TEMP_HIGH,
    DEFAULT_TEMP_LOW,
    DEFAULT_WATCHDOG_ENABLED,
    DEFAULT_WEBHOOK_SECRET,
    DEFAULT_WEBHOOK_URL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default="辣堡健康监控"): str,
        vol.Optional(CONF_ENTITIES, default=[]): selector.EntitySelector(
            selector.EntitySelectorConfig(
                domain=["sensor", "device_tracker", "person", "input_select"],
                multiple=True,
            )
        ),
        vol.Optional(CONF_TEMP_LOW, default=DEFAULT_TEMP_LOW): vol.Coerce(float),
        vol.Optional(CONF_TEMP_HIGH, default=DEFAULT_TEMP_HIGH): vol.Coerce(float),
        vol.Optional(CONF_HR_HIGH, default=DEFAULT_HR_HIGH): vol.Coerce(int),
        vol.Optional(CONF_HR_LOW, default=DEFAULT_HR_LOW): vol.Coerce(int),
        vol.Optional(CONF_ALERTS_FILE, default=DEFAULT_ALERTS_FILE): str,
        vol.Optional(CONF_COOLDOWN, default=DEFAULT_COOLDOWN): vol.Coerce(int),
        vol.Optional(CONF_WEBHOOK_URL, default=DEFAULT_WEBHOOK_URL): str,
        vol.Optional(CONF_WEBHOOK_SECRET, default=DEFAULT_WEBHOOK_SECRET): str,
        vol.Optional(CONF_WATCHDOG_ENABLED, default=DEFAULT_WATCHDOG_ENABLED): selector.BooleanSelector(),
        vol.Optional(CONF_STALE_MIN, default=DEFAULT_STALE_MIN): vol.Coerce(int),
    }
)


class HaMonitorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HA Monitor."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_NAME])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=user_input[CONF_NAME],
                data=user_input,
            )
        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return HaMonitorOptionsFlow(config_entry)


class HaMonitorOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for HA Monitor."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_ENTITIES,
                    default=self.config_entry.data.get(CONF_ENTITIES, []),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        domain=["sensor", "device_tracker", "person", "input_select"],
                        multiple=True,
                    )
                ),
                vol.Optional(
                    CONF_TEMP_LOW,
                    default=self.config_entry.data.get(CONF_TEMP_LOW, DEFAULT_TEMP_LOW),
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_TEMP_HIGH,
                    default=self.config_entry.data.get(CONF_TEMP_HIGH, DEFAULT_TEMP_HIGH),
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_HR_HIGH,
                    default=self.config_entry.data.get(CONF_HR_HIGH, DEFAULT_HR_HIGH),
                ): vol.Coerce(int),
                vol.Optional(
                    CONF_HR_LOW,
                    default=self.config_entry.data.get(CONF_HR_LOW, DEFAULT_HR_LOW),
                ): vol.Coerce(int),
                vol.Optional(
                    CONF_ALERTS_FILE,
                    default=self.config_entry.data.get(CONF_ALERTS_FILE, DEFAULT_ALERTS_FILE),
                ): str,
                vol.Optional(
                    CONF_COOLDOWN,
                    default=self.config_entry.data.get(CONF_COOLDOWN, DEFAULT_COOLDOWN),
                ): vol.Coerce(int),
                vol.Optional(
                    CONF_WEBHOOK_URL,
                    default=self.config_entry.data.get(CONF_WEBHOOK_URL, DEFAULT_WEBHOOK_URL),
                ): str,
                vol.Optional(
                    CONF_WEBHOOK_SECRET,
                    default=self.config_entry.data.get(CONF_WEBHOOK_SECRET, DEFAULT_WEBHOOK_SECRET),
                ): str,
                vol.Optional(
                    CONF_WATCHDOG_ENABLED,
                    default=self.config_entry.data.get(CONF_WATCHDOG_ENABLED, DEFAULT_WATCHDOG_ENABLED),
                ): selector.BooleanSelector(),
                vol.Optional(
                    CONF_STALE_MIN,
                    default=self.config_entry.data.get(CONF_STALE_MIN, DEFAULT_STALE_MIN),
                ): vol.Coerce(int),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                options_schema, self.config_entry.options
            ),
        )
