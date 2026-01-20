from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional, Tuple

from bleak import BleakClient, BleakScanner

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
        if p[i] == 0xA5 and p[i + 1] == 0x05 and p[i + 3] == 0x1E:
            return i
    return -1


def parse_onoff_from_status52(p: bytes) -> Optional[bool]:
    # ... A5 05 ?? 1E 01 73 ... => ON
    # ... A5 05 ?? 1E 02 EF ... => OFF
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
    # Температуры пока не подтверждены (оффсеты/тип данных).
    # Возвращаем None, чтобы не публиковать неверные значения.
    return (None, None)


async def _find_device(address: str, timeout: float = 8.0):
    dev = await BleakScanner.find_device_by_address(address, timeout=timeout)
    if dev:
        return dev
    for d in await BleakScanner.discover(timeout=timeout):
        if (d.address or "").lower() == address.lower():
            return d
    return None


async def async_can_connect(_hass, address: str) -> tuple[bool, Optional[str]]:
    try:
        dev = await _find_device(address, timeout=6.0)
        if not dev:
            return False, "not_found"
        async with BleakClient(dev) as c:
            if not c.is_connected:
                return False, "cannot_connect"
        return True, None
    except Exception:
        return False, "cannot_connect"


class ProfterHeaterBLE:
    def __init__(self, address: str):
        self._address = address
        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._last = Parsed()
        self._got_evt = asyncio.Event()

    @property
    def last(self) -> Parsed:
        return self._last

    async def connect(self) -> None:
        dev = await _find_device(self._address, timeout=10.0)
        if not dev:
            raise RuntimeError("Device not found")
        self._client = BleakClient(dev)
        await self._client.connect()
        if not self._client.is_connected:
            raise RuntimeError("BLE connect failed")

        def cb(_handle: int, data: bytearray):
            b = bytes(data)
            if len(b) == 52:
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

            # Иногда READ работает, но на практике часто нет — оставим как бонус
            try:
                b = await c.read_gatt_char(NOTIFY_CHAR)
                if isinstance(b, (bytes, bytearray)) and len(b) == 52:
                    b = bytes(b)
                    self._last.raw52 = b
                    self._last.is_on = parse_onoff_from_status52(b)
                    self._last.room_c, self._last.heater_c = parse_temps_best_effort(b)
                    return self._last
            except Exception:
                pass

            for attempt in range(3):
                self._got_evt.clear()

                try:
                    await c.write_gatt_char(WRITE_CHAR, POLL52, response=True)
                except Exception:
                    await self.disconnect()
                    c = await self._ensure()
                    await c.write_gatt_char(WRITE_CHAR, POLL52, response=True)

                try:
                    await asyncio.wait_for(self._got_evt.wait(), timeout=timeout)
                    return self._last
                except asyncio.TimeoutError:
                    await asyncio.sleep(0.3)
                except asyncio.CancelledError:
                    # HA отменил таск — просто возвращаем то, что есть
                    return self._last

            return self._last

    async def set_on(self, on: bool, timeout: float = 6.0) -> Parsed:
        async with self._lock:
            c = await self._ensure()

            cmd = CMD_ON if on else CMD_OFF
            self._got_evt.clear()

            # 1) отправляем команду
            await c.write_gatt_char(WRITE_CHAR, cmd, response=True)

            # 2) ждём чуть-чуть (как ты уже делал в CLI)
            await asyncio.sleep(0.25)

            # 3) опрашиваем статус устойчивым методом (read + poll)
            await self.poll_status(timeout=timeout)

            return self._last
