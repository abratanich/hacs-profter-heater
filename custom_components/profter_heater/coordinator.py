from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, CONF_ADDRESS, CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
from .ble import ProfterHeaterBLE, Parsed

_LOGGER = logging.getLogger(__name__)


class ProfterHeaterCoordinator(DataUpdateCoordinator[Parsed]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.address = entry.data[CONF_ADDRESS]
        poll = entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)

        self.ble = ProfterHeaterBLE(self.address)

        super().__init__(
            hass,
            logger=_LOGGER,
            name=f"{DOMAIN}_{self.address}",
            update_interval=timedelta(seconds=int(poll)),
        )

    async def _async_update_data(self) -> Parsed:
        try:
            return await self.ble.poll_status(timeout=3.0)
        except Exception as e:
            raise UpdateFailed(str(e)) from e

    async def async_shutdown(self) -> None:
        await self.ble.disconnect()

    async def async_set_power(self, on: bool) -> None:
        await self.ble.set_on(on, timeout=4.0)
        await self.async_request_refresh()
