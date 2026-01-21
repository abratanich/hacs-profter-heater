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
    """Parse temperatures from known offsets.

    Based on captured 52-byte frames:
      - heater temp appears as int16 LE at offset 12, scaled by 0.1°C (e.g. 0x0320=800 => 80.0°C)
      - room temp appears as int16 LE at offset 14, scaled by 0.1°C (e.g. 0x00CA=202 => 20.2°C)

    If values fall outside sane ranges, return None.
    """
    if len(p) != 52:
        return (None, None)

    try:
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
        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._last = Parsed()
        self._evt = asyncio.Event()

    @property
    def last(self) -> Parsed:
        return self._last

    # Bleak requires a *sync* callback for notifications.
    def _notification_cb(self, _handle: int, data: bytearray) -> None:
        b = bytes(data)
        if len(b) != 52:
            return
        self._last.raw52 = b
        self._last.is_on = parse_onoff_from_status52(b)
        self._last.room_c, self._last.heater_c = parse_temps_best_effort(b)
        self._evt.set()

    async def connect(self) -> None:
        ble_device = bluetooth.async_ble_device_from_address(self._hass, self._address, connectable=True)
        if ble_device is None:
            raise BleakNotFoundError(f"{DOMAIN}: Device not found: {self._address}")

        # bleak-retry-connector expects a *client class*, not an instance
        self._client = await establish_connection(
            BleakClient,
            ble_device,
            self._address,
            max_attempts=3,
        )

        await self._client.start_notify(NOTIFY_CHAR, self._notification_cb)

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

            # 1) try READ first (notify characteristic also has read)
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

            # 2) then POLL and wait for notify
            for _ in range(3):
                self._evt.clear()
                await c.write_gatt_char(WRITE_CHAR, POLL52, response=False)
                await asyncio.sleep(0.05)  # короткая пауза помогает некоторым устройствам
                try:
                    await asyncio.wait_for(self._evt.wait(), timeout=timeout / 3)
                    return self._last
                except asyncio.TimeoutError:
                    await asyncio.sleep(0.25)
                except asyncio.CancelledError:
                    return self._last

            await self.disconnect()
            c = await self._ensure()

            self._evt.clear()
            await c.write_gatt_char(WRITE_CHAR, POLL52, response=False)
            try:
                await asyncio.wait_for(self._evt.wait(), timeout=timeout / 2)
            except asyncio.TimeoutError:
                pass

            return self._last

    async def set_on(self, on: bool, timeout: float = 6.0) -> Parsed:
        async with self._lock:
            c = await self._ensure()
            cmd = CMD_ON if on else CMD_OFF

            self._evt.clear()
            await c.write_gatt_char(WRITE_CHAR, cmd, response=False)
            await asyncio.sleep(0.25)
            await c.write_gatt_char(WRITE_CHAR, POLL52, response=False)

            try:
                await asyncio.wait_for(self._evt.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                pass

            return self._last
