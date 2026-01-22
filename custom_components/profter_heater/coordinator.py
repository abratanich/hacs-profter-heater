from __future__ import annotations

from datetime import timedelta
import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, CONF_ADDRESS, CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
from .ble import ProfterHeaterBLE, Parsed

_LOGGER = logging.getLogger(__name__)

# BLE-реальность: опрашивать чаще 15 сек обычно вредно
MIN_EFFECTIVE_POLL_SECONDS = 15


class ProfterHeaterCoordinator(DataUpdateCoordinator[Parsed]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS]

        poll = int(
            entry.options.get(
                CONF_POLL_INTERVAL,
                entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
            )
        )

        # update_interval оставляем как настроено (пусть тикает хоть 10),
        # но реальный BLE-запрос будем ограничивать MIN_EFFECTIVE_POLL_SECONDS.
        self._configured_poll = poll
        self._last_ble_poll_monotonic: float = 0.0

        self.ble = ProfterHeaterBLE(hass, self.address)

        super().__init__(
            hass,
            logger=_LOGGER,
            name=f"{DOMAIN}_{self.address}",
            update_interval=timedelta(seconds=poll),
        )

    async def _async_update_data(self) -> Parsed:
        now = time.monotonic()
        since = now - self._last_ble_poll_monotonic

        # если тик пришёл слишком рано — не трогаем BLE, отдаём последнее
        if self._last_ble_poll_monotonic and since < MIN_EFFECTIVE_POLL_SECONDS:
            _LOGGER.debug(
                "TICK skip BLE poll (%ss < %ss) %s",
                round(since, 2),
                MIN_EFFECTIVE_POLL_SECONDS,
                self.address,
            )
            # coordinator.data может быть None на старте — тогда пусть всё же попробует BLE
            if self.data is not None:
                return self.data

        _LOGGER.debug("TICK poll_status() %s (configured=%ss)", self.address, self._configured_poll)

        try:
            data = await self.ble.poll_status(timeout=6.0)
            self._last_ble_poll_monotonic = time.monotonic()

            _LOGGER.debug(
                "GOT %s on=%s room=%s heater=%s",
                self.address, data.is_on, data.room_c, data.heater_c
            )
            return data
        except Exception as e:
            raise UpdateFailed(str(e)) from e

    async def async_shutdown(self) -> None:
        await self.ble.disconnect()

    async def async_set_power(self, on: bool) -> None:
        # Команда может идти сразу, без throttle (это “ручное действие”)
        await self.ble.set_on(on, timeout=6.0)

        # после команды считаем, что “свежее” уже получали/пытались получить
        self._last_ble_poll_monotonic = time.monotonic()

        await self.async_request_refresh()