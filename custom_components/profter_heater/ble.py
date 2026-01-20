import asyncio
from dataclasses import dataclass
from typing import Optional, Tuple

from bleak import BleakClient, BleakScanner

from .const import WRITE_CHAR, NOTIFY_CHAR, CMD_ON, CMD_OFF, POLL52

# (опционально) keepalive из твоего snoop — если подтвердилось, что помогает
KEEPALIVE_A = bytes.fromhex("AA0065020A000316")
KEEPALIVE_B = bytes.fromhex("AA006502001E1F22")


@dataclass
class Parsed:
    is_on: Optional[bool] = None
    room_c: Optional[float] = None
    heater_c: Optional[float] = None
    raw52: Optional[bytes] = None


def _find_sync_index(p: bytes) -> int:
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
    return (None, None)


async def _find_device(address: str, timeout: float = 8.0):
    dev = await BleakScanner.find_device_by_address(address, timeout=timeout)
    if dev:
        return dev
    for d in await BleakScanner.discover(timeout=timeout):
        if (d.address or "").lower() == address.lower():
            return d
    return None


class ProfterHeaterBLE:
    def __init__(self, address: str):
        self._address = address
        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._last = Parsed()
        self._got_evt = asyncio.Event()
        self._notify_started = False

    @property
    def last(self) -> Parsed:
        return self._last

    def _notify_cb(self, _handle: int, data: bytearray):
        b = bytes(data)
        if len(b) != 52:
            return
        self._last.raw52 = b
        self._last.is_on = parse_onoff_from_status52(b)
        self._last.room_c, self._last.heater_c = parse_temps_best_effort(b)
        self._got_evt.set()

    async def connect(self) -> None:
        dev = await _find_device(self._address, timeout=12.0)
        if not dev:
            raise RuntimeError("Device not found")

        self._client = BleakClient(dev)
        await self._client.connect()
        if not self._client.is_connected:
            raise RuntimeError("BLE connect failed")

        # важный момент: подписку держим и умеем перевключать
        await self._client.start_notify(NOTIFY_CHAR, self._notify_cb)
        self._notify_started = True

    async def disconnect(self) -> None:
        if not self._client:
            return
        try:
            if self._notify_started:
                await self._client.stop_notify(NOTIFY_CHAR)
        except Exception:
            pass
        try:
            await self._client.disconnect()
        except Exception:
            pass
        self._client = None
        self._notify_started = False

    async def _ensure(self) -> BleakClient:
        if self._client and self._client.is_connected:
            return self._client
        await self.disconnect()
        await self.connect()
        assert self._client is not None
        return self._client

    async def _resubscribe_notify(self) -> None:
        """BlueZ иногда теряет notify — делаем гарантированное перевключение."""
        c = await self._ensure()
        try:
            await c.stop_notify(NOTIFY_CHAR)
        except Exception:
            pass
        await c.start_notify(NOTIFY_CHAR, self._notify_cb)
        self._notify_started = True

    async def _write8(self, payload: bytes) -> None:
        c = await self._ensure()
        await c.write_gatt_char(WRITE_CHAR, payload, response=True)

    async def _poll52_once(self, timeout: float) -> Parsed:
        c = await self._ensure()
        self._got_evt.clear()
        await c.write_gatt_char(WRITE_CHAR, POLL52, response=True)
        try:
            await asyncio.wait_for(self._got_evt.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        return self._last

    async def poll_status(self, timeout: float = 4.0) -> Parsed:
        async with self._lock:
            await self._ensure()

            # 1) перевключить notify (лечит "подписка есть, данных нет")
            await self._resubscribe_notify()

            # 2) попытка poll
            p = await self._poll52_once(timeout=timeout)
            if p.raw52:
                return p

            # 3) fallback: keepalive + повтор poll (лечит "устройство спит")
            await self._write8(KEEPALIVE_A)
            await asyncio.sleep(0.2)
            await self._write8(KEEPALIVE_B)
            await asyncio.sleep(0.2)

            await self._resubscribe_notify()
            return await self._poll52_once(timeout=timeout)

    async def set_on(self, on: bool, timeout: float = 5.0) -> Parsed:
        async with self._lock:
            await self._ensure()
            await self._resubscribe_notify()

            cmd = CMD_ON if on else CMD_OFF
            self._got_evt.clear()
            await self._client.write_gatt_char(WRITE_CHAR, cmd, response=True)  # type: ignore[union-attr]
            await asyncio.sleep(0.25)

            # после команды — обязательно poll
            p = await self._poll52_once(timeout=timeout)
            if p.raw52:
                return p

            # fallback keepalive + poll
            await self._write8(KEEPALIVE_A)
            await asyncio.sleep(0.2)
            await self._write8(KEEPALIVE_B)
            await asyncio.sleep(0.2)
            await self._resubscribe_notify()
            return await self._poll52_once(timeout=timeout)

    async def async_can_connect(_hass, address: str) -> tuple[bool, str | None]:
        """Проверка на этапе config_flow: видим ли девайс и можем ли подключиться."""
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