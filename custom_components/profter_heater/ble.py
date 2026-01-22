from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass
from typing import Optional, Tuple

from bleak import BleakClient
from bleak_retry_connector import establish_connection, BleakNotFoundError

from homeassistant.components import bluetooth

from .const import WRITE_CHAR, NOTIFY_CHAR, CMD_ON, CMD_OFF, POLL52, DOMAIN


@dataclass
class Parsed:
    is_on: Optional[bool] = None
    room_c: Optional[float] = None
    heater_c: Optional[float] = None
    raw52: Optional[bytes] = None


def parse_onoff_from_status52(p: bytes) -> Optional[bool]:
    """Ищем маркер A5 05 ?? ?? b1 b2 и трактуем (b1,b2).
    В твоих кадрах встречалось:
      ... A5 05 09 14 01 73 ... -> ON
      ... A5 05 09 14 02 EF ... -> OFF
      ... A5 05 09 15 01 73 ... -> ON
      ... A5 05 09 15 02 EF ... -> OFF
    """
    if len(p) != 52:
        return None

    # ищем A5 05, дальше читаем b1,b2 по +4,+5 (как у тебя было)
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
    """Температуры (твой рабочий вариант):
      room  = int16 LE @ 14 / 10
      heater= int16 LE @ 16 / 10
    """
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

        client = await establish_connection(BleakClient, ble_device, address, max_attempts=2)
        await client.disconnect()
        return True, None
    except BleakNotFoundError:
        return False, "not_found"
    except Exception:
        return False, "cannot_connect"


class ProfterHeaterBLE:
    """BLE транспорт с короткими сессиями: connect->notify->write->wait->disconnect.

    Это наиболее устойчиво для устройств, которые:
      - не пушат статус сами
      - "залипают" при долгих соединениях
      - плохо отрабатывают write response=True
    """

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

    async def _wait_ble_device(self, timeout: float = 10.0):
        """Ждём пока HA увидит connectable BLEDevice по адресу (иногда бывает None)."""
        end = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < end:
            ble_device = bluetooth.async_ble_device_from_address(self._hass, self._address, connectable=True)
            if ble_device is not None:
                return ble_device
            await asyncio.sleep(0.5)
        raise BleakNotFoundError(f"{DOMAIN}: Device not found (no adv): {self._address}")

    async def _with_client(self):
        """Контекст короткой сессии."""
        ble_device = await self._wait_ble_device(timeout=10.0)
        client = await establish_connection(
            BleakClient,
            ble_device,
            self._address,
            max_attempts=3,
        )
        return client

    async def disconnect(self) -> None:
        """Для совместимости с coordinator.async_shutdown().
        В коротких сессиях держать глобальное соединение не нужно.
        """
        return

    async def _poll_once(self, client: BleakClient, evt: asyncio.Event, timeout: float) -> bool:
        """Отправить POLL и дождаться notify."""
        evt.clear()
        # ВАЖНО: response=False для POLL (много устройств ломается на response=True)
        await client.write_gatt_char(WRITE_CHAR, POLL52, response=False)
        await asyncio.sleep(0.05)  # маленькая пауза помогает BLE-стекам
        await asyncio.wait_for(evt.wait(), timeout=timeout)
        return True

    async def poll_status(self, timeout: float = 6.0) -> Parsed:
        """Периодический опрос из coordinator."""
        async with self._lock:
            evt = asyncio.Event()

            def cb(_handle: int, data: bytearray) -> None:
                b = bytes(data)
                if len(b) == 52:
                    self._parse_52(b)
                    evt.set()

            client: BleakClient | None = None
            try:
                client = await self._with_client()
                await client.start_notify(NOTIFY_CHAR, cb)

                # 1) пробуем получить кадр несколькими попытками
                slice_t = max(0.7, timeout / 4)

                # Первый POLL сразу
                for _ in range(4):
                    try:
                        await self._poll_once(client, evt, timeout=slice_t)
                        return self._last
                    except asyncio.TimeoutError:
                        # если не ответил — повторяем
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
        """Команда ON/OFF + подтверждение через статус."""
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
                client = await self._with_client()
                await client.start_notify(NOTIFY_CHAR, cb)

                # 1) отправляем команду (лучше response=False)
                await client.write_gatt_char(WRITE_CHAR, cmd, response=False)
                await asyncio.sleep(0.35)

                # 2) добиваемся нужного статуса poll'ами до timeout
                deadline = asyncio.get_running_loop().time() + timeout
                last = self._last

                while asyncio.get_running_loop().time() < deadline:
                    evt.clear()
                    await client.write_gatt_char(WRITE_CHAR, POLL52, response=False)

                    try:
                        await asyncio.wait_for(evt.wait(), timeout=0.8)
                    except asyncio.TimeoutError:
                        await asyncio.sleep(0.25)
                        continue

                    last = self._last
                    if last.is_on is not None and last.is_on == on:
                        return last

                    await asyncio.sleep(0.25)

                return last

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