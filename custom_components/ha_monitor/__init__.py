"""HA Monitor integration.

HACS 插件：监控辣堡手表健康/位置数据，告警直推米糊 webhook。
包含数据新鲜度 watchdog（断报只推一次）与多套房子在家判断。
晚霞预测提醒：日落前20分钟检测，阈值触发推送。
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .coordinator import HaMonitorCoordinator

_LOGGER = logging.getLogger(__name__)

type HaMonitorConfigEntry = ConfigEntry[HaMonitorCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the HA Monitor component."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: HaMonitorConfigEntry
) -> bool:
    """Set up HA Monitor from a config entry."""
    coordinator = HaMonitorCoordinator(hass, entry)

    # Store coordinator
    entry.runtime_data = coordinator
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Listen to state changes
    @callback
    def _handle_state_change(event: Event) -> None:
        """Handle state change events."""
        entity_id: str = event.data.get("entity_id", "")
        new_state: State | None = event.data.get("new_state")
        old_state: State | None = event.data.get("old_state")

        if not new_state or not old_state:
            return

        coordinator.handle_state_change(entity_id, new_state, old_state)

    # Register event listener
    entry.async_on_unload(
        hass.bus.async_listen(EVENT_STATE_CHANGED, _handle_state_change)
    )

    # Initial refresh
    await coordinator.async_config_entry_first_refresh()

    # Start data freshness watchdog
    await coordinator.async_start_watchdog()
    entry.async_on_unload(coordinator.async_stop_watchdog)

    # Start sunset quality check（新增：晚霞预测提醒）
    await coordinator.async_start_sunset_check()
    entry.async_on_unload(coordinator.async_stop_sunset_check)

    _LOGGER.info("HA Monitor integration loaded for %s", entry.title)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: HaMonitorConfigEntry
) -> bool:
    """Unload a config entry."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator:
        await coordinator.async_stop_watchdog()
        await coordinator.async_stop_sunset_check()
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return True