from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from typing import Optional, Tuple

from bleak import BleakClient
from bleak_retry_connector import (
    establish_connection,
    BleakNotFoundError,
    BleakConnectionError,
)

from homeassistant.components import bluetooth

from .const import WRITE_CHAR, NOTIFY_CHAR, CMD_ON, CMD_OFF, POLL52, DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass
class Parsed:
    is_on: Optional[bool] = None
    room_c: Optional[float] = None
    heater_c: Optional[float] = None
    raw52: Optional[bytes] = None


def parse_onoff_from_status52(p: bytes) -> Optional[bool]:
    if len(p) != 52:
        return None

    for i in range(len(p) - 6):
        if p[i] == 0xA5 and p[i + 1] == 0x05:
            b1 = p[i + 4]
            b2 = p[i + 5]
            if (b1, b2) == (0x01, 0x73):
                return True
            if (b1, b2) == (0x02, 0xEF):
                return False
    return None


def parse_temps_best_effort(p: bytes) -> Tuple[Optional[float], Optional[float]]:
    if len(p) != 52:
        return (None, None)

    try:
        # как у тебя сейчас: room@14, heater@16
        heater = struct.unpack_from("<h", p, 16)[0] / 10.0
        room = struct.unpack_from("<h", p, 14)[0] / 10.0
    except struct.error:
        return (None, None)

    if not (-40.0 <= room <= 80.0):
        room = None
    if not (-40.0 <= heater <= 250.0):
        heater = None
    return (room, heater)


async def async_can_connect(hass, address: str) -> tuple[bool, Optional[str]]:
    try:
        ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
        if ble_device is None:
            return False, "not_found"
        client = await establish_connection(BleakClient, ble_device, address, max_attempts=2)
        await client.disconnect()
        return True, None
    except BleakNotFoundError:
        return False, "not_found"
    except Exception:
        return False, "cannot_connect"


class ProfterHeaterBLE:
    """
    Что добавлено, чтобы реально работало в фоне:
    1) keepalive-таск, который регулярно шлёт POLL (иначе notify может "уснуть", а HA будет видеть UNKNOWN)
    2) явный start/stop этого таска при connect/disconnect
    3) обработка "не пришёл notify" -> реконнект и повтор
    4) защита от "липкого" event: чистим его до и после ожиданий
    5) логирование для понимания: приходят ли notify и когда рвётся соединение
    """

    def __init__(self, hass, address: str) -> None:
        self._hass = hass
        self._address = address

        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()

        self._last = Parsed()
        self._evt = asyncio.Event()

        self._keepalive_task: asyncio.Task | None = None
        self._keepalive_interval_s: float = 20.0  # можно вынести в options

    @property
    def last(self) -> Parsed:
        return self._last

    def _notification_cb(self, _handle: int, data: bytearray) -> None:
        b = bytes(data)
        if len(b) != 52:
            return
        self._last.raw52 = b
        self._last.is_on = parse_onoff_from_status52(b)
        self._last.room_c, self._last.heater_c = parse_temps_best_effort(b)
        # важно: set из callback (sync) — ок
        self._evt.set()

        _LOGGER.debug(
            "%s notify: on=%s room=%s heater=%s tail=%s",
            self._address,
            self._last.is_on,
            self._last.room_c,
            self._last.heater_c,
            b[-8:].hex(),
        )

    async def _wait_ble_device(self, timeout: float = 10.0):
        end = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < end:
            ble_device = bluetooth.async_ble_device_from_address(
                self._hass, self._address, connectable=True
            )
            if ble_device is not None:
                return ble_device
            await asyncio.sleep(0.5)
        raise BleakNotFoundError(f"{DOMAIN}: Device not found (no adv): {self._address}")

    async def connect(self) -> None:
        ble_device = await self._wait_ble_device(timeout=10.0)

        self._client = await establish_connection(
            BleakClient,
            ble_device,
            self._address,
            max_attempts=3,
        )

        await self._client.start_notify(NOTIFY_CHAR, self._notification_cb)

        _LOGGER.debug("%s connected + notify started", self._address)

        # запускаем keepalive, чтобы в фоне продолжали обновляться данные
        self._start_keepalive()

    async def disconnect(self) -> None:
        self._stop_keepalive()

        if not self._client:
            return
        try:
            await self._client.stop_notify(NOTIFY_CHAR)
        except Exception:
            pass
        try:
            await self._client.disconnect()
        except Exception:
            pass

        self._client = None
        _LOGGER.debug("%s disconnected", self._address)

    def _start_keepalive(self) -> None:
        if self._keepalive_task and not self._keepalive_task.done():
            return
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    def _stop_keepalive(self) -> None:
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        self._keepalive_task = None

    async def _keepalive_loop(self) -> None:
        """
        Важно: не держим _lock постоянно, чтобы не блокировать set_on().
        Просто периодически делаем poll_status() — он сам возьмёт lock и обеспечит актуальность.
        """
        try:
            while True:
                await asyncio.sleep(self._keepalive_interval_s)
                try:
                    await self.poll_status(timeout=6.0)
                except Exception as e:
                    _LOGGER.debug("%s keepalive poll failed: %r", self._address, e)
        except asyncio.CancelledError:
            return

    async def _ensure(self) -> BleakClient:
        if self._client and self._client.is_connected:
            return self._client
        await self.disconnect()
        await self.connect()
        assert self._client is not None
        return self._client

    async def _poll_once(self, c: BleakClient, timeout: float) -> bool:
        """
        Отправить POLL и дождаться notify.
        Возвращает True если notify пришёл, иначе False.
        """
        # обязательно чистим событие ДО poll
        self._evt.clear()

        # POLL — без response, иначе у некоторых устройств notify "не приходит"
        await c.write_gatt_char(WRITE_CHAR, POLL52, response=False)

        try:
            await asyncio.wait_for(self._evt.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            return False

    async def poll_status(self, timeout: float = 6.0) -> Parsed:
        async with self._lock:
            c = await self._ensure()

            slice_t = max(0.8, timeout / 3)

            # 3 попытки на живом соединении
            for attempt in range(3):
                ok = await self._poll_once(c, timeout=slice_t)
                if ok:
                    return self._last
                _LOGGER.debug("%s poll timeout attempt=%s", self._address, attempt + 1)
                await asyncio.sleep(0.25)

            # если не пришло — реконнект и последняя попытка
            _LOGGER.debug("%s poll: reconnect + retry", self._address)
            await self.disconnect()
            c = await self._ensure()

            ok = await self._poll_once(c, timeout=max(1.2, timeout / 2))
            return self._last if ok else self._last

    async def set_on(self, on: bool, timeout: float = 6.0) -> Parsed:
        async with self._lock:
            c = await self._ensure()
            cmd = CMD_ON if on else CMD_OFF

            self._evt.clear()

            # команду можно оставлять response=True, но если иногда "зависает" — переведи в False
            await c.write_gatt_char(WRITE_CHAR, cmd, response=True)
            await asyncio.sleep(0.25)

            # добиваемся свежего статуса POLL-ом
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                ok = await self._poll_once(c, timeout=0.8)
                if ok and self._last.is_on is not None and self._last.is_on == on:
                    return self._last
                await asyncio.sleep(0.25)

            return self._last