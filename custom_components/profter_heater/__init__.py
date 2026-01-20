from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import ProfterHeaterCoordinator

PLATFORMS: list[str] = ["switch", "sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = ProfterHeaterCoordinator(hass, entry)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator  # <= раньше

    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator: ProfterHeaterCoordinator = hass.data[DOMAIN].pop(entry.entry_id)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await coordinator.async_shutdown()

    return unload_ok