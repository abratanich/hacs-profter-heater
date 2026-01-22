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

HEX_LOG_LIMIT = 256

# Если вообще нет нотификаций (ни 8, ни 52) столько времени — тогда reconnect.
NO_NOTIFY_RECONNECT_SEC = 25.0

# Сколько секунд после CMD_OFF мы готовы считать "idle notify (8 bytes)" подтверждением выключения
IDLE_AFTER_OFF_CONFIRM_SEC = 4.0

# Если во время poll_status мы увидели свежий 8B notify — считаем это "устройство OFF/idle"
# и не продолжаем дожимать 52 (чтобы не тратить время).
IDLE_NOTIFY_FAST_RETURN_SEC = 1.0


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
    def __init__(self, hass, address: str) -> None:
        self._hass = hass
        self._address = address

        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()

        self._last = Parsed()
        self._evt_52 = asyncio.Event()

        self._notify_count = 0
        self._last_notify_len: Optional[int] = None

        # timestamps
        self._last_any_notify_ts: Optional[float] = None
        self._last_52_ts: Optional[float] = None
        self._last_8_ts: Optional[float] = None

    @property
    def last(self) -> Parsed:
        return self._last

    def _now(self) -> float:
        return asyncio.get_running_loop().time()

    def _notification_cb(self, handle: int, data: bytearray) -> None:
        self._notify_count += 1
        b = bytes(data)
        ln = len(b)
        self._last_notify_len = ln

        now = self._now()
        self._last_any_notify_ts = now
        if ln == 52:
            self._last_52_ts = now
        if ln == 8:
            self._last_8_ts = now

        _LOGGER.debug(
            "BLE[%s] NOTIFY #%d handle=%s len=%d hex=%s",
            self._address,
            self._notify_count,
            handle,
            ln,
            _hex(b),
        )

        if ln == 52:
            self._parse_52(b, src="notify")
            self._evt_52.set()
        else:
            # 8 bytes = idle/ack (у тебя это нормально при выключенном)
            _LOGGER.debug(
                "BLE[%s] NOTIFY(non-52) (likely idle/ack) len=%d hex=%s",
                self._address,
                ln,
                _hex(b),
            )

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

    async def _wait_ble_device(self, timeout: float = 10.0):
        end = self._now() + timeout
        attempt = 0
        while self._now() < end:
            attempt += 1
            ble_device = bluetooth.async_ble_device_from_address(
                self._hass, self._address, connectable=True
            )
            if ble_device is not None:
                _LOGGER.debug("BLE[%s] Found ble_device attempt=%d: %s", self._address, attempt, ble_device)
                return ble_device
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
            except Exception:
                pass
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

    async def _try_read_status52(self, c: BleakClient) -> bool:
        # В твоих логах read почти всегда len=0 — оставляем, но не полагаемся.
        try:
            b = await c.read_gatt_char(NOTIFY_CHAR)
            ln = len(b) if isinstance(b, (bytes, bytearray)) else None
            _LOGGER.debug("BLE[%s] READ char=%s len=%s hex=%s", self._address, NOTIFY_CHAR, ln, _hex(b))
            if isinstance(b, (bytes, bytearray)) and len(b) == 52:
                self._parse_52(bytes(b), src="read")
                now = self._now()
                self._last_any_notify_ts = now
                self._last_52_ts = now
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

    async def _poll_for_52(self, c: BleakClient, budget_sec: float) -> bool:
        """
        Пытаемся получить 52B, строго не вылезая за budget_sec.
        Делаем 2 попытки: noresp, затем resp (если осталось время).
        """
        start = self._now()

        async def _try(resp: bool, tag: str, wait_sec: float) -> bool:
            self._evt_52.clear()
            await self._write(c, POLL52, response=resp, tag=tag)
            try:
                await asyncio.wait_for(self._evt_52.wait(), timeout=wait_sec)
                return True
            except asyncio.TimeoutError:
                return False

        # noresp
        left = budget_sec - (self._now() - start)
        if left <= 0:
            return False
        if await _try(False, "POLL52(noresp)", wait_sec=min(0.9, left)):
            return True

        # resp
        left = budget_sec - (self._now() - start)
        if left <= 0:
            return False
        return await _try(True, "POLL52(resp)", wait_sec=min(0.9, left))

    def _recent_idle_notify(self, since_ts: float, window_sec: float) -> bool:
        return bool(self._last_8_ts is not None and (self._last_8_ts >= since_ts) and (self._now() - self._last_8_ts) <= window_sec)

    async def poll_status(self, timeout: float = 6.0) -> Parsed:
        async with self._lock:
            t0 = self._now()
            deadline = t0 + timeout
            poll_started = t0

            c = await self._ensure()

            def _ago(ts: Optional[float]) -> float:
                return (t0 - ts) if ts else -1.0

            _LOGGER.debug(
                "BLE[%s] poll_status() begin timeout=%.2f last(on=%s room=%s heater=%s) "
                "last_any=%.1fs last_52=%.1fs last_8=%.1fs",
                self._address,
                timeout,
                self._last.is_on,
                self._last.room_c,
                self._last.heater_c,
                _ago(self._last_any_notify_ts),
                _ago(self._last_52_ts),
                _ago(self._last_8_ts),
            )

            # 1) Быстрый read (редко помогает, но дешево)
            if self._now() < deadline:
                if await self._try_read_status52(c):
                    _LOGGER.debug("BLE[%s] poll_status() got 52 via READ in %.3fs", self._address, self._now() - t0)
                    return self._last

            # 2) POLL с жёстким бюджетом времени
            for attempt in range(1, 4):
                now = self._now()
                remaining = deadline - now
                if remaining <= 0:
                    break

                # Если уже прилетал 8B в рамках этого poll — считаем OFF/idle и выходим быстро
                if self._recent_idle_notify(since_ts=poll_started, window_sec=IDLE_NOTIFY_FAST_RETURN_SEC):
                    _LOGGER.debug(
                        "BLE[%s] poll_status() saw fresh idle(8B) notify during poll -> return last (attempt=%d, elapsed=%.3fs)",
                        self._address,
                        attempt,
                        now - t0,
                    )
                    return self._last

                budget = min(1.8, remaining)  # на одну попытку не тратим больше ~1.8с
                got52 = False
                try:
                    got52 = await self._poll_for_52(c, budget_sec=budget)
                except Exception as e:
                    _LOGGER.debug(
                        "BLE[%s] poll_status() POLL attempt=%d error=%s -> reconnect",
                        self._address,
                        attempt,
                        e,
                    )
                    await self.disconnect()
                    c = await self._ensure()
                    continue

                if got52:
                    _LOGGER.debug(
                        "BLE[%s] poll_status() got 52 attempt=%d in %.3fs",
                        self._address,
                        attempt,
                        self._now() - t0,
                    )
                    return self._last

                # Если во время этой попытки прилетели 8B — не мучаем дальше
                if self._recent_idle_notify(since_ts=poll_started, window_sec=IDLE_NOTIFY_FAST_RETURN_SEC):
                    _LOGGER.debug(
                        "BLE[%s] poll_status() got idle(8B) after POLL attempt=%d -> return last",
                        self._address,
                        attempt,
                    )
                    return self._last

                # optional: read, но только если ещё есть время
                if self._now() < deadline:
                    if await self._try_read_status52(c):
                        _LOGGER.debug("BLE[%s] poll_status() got 52 via READ(after POLL) in %.3fs", self._address, self._now() - t0)
                        return self._last

                _LOGGER.debug("BLE[%s] poll_status() no 52 attempt=%d (elapsed=%.3fs)", self._address, attempt, self._now() - t0)

                # короткая пауза, но строго по бюджету
                if self._now() + 0.15 < deadline:
                    await asyncio.sleep(0.15)

            # 3) reconnect только если реально “тишина” по notify слишком долго
            now = self._now()
            if self._last_any_notify_ts is not None:
                silent_for = now - self._last_any_notify_ts
                if silent_for > NO_NOTIFY_RECONNECT_SEC:
                    _LOGGER.warning("BLE[%s] No any notify for %.1fs -> reconnect", self._address, silent_for)
                    await self.disconnect()
                    await self._ensure()
            else:
                # если вообще никогда не было notify — не дергаем reconnect тут,
                # это может быть первый цикл сразу после старта
                pass

            _LOGGER.debug("BLE[%s] poll_status() end NO-52 in %.3fs -> return last", self._address, self._now() - t0)
            return self._last

    async def set_on(self, on: bool, timeout: float = 6.0) -> Parsed:
        async with self._lock:
            t0 = self._now()
            deadline = t0 + timeout
            cmd = CMD_ON if on else CMD_OFF

            c = await self._ensure()

            _LOGGER.debug("BLE[%s] set_on(%s) begin timeout=%.2f", self._address, on, timeout)

            try:
                await self._write(c, cmd, response=True, tag="CMD")
            except Exception as e:
                _LOGGER.warning("BLE[%s] set_on(%s) CMD failed: %s -> reconnect+retry", self._address, on, e)
                await self.disconnect()
                c = await self._ensure()
                await self._write(c, cmd, response=True, tag="CMD(retry)")

            # маленькая пауза, но не вылезаем за deadline
            if self._now() + 0.25 < deadline:
                await asyncio.sleep(0.25)

            while self._now() < deadline:
                # если уже совпало — выходим
                if self._last.is_on is not None and self._last.is_on == on:
                    _LOGGER.debug("BLE[%s] set_on(%s) already matched state", self._address, on)
                    return self._last

                # особый кейс: после выключения устройство может перейти в idle и слать только 8 байт
                if on is False and self._last_8_ts and (self._now() - self._last_8_ts) <= IDLE_AFTER_OFF_CONFIRM_SEC:
                    _LOGGER.debug(
                        "BLE[%s] set_on(False) treating recent idle(8B) notify as confirmation (last8=%.2fs ago)",
                        self._address,
                        self._now() - self._last_8_ts,
                    )
                    self._last.is_on = False
                    return self._last

                remaining = deadline - self._now()
                if remaining <= 0:
                    break

                # POLL на остаток бюджета (не больше 1.2с за итерацию)
                budget = min(1.2, remaining)
                try:
                    ok = await self._poll_for_52(c, budget_sec=budget)
                    if ok and self._last.is_on is not None and self._last.is_on == on:
                        _LOGGER.debug("BLE[%s] set_on(%s) confirmed via 52 in %.3fs", self._address, on, self._now() - t0)
                        return self._last
                except Exception as e:
                    _LOGGER.debug("BLE[%s] set_on(%s) poll error: %s -> reconnect", self._address, on, e)
                    await self.disconnect()
                    c = await self._ensure()

                # read только если успеваем
                if self._now() < deadline:
                    if await self._try_read_status52(c):
                        if self._last.is_on is not None and self._last.is_on == on:
                            _LOGGER.debug("BLE[%s] set_on(%s) confirmed via READ in %.3fs", self._address, on, self._now() - t0)
                            return self._last

                # короткая пауза по бюджету
                if self._now() + 0.15 < deadline:
                    await asyncio.sleep(0.15)

            _LOGGER.warning(
                "BLE[%s] set_on(%s) NOT CONFIRMED in %.3fs -> optimistic is_on=%s (last was %s)",
                self._address,
                on,
                self._now() - t0,
                on,
                self._last.is_on,
            )
            self._last.is_on = on
            return self._last