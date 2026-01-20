from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Tuple

from bleak import BleakClient

from homeassistant.components import bluetooth
from bleak_retry_connector import (
    BleakError,
    establish_connection,
    close_stale_connections,
)

from .const import WRITE_CHAR, NOTIFY_CHAR, CMD_ON, CMD_OFF, POLL52

_LOGGER = logging.getLogger(__name__)


@dataclass
class Parsed:
    is_on: Optional[bool] = None
    room_c: Optional[float] = None
    heater_c: Optional[float] = None
    raw52: Optional[bytes] = None


def _find_sync_index(p: bytes) -> int:
    # Ищем паттерн A5 05 ?? 1E
    for i in range(0, len(p) - 6):
        if p[i] == 0xA5 and p[i + 1] == 0x05 and p[i + 3] == 0x1E:
            return i
    return -1


def parse_onoff_from_status52(p: bytes) -> Optional[bool]:
    if len(p) != 52:
        return None
    i = _find_sync_index(p)
    if i < 0:
        return None
    b1, b2 = p[i + 4], p[i + 5]
    if (b1, b2) == (0x01, 0x73):
        return True
    if (b1, b2) == (0x02, 0xEF):
        return False
    return None


def parse_temps_best_effort(_p: bytes) -> Tuple[Optional[float], Optional[float]]:
    # Пока не публикуем, чтобы не врать.
    return (None, None)


async def _get_ble_device(hass, address: str):
    """Get BLEDevice from HA bluetooth stack by MAC address."""
    # address must be MAC on Linux/HA
    dev = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    if dev:
        return dev

    # fallback: try known devices list
    for info in bluetooth.async_discovered_service_info(hass):
        if (info.device.address or "").lower() == address.lower():
            return info.device

    return None


async def async_can_connect(hass, address: str) -> tuple[bool, Optional[str]]:
    try:
        ble_device = await _get_ble_device(hass, address)
        if not ble_device:
            return False, "not_found"

        await close_stale_connections(ble_device)

        client = BleakClient(ble_device)
        client = await establish_connection(
            client,
            ble_device,
            address,
            timeout=10,
        )
        try:
            if not client.is_connected:
                return False, "cannot_connect"
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

        return True, None
    except Exception as e:
        _LOGGER.debug("async_can_connect failed: %r", e)
        return False, "cannot_connect"


class ProfterHeaterBLE:
    def __init__(self, hass, address: str):
        self._hass = hass
        self._address = address

        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()

        self._last = Parsed()
        self._got_evt = asyncio.Event()

    @property
    def last(self) -> Parsed:
        return self._last

    async def connect(self) -> None:
        ble_device = await _get_ble_device(self._hass, self._address)
        if not ble_device:
            raise RuntimeError("Device not found")

        await close_stale_connections(ble_device)

        client = BleakClient(ble_device)
        try:
            self._client = await establish_connection(
                client,
                ble_device,
                self._address,
                timeout=12,
            )
        except BleakError as e:
            self._client = None
            raise RuntimeError(f"BLE connect failed: {e}") from e

        if not self._client or not self._client.is_connected:
            self._client = None
            raise RuntimeError("BLE connect failed")

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
            try:
                await self._client.stop_notify(NOTIFY_CHAR)
            except Exception:
                pass
            await self._client.disconnect()
        finally:
            self._client = None

    async def _ensure(self) -> BleakClient:
        if self._client and self._client.is_connected:
            return self._client
        await self.disconnect()
        await self.connect()
        assert self._client is not None
        return self._client

    async def _poll_once(self, c: BleakClient, timeout: float) -> bool:
        self._got_evt.clear()
        await c.write_gatt_char(WRITE_CHAR, POLL52, response=True)
        try:
            await asyncio.wait_for(self._got_evt.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            # HA отменил refresh — не считаем это ошибкой
            return False

    async def poll_status(self, timeout: float = 6.0) -> Parsed:
        async with self._lock:
            c = await self._ensure()

            # READ как бонус
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

            # POLL 2-3 раза с нормальным ожиданием
            for attempt in range(3):
                try:
                    ok = await self._poll_once(c, timeout=timeout)
                    if ok:
                        return self._last
                except Exception:
                    # на ошибке — переподключаемся и повторяем
                    await self.disconnect()
                    c = await self._ensure()

                await asyncio.sleep(0.25)

            return self._last

    async def set_on(self, on: bool, timeout: float = 8.0) -> Parsed:
        async with self._lock:
            c = await self._ensure()

            cmd = CMD_ON if on else CMD_OFF
            try:
                await c.write_gatt_char(WRITE_CHAR, cmd, response=True)
            except Exception:
                await self.disconnect()
                c = await self._ensure()
                await c.write_gatt_char(WRITE_CHAR, cmd, response=True)

            await asyncio.sleep(0.25)
            await self.poll_status(timeout=timeout)
            return self._last