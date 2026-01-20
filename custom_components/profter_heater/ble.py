from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional, Tuple

from bleak import BleakClient

from homeassistant.components.bluetooth import async_ble_device_from_address
from bleak_retry_connector import establish_connection

from .const import WRITE_CHAR, NOTIFY_CHAR, CMD_ON, CMD_OFF, POLL52


@dataclass
class Parsed:
    is_on: Optional[bool] = None
    room_c: Optional[float] = None
    heater_c: Optional[float] = None
    raw52: Optional[bytes] = None


def _find_sync_index(p: bytes) -> int:
    # Ищем паттерн A5 05 ?? 1E
    for i in range(0, len(p) - 3):
        if p[i] == 0xA5 and p[i + 1] == 0xA5:  # защита от случайных совпадений? не нужно
            pass
    for i in range(0, len(p) - 3):
        if p[i] == 0xA5 and p[i + 1] == 0x05 and p[i + 3] == 0x1E:
            return i
    return -1


def parse_onoff_from_status52(p: bytes) -> Optional[bool]:
    if len(p) != 52:
        return None
    i = _find_sync_index(p)
    if i < 0 or i + 6 > len(p):
        return None
    b1, b2 = p[i + 4], p[i + 5]
    if (b1, b2) == (0x01, 0x73):
        return True
    if (b1, b2) == (0x02, 0xEF):
        return False
    return None


def parse_temps_best_effort(_p: bytes) -> Tuple[Optional[float], Optional[float]]:
    # Пока не публикуем неподтверждённые значения
    return (None, None)


async def async_can_connect(hass, address: str) -> tuple[bool, Optional[str]]:
    try:
        ble_device = async_ble_device_from_address(hass, address.upper(), connectable=True)
        if ble_device is None:
            return False, "not_found"
        client = await establish_connection(BleakClient, ble_device, address.upper())
        try:
            if not client.is_connected:
                return False, "cannot_connect"
        finally:
            await client.disconnect()
        return True, None
    except Exception:
        return False, "cannot_connect"


class ProfterHeaterBLE:
    def __init__(self, hass, address: str):
        self._hass = hass
        self._address = address.upper()
        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._last = Parsed()
        self._got_evt = asyncio.Event()

    @property
    def last(self) -> Parsed:
        return self._last

    async def connect(self) -> None:
        ble_device = async_ble_device_from_address(self._hass, self._address, connectable=True)
        if ble_device is None:
            raise RuntimeError("Device not found (not in HA Bluetooth cache)")

        self._client = await establish_connection(BleakClient, ble_device, self._address)

        def cb(_handle: int, data: bytearray):
            b = bytes(data)
            if len(b) != 52:
                return
            self._last.raw52 = b
            self._last.is_on = parse_onoff_from_status52(b)
            self._last.room_c, self._last.heater_c = parse_temps_best_effort(b)
            self._got_evt.set()

        await self._client.start_notify(NOTIFY_CHAR, cb)

    async def disconnect(self) -> None:
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

    async def _ensure(self) -> BleakClient:
        if self._client and self._client.is_connected:
            return self._client
        await self.disconnect()
        await self.connect()
        assert self._client is not None
        return self._client

    async def poll_status(self, timeout: float = 6.0) -> Parsed:
        async with self._lock:
            c = await self._ensure()

            # 1) дергаем POLL, ждём notify 52 байта
            for _ in range(3):
                self._got_evt.clear()
                await c.write_gatt_char(WRITE_CHAR, POLL52, response=True)
                try:
                    await asyncio.wait_for(self._got_evt.wait(), timeout=timeout)
                    return self._last
                except asyncio.TimeoutError:
                    await asyncio.sleep(0.25)

            return self._last

    async def set_on(self, on: bool, timeout: float = 6.0) -> Parsed:
        async with self._lock:
            c = await self._ensure()
            self._got_evt.clear()

            cmd = CMD_ON if on else CMD_OFF
            await c.write_gatt_char(WRITE_CHAR, cmd, response=True)

            await asyncio.sleep(0.25)

            # форсируем свежий статус
            await c.write_gatt_char(WRITE_CHAR, POLL52, response=True)
            try:
                await asyncio.wait_for(self._got_evt.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

            return self._last