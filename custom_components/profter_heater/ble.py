# ble.py
from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass
from typing import Optional, Tuple

from bleak import BleakClient
from bleak_retry_connector import BleakNotFoundError, establish_connection
from homeassistant.components import bluetooth

from .const import CMD_OFF, CMD_ON, DOMAIN, NOTIFY_CHAR, POLL52, WRITE_CHAR

_LOGGER = logging.getLogger(__name__)

# Сколько подряд poll-циклов (каждый цикл может включать 2-3 попытки),
# в которых мы не получили ни одного пакета len=52, прежде чем делать reconnect.
NO52_RECONNECT_THRESHOLD = 2

# Максимум байт, которые печатаем в hex в логах
HEX_LOG_LIMIT = 256


@dataclass
class Parsed:
    is_on: Optional[bool] = None
    room_c: Optional[float] = None
    heater_c: Optional[float] = None
    raw52: Optional[bytes] = None


def _hex(b: bytes | bytearray | None, limit: int = HEX_LOG_LIMIT) -> str:
    if not b:
        return ""
    bb = bytes(b)
    if len(bb) > limit:
        return bb[:limit].hex().upper() + f"...(+{len(bb) - limit} bytes)"
    return bb.hex().upper()


def _u16le(p: bytes, off: int) -> int:
    return struct.unpack_from("<H", p, off)[0]


def _s16le(p: bytes, off: int) -> int:
    return struct.unpack_from("<h", p, off)[0]


def parse_onoff_from_status52(p: bytes) -> Optional[bool]:
    """Ищем маркер A5 05 ?? ?? b1 b2 .. (варианты хвоста разные).
    True  -> (b1,b2) == (01,73)
    False -> (b1,b2) == (02,EF)
    """
    if len(p) != 52:
        return None

    for i in range(0, len(p) - 6):
        if p[i] == 0xA5 and p[i + 1] == 0x05:
            b1 = p[i + 4]
            b2 = p[i + 5]
            if (b1, b2) == (0x01, 0x73):
                return True
            if (b1, b2) == (0x02, 0xEF):
                return False
    return None


def parse_temps_best_effort(p: bytes) -> Tuple[Optional[float], Optional[float]]:
    """Температуры:
    room  -> int16 LE @ 14, scale 0.1°C
    heater-> int16 LE @ 16, scale 0.1°C
    """
    if len(p) != 52:
        return (None, None)

    try:
        room = _s16le(p, 14) / 10.0
        heater = _s16le(p, 16) / 10.0
    except struct.error:
        return (None, None)

    if not (-40.0 <= room <= 80.0):
        room = None
    if not (-40.0 <= heater <= 250.0):
        heater = None

    return (room, heater)


async def async_can_connect(hass, address: str) -> tuple[bool, Optional[str]]:
    """Пробная проверка подключения (используется в config_flow)."""
    try:
        ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
        if ble_device is None:
            return False, "not_found"

        client = await establish_connection(BleakClient, ble_device, address, max_attempts=2)
        try:
            await client.disconnect()
        except Exception:
            pass
        return True, None

    except BleakNotFoundError:
        return False, "not_found"
    except Exception:
        return False, "cannot_connect"


class ProfterHeaterBLE:
    """BLE транспорт с подробным логированием и автоворотами (reconnect) при "ACK-only" режиме."""

    def __init__(self, hass, address: str) -> None:
        self._hass = hass
        self._address = address

        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()

        self._last = Parsed()
        self._evt_52 = asyncio.Event()

        self._notify_count = 0
        self._last_notify_len: Optional[int] = None

        # watchdog
        self._no52_cycles = 0
        self._last_good52_monotonic: Optional[float] = None

    @property
    def last(self) -> Parsed:
        return self._last

    # ---- notify ----
    def _notification_cb(self, handle: int, data: bytearray) -> None:
        self._notify_count += 1
        b = bytes(data)
        self._last_notify_len = len(b)

        _LOGGER.debug(
            "BLE[%s] NOTIFY #%d handle=%s len=%d hex=%s",
            self._address,
            self._notify_count,
            handle,
            len(b),
            _hex(b),
        )

        if len(b) == 52:
            self._parse_52(b, src="notify")
            self._last_good52_monotonic = asyncio.get_running_loop().time()
            self._evt_52.set()

    def _parse_52(self, b: bytes, src: str) -> None:
        self._last.raw52 = b
        self._last.is_on = parse_onoff_from_status52(b)
        self._last.room_c, self._last.heater_c = parse_temps_best_effort(b)

        try:
            w14 = _u16le(b, 14)
            w16 = _u16le(b, 16)
        except Exception:
            w14 = None
            w16 = None

        _LOGGER.debug(
            "BLE[%s] PARSE52(%s) on=%s room=%s heater=%s off14_u16=%s off16_u16=%s tail=%s",
            self._address,
            src,
            self._last.is_on,
            self._last.room_c,
            self._last.heater_c,
            w14,
            w16,
            _hex(b[-16:]),
        )

    # ---- discovery / connect ----
    async def _wait_ble_device(self, timeout: float = 10.0):
        end = asyncio.get_running_loop().time() + timeout
        attempt = 0

        while asyncio.get_running_loop().time() < end:
            attempt += 1
            ble_device = bluetooth.async_ble_device_from_address(
                self._hass, self._address, connectable=True
            )
            if ble_device is not None:
                _LOGGER.debug("BLE[%s] Found ble_device attempt=%d: %s", self._address, attempt, ble_device)
                return ble_device

            _LOGGER.debug("BLE[%s] Waiting adv... attempt=%d", self._address, attempt)
            await asyncio.sleep(0.5)

        raise BleakNotFoundError(f"{DOMAIN}: Device not found (no adv): {self._address}")

    async def connect(self) -> None:
        ble_device = await self._wait_ble_device(timeout=10.0)

        _LOGGER.debug("BLE[%s] Connecting...", self._address)
        self._client = await establish_connection(
            BleakClient,
            ble_device,
            self._address,
            max_attempts=3,
        )
        _LOGGER.debug("BLE[%s] Connected: is_connected=%s", self._address, self._client.is_connected)

        try:
            await self._client.start_notify(NOTIFY_CHAR, self._notification_cb)
            _LOGGER.debug("BLE[%s] start_notify OK char=%s", self._address, NOTIFY_CHAR)
        except Exception as e:
            _LOGGER.warning("BLE[%s] start_notify FAILED char=%s err=%s", self._address, NOTIFY_CHAR, e)

        self._evt_52.clear()

    async def disconnect(self) -> None:
        if not self._client:
            return

        _LOGGER.debug("BLE[%s] Disconnecting...", self._address)
        try:
            try:
                await self._client.stop_notify(NOTIFY_CHAR)
                _LOGGER.debug("BLE[%s] stop_notify OK", self._address)
            except Exception as e:
                _LOGGER.debug("BLE[%s] stop_notify ignored: %s", self._address, e)

            await self._client.disconnect()
            _LOGGER.debug("BLE[%s] Disconnected", self._address)
        except Exception as e:
            _LOGGER.debug("BLE[%s] Disconnect exception: %s", self._address, e)
        finally:
            self._client = None

    async def _ensure(self) -> BleakClient:
        if self._client and self._client.is_connected:
            return self._client

        _LOGGER.debug("BLE[%s] _ensure() reconnect needed", self._address)
        await self.disconnect()
        await self.connect()
        assert self._client is not None
        return self._client

    # ---- low-level ops ----
    async def _try_read_status52(self, c: BleakClient) -> bool:
        """Пробуем прочитать 52 байта через READ."""
        try:
            b = await c.read_gatt_char(NOTIFY_CHAR)
            ln = len(b) if isinstance(b, (bytes, bytearray)) else None
            _LOGGER.debug("BLE[%s] READ char=%s len=%s hex=%s", self._address, NOTIFY_CHAR, ln, _hex(b))
            if isinstance(b, (bytes, bytearray)) and len(b) == 52:
                self._parse_52(bytes(b), src="read")
                self._last_good52_monotonic = asyncio.get_running_loop().time()
                return True
        except Exception as e:
            _LOGGER.debug("BLE[%s] READ failed: %s", self._address, e)
        return False

    async def _write(self, c: BleakClient, payload: bytes, response: bool, tag: str) -> None:
        _LOGGER.debug(
            "BLE[%s] WRITE(%s) char=%s response=%s len=%d hex=%s",
            self._address,
            tag,
            WRITE_CHAR,
            response,
            len(payload),
            _hex(payload),
        )
        await c.write_gatt_char(WRITE_CHAR, payload, response=response)

    async def _poll_once(self, c: BleakClient, per_wait: float) -> bool:
        """Одна попытка POLL: пробуем response=False, если не пришёл 52 — response=True."""
        # 1) write without response
        self._evt_52.clear()
        await self._write(c, POLL52, response=False, tag="POLL52(noresp)")
        try:
            await asyncio.wait_for(self._evt_52.wait(), timeout=per_wait)
            return True
        except asyncio.TimeoutError:
            pass

        # 2) write with response
        self._evt_52.clear()
        await self._write(c, POLL52, response=True, tag="POLL52(resp)")
        try:
            await asyncio.wait_for(self._evt_52.wait(), timeout=per_wait)
            return True
        except asyncio.TimeoutError:
            return False

    # ---- public API ----
    async def poll_status(self, timeout: float = 6.0) -> Parsed:
        """Запросить статус. Если устройство ушло в "ACK-only" режим — переподключаемся."""
        async with self._lock:
            t0 = asyncio.get_running_loop().time()
            c = await self._ensure()

            _LOGGER.debug(
                "BLE[%s] poll_status() begin timeout=%.2f last(on=%s room=%s heater=%s) notify_count=%d last_notify_len=%s no52_cycles=%d",
                self._address,
                timeout,
                self._last.is_on,
                self._last.room_c,
                self._last.heater_c,
                self._notify_count,
                self._last_notify_len,
                self._no52_cycles,
            )

            # 1) READ (часто бывает пусто, но оставим)
            if await self._try_read_status52(c):
                self._no52_cycles = 0
                _LOGGER.debug("BLE[%s] poll_status() got via READ in %.3fs", self._address, asyncio.get_running_loop().time() - t0)
                return self._last

            # 2) POLL + ждём 52
            per_wait = max(0.7, timeout / 3)

            got52 = False
            for attempt in range(1, 4):
                try:
                    got52 = await self._poll_once(c, per_wait=per_wait)
                except Exception as e:
                    _LOGGER.debug("BLE[%s] poll_status() POLL attempt=%d error=%s -> reconnect", self._address, attempt, e)
                    await self.disconnect()
                    c = await self._ensure()
                    continue

                if got52:
                    self._no52_cycles = 0
                    _LOGGER.debug("BLE[%s] poll_status() got 52 via NOTIFY attempt=%d in %.3fs", self._address, attempt, asyncio.get_running_loop().time() - t0)
                    return self._last

                # иногда после poll можно прочитать
                if await self._try_read_status52(c):
                    self._no52_cycles = 0
                    _LOGGER.debug("BLE[%s] poll_status() got via READ(after POLL) attempt=%d", self._address, attempt)
                    return self._last

                _LOGGER.debug("BLE[%s] poll_status() no 52 attempt=%d", self._address, attempt)
                await asyncio.sleep(0.2)

            # не получили 52
            self._no52_cycles += 1
            _LOGGER.debug(
                "BLE[%s] poll_status() end NO-52 in %.3fs -> no52_cycles=%d (return last)",
                self._address,
                asyncio.get_running_loop().time() - t0,
                self._no52_cycles,
            )

            # watchdog: переподключаемся, если устройство системно не даёт 52
            if self._no52_cycles >= NO52_RECONNECT_THRESHOLD:
                _LOGGER.warning(
                    "BLE[%s] NO-52 watchdog triggered (no52_cycles=%d). Reconnecting...",
                    self._address,
                    self._no52_cycles,
                )
                self._no52_cycles = 0
                await self.disconnect()
                await self._ensure()

            return self._last

    async def set_on(self, on: bool, timeout: float = 6.0) -> Parsed:
        """Включить/выключить:
        - CMD (response=True)
        - ждём 52, если не пришло — пытаемся POLL (оба write-mode) и READ
        - если не подтвердили — optimistic is_on=on
        """
        async with self._lock:
            t0 = asyncio.get_running_loop().time()
            c = await self._ensure()
            cmd = CMD_ON if on else CMD_OFF

            _LOGGER.debug("BLE[%s] set_on(%s) begin timeout=%.2f", self._address, on, timeout)

            # 1) отправляем команду
            try:
                await self._write(c, cmd, response=True, tag="CMD")
            except Exception as e:
                _LOGGER.warning("BLE[%s] set_on(%s) CMD write failed: %s -> reconnect+retry", self._address, on, e)
                await self.disconnect()
                c = await self._ensure()
                await self._write(c, cmd, response=True, tag="CMD(retry)")

            await asyncio.sleep(0.25)

            # 2) ждём подтверждение
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                if self._last.is_on is not None and self._last.is_on == on:
                    _LOGGER.debug("BLE[%s] set_on(%s) already matched state", self._address, on)
                    return self._last

                # poll (оба режима)
                try:
                    ok = await self._poll_once(c, per_wait=0.9)
                    if ok and self._last.is_on is not None and self._last.is_on == on:
                        _LOGGER.debug("BLE[%s] set_on(%s) confirmed via NOTIFY in %.3fs", self._address, on, asyncio.get_running_loop().time() - t0)
                        return self._last
                except Exception as e:
                    _LOGGER.debug("BLE[%s] set_on(%s) POLL failed: %s -> reconnect", self._address, on, e)
                    await self.disconnect()
                    c = await self._ensure()

                # read fallback
                if await self._try_read_status52(c):
                    if self._last.is_on is not None and self._last.is_on == on:
                        _LOGGER.debug("BLE[%s] set_on(%s) confirmed via READ in %.3fs", self._address, on, asyncio.get_running_loop().time() - t0)
                        return self._last

                await asyncio.sleep(0.2)

            # 3) optimistic
            if self._last.is_on != on:
                _LOGGER.warning(
                    "BLE[%s] set_on(%s) NOT CONFIRMED in %.3fs -> optimistic is_on=%s (last was %s)",
                    self._address,
                    on,
                    asyncio.get_running_loop().time() - t0,
                    on,
                    self._last.is_on,
                )
                self._last.is_on = on

            return self._last