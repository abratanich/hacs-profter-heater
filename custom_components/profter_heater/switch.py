from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ProfterHeaterCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: ProfterHeaterCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ProfterHeaterSwitch(coordinator, entry)])


class ProfterHeaterSwitch(CoordinatorEntity[ProfterHeaterCoordinator], SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = "Heater"
    _attr_icon = "mdi:radiator"

    def __init__(self, coordinator: ProfterHeaterCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.unique_id}_switch"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.unique_id)},
            "name": f"Profter Heater ({coordinator.address})",
            "manufacturer": "Profter",
            "model": "BLE Heater Controller",
        }

    @property
    def is_on(self):
        return bool(self.coordinator.data.is_on) if self.coordinator.data.is_on is not None else False

    async def async_turn_on(self, **kwargs) -> None:
        await self.coordinator.async_set_power(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.coordinator.async_set_power(False)
