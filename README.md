# Profter Heater (BLE) — Home Assistant (HACS)

Кастомная интеграция для управления дизельной печкой (контроллер Profter) по BLE.

## Что умеет
- Переключатель: ON/OFF
- Сенсоры:
  - State (ON/OFF/UNKNOWN)
  - Raw Status 52 (диагностика, последний 52-байтный фрейм)
  - Room/Heater temperature (по умолчанию выключены — пока не подтверждены оффсеты)

## Требования
- Home Assistant должен иметь доступ к Bluetooth адаптеру (BlueZ/DBus на Linux).
- Если HA запущен там, где нет BLE (например, в контейнере на удалённой машине) — делайте bridge (BLE→MQTT/HTTP).

## Установка через HACS
1. Создай GitHub репозиторий из этого архива.
2. HACS → Integrations → ⋮ → Custom repositories → добавь репозиторий как **Integration**.
3. Установи и перезапусти Home Assistant.
4. Settings → Devices & services → Add integration → **Profter Heater (BLE)**.

## Примечание по температурам
В твоих логах были значения, но формат/оффсеты ещё не подтверждены стабильно для всех кадров.
Чтобы не публиковать мусор — в интеграции парсер температур пока возвращает `None`.
0.2.18