"""Sensor platform for HA Monitor."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HaMonitorCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HA Monitor sensor from a config entry."""
    coordinator: HaMonitorCoordinator = entry.runtime_data

    async_add_entities(
        [
            HaMonitorStatusSensor(coordinator, entry),
        ]
    )


class HaMonitorStatusSensor(CoordinatorEntity[HaMonitorCoordinator], SensorEntity):
    """Sensor to show HA Monitor status."""

    _attr_has_entity_name = True
    _attr_name = "监控状态"
    _attr_icon = "mdi:heart-pulse"

    def __init__(
        self,
        coordinator: HaMonitorCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_status"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "HA Monitor",
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        data = self.coordinator.data or {}
        alerts_count = data.get("alerts_count", 0)
        is_home = data.get("is_home", False)
        home_states = data.get("home_states", [])
        watchdog_enabled = data.get("watchdog_enabled", False)

        self._attr_native_value = f"监控中 | 告警: {alerts_count} | 在家: {'是' if is_home else '否'}"
        self._attr_extra_state_attributes = {
            "alerts_count": alerts_count,
            "is_home": is_home,
            "home_states": home_states,
            "watchdog_enabled": watchdog_enabled,
            "last_alert": data.get("last_alert"),
        }
        self.async_write_ha_state()
