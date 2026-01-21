from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass
from typing import Optional, Tuple

from bleak import BleakClient

from homeassistant.components import bluetooth
from bleak_retry_connector import establish_connection, BleakNotFoundError

from .const import WRITE_CHAR, NOTIFY_CHAR, CMD_ON, CMD_OFF, POLL52, DOMAIN


@dataclass
class Parsed:
    is_on: Optional[bool] = None
    room_c: Optional[float] = None
    heater_c: Optional[float] = None
    raw52: Optional[bytes] = None


def _find_marker(p: bytes) -> int:
    """Find the start of the tail marker.

    We have observed multiple variants:
      A5 05 02 1E ...
      A5 05 03 1E ...
      A5 05 06 1E ...
      A5 05 09 15 ...

    So we only require A5 05 and then read (b1,b2) at +4,+5.
    """
    for i in range(0, len(p) - 6):
        if p[i] == 0xA5 and p[i + 1] == 0x05:
            return i
    return -1


SYNC = b"\xA5\x05\x09\x15"

def parse_onoff_from_status52(p: bytes) -> Optional[bool]:
    if len(p) != 52:
        return None

    i = p.find(SYNC)
    if i == -1 or i + 6 > len(p):
        return None

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
        room = struct.unpack_from("<h", p, 14)[0] / 10.0
        heater = struct.unpack_from("<h", p, 16)[0] / 10.0
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
        # bleak-retry-connector expects a *client class*, not an instance
        client = await establish_connection(BleakClient, ble_device, address, max_attempts=2)
        await client.disconnect()
        return True, None
    except BleakNotFoundError:
        return False, "not_found"
    except Exception:
        return False, "cannot_connect"


class ProfterHeaterBLE:
    def __init__(self, hass, address: str) -> None:
        self._hass = hass
        self._address = address
        self._lock = asyncio.Lock()
        self._last = Parsed()

    @property
    def last(self) -> Parsed:
        return self._last

    def _parse_52(self, b: bytes) -> None:
        self._last.raw52 = b
        self._last.is_on = parse_onoff_from_status52(b)
        self._last.room_c, self._last.heater_c = parse_temps_best_effort(b)

    async def _connect(self) -> BleakClient:
        ble_device = bluetooth.async_ble_device_from_address(
            self._hass, self._address, connectable=True
        )
        if ble_device is None:
            raise BleakNotFoundError(f"{DOMAIN}: Device not found: {self._address}")

        client = await establish_connection(
            BleakClient,
            ble_device,
            self._address,
            max_attempts=3,
        )
        return client

    async def disconnect(self) -> None:
        # оставляем для coordinator.async_shutdown(), но тут делать нечего
        return

    async def poll_status(self, timeout: float = 6.0) -> Parsed:
        async with self._lock:
            evt = asyncio.Event()

            def cb(_handle: int, data: bytearray) -> None:
                b = bytes(data)
                if len(b) == 52:
                    self._parse_52(b)
                    evt.set()

            client: BleakClient | None = None
            try:
                client = await self._connect()

                # 1) пробуем READ (если реально поддерживается)
                try:
                    b = await client.read_gatt_char(NOTIFY_CHAR)
                    if isinstance(b, (bytes, bytearray)) and len(b) == 52:
                        self._parse_52(bytes(b))
                        return self._last
                except Exception:
                    pass

                # 2) подписка + POLL
                await client.start_notify(NOTIFY_CHAR, cb)

                for _ in range(3):
                    evt.clear()
                    await client.write_gatt_char(WRITE_CHAR, POLL52, response=True)
                    try:
                        await asyncio.wait_for(evt.wait(), timeout=timeout / 3)
                        return self._last
                    except asyncio.TimeoutError:
                        await asyncio.sleep(0.25)

                return self._last

            finally:
                if client:
                    try:
                        await client.stop_notify(NOTIFY_CHAR)
                    except Exception:
                        pass
                    try:
                        await client.disconnect()
                    except Exception:
                        pass

    async def set_on(self, on: bool, timeout: float = 6.0) -> Parsed:
        async with self._lock:
            evt = asyncio.Event()

            def cb(_handle: int, data: bytearray) -> None:
                b = bytes(data)
                if len(b) == 52:
                    self._parse_52(b)
                    evt.set()

            cmd = CMD_ON if on else CMD_OFF
            client: BleakClient | None = None
            try:
                client = await self._connect()
                await client.start_notify(NOTIFY_CHAR, cb)

                await client.write_gatt_char(WRITE_CHAR, cmd, response=True)
                await asyncio.sleep(0.25)

                # форсим свежий статус
                evt.clear()
                await client.write_gatt_char(WRITE_CHAR, POLL52, response=True)
                try:
                    await asyncio.wait_for(evt.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass

                return self._last

            finally:
                if client:
                    try:
                        await client.stop_notify(NOTIFY_CHAR)
                    except Exception:
                        pass
                    try:
                        await client.disconnect()
                    except Exception:
                        pass