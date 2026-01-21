from __future__ import annotations

DOMAIN = "profter_heater"

CONF_ADDRESS = "address"
CONF_POLL_INTERVAL = "poll_interval"

DEFAULT_POLL_INTERVAL = 10  # seconds

# GATT UUIDs
WRITE_CHAR  = "00003a01-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR = "00003a00-0000-1000-8000-00805f9b34fb"

# Commands (8 bytes)
CMD_ON  = bytes.fromhex("AA00610173002276")
CMD_OFF = bytes.fromhex("AA006102EF0026A0")

# Poll frame (52 bytes) - forces status notifications
POLL52 = bytes.fromhex(
    "AA09FF19000000F25520840020030101DB00000000000000F87FF87F00000440156EFBBC0000A505061E022D000000000F3E359F"
)
