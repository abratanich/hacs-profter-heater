from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.const import UnitOfTemperature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ProfterHeaterCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: ProfterHeaterCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            HeaterStateSensor(coordinator, entry),
            HeaterRoomTempSensor(coordinator, entry),
            HeaterCoreTempSensor(coordinator, entry),
            HeaterRaw52Sensor(coordinator, entry),
        ]
    )


class _Base(CoordinatorEntity[ProfterHeaterCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: ProfterHeaterCoordinator, entry: ConfigEntry, key: str, name: str) -> None:
        super().__init__(coordinator)
        self._attr_name = name
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.unique_id)}}


class HeaterStateSensor(_Base):
    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "state", "State")

    @property
    def native_value(self):
        v = self.coordinator.data.is_on
        if v is True:
            return "ON"
        if v is False:
            return "OFF"
        return "UNKNOWN"


class HeaterRoomTempSensor(_Base):
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_enabled_by_default = True

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "room_temp", "Room Temperature")

    @property
    def native_value(self):
        return self.coordinator.data.room_c


class HeaterCoreTempSensor(_Base):
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_enabled_by_default = True

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "heater_temp", "Heater Temperature")

    @property
    def native_value(self):
        return self.coordinator.data.heater_c


class HeaterRaw52Sensor(_Base):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_enabled_by_default = False

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "raw52", "Raw Status 52")

    @property
    def native_value(self):
        raw = self.coordinator.data.raw52
        return raw.hex().upper() if raw else None
