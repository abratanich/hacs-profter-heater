from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN, CONF_ADDRESS, CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
from .ble import async_can_connect


class ProfterHeaterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        errors = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip()
            poll = int(user_input[CONF_POLL_INTERVAL])

            ok, reason = await async_can_connect(self.hass, address)
            if not ok:
                errors["base"] = reason or "cannot_connect"
            else:
                await self.async_set_unique_id(address.lower())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Profter Heater ({address})",
                    data={CONF_ADDRESS: address, CONF_POLL_INTERVAL: poll},
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS, default=""): str,
                vol.Optional(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): vol.Coerce(int),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
