"""Adopt the integration token the hub renewed while the entry was set up.

The hub mints a new integration token on every start and announces it
through Supervisor discovery; the old token stops working once the announce
succeeded. HA applies a re-discovery to the entry, but when Core and the
hub (re)start at the same time (host reboot, update) the entry can be set
up with the old token in between: the hub rejects it before HA has
processed the discovery, and HA does not reload an entry that failed with an
authentication error.

So before the coordinator gives up with a reauth flow it looks for a newer
token itself: one written to the entry in the meantime (a re-discovery during
the setup), or the one the hub announced to the Supervisor. The token is
only taken over for the host and port the entry already uses, so it is never
sent anywhere else. If the hub rejects the very token it announced, it is
switching right now: the coordinator retries later instead of asking for a
token. Never log the token.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.hassio import is_hassio

from .const import CONF_API_TOKEN, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Seconds to wait for the Supervisor's discovery list.
SUPERVISOR_TIMEOUT = 10


async def _async_supervisor_discovery(hass: HomeAssistant) -> list[Any]:
    """All discovery messages stored by the Supervisor."""
    from homeassistant.components.hassio import get_supervisor_client

    return list(await get_supervisor_client(hass).discovery.list())


async def async_announced_configs(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Discovery payloads of this service known to the Supervisor.

    Empty without a Supervisor, and when the Supervisor does not answer in
    time or fails: callers fall back to their manual path.
    """
    if not is_hassio(hass) or "hassio" not in hass.config.components:
        return []
    try:
        async with asyncio.timeout(SUPERVISOR_TIMEOUT):
            messages = await _async_supervisor_discovery(hass)
    except Exception as err:  # noqa: BLE001 - optional lookup, a manual path follows
        _LOGGER.debug("Supervisor discovery not readable: %s", err)
        return []
    return [
        dict(message.config)
        for message in messages
        if getattr(message, "service", None) == DOMAIN
        and isinstance(getattr(message, "config", None), dict)
    ]


def _announced_token(config: dict[str, Any], host: str, port: int) -> str | None:
    """The token of a payload announced for exactly this host and port."""
    try:
        announced_port = int(config.get("port"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    token = config.get("api_token")
    if str(config.get("host")) != host or announced_port != port:
        return None
    if not isinstance(token, str) or not token:
        return None
    return token


async def async_token_after_rejection(
    hass: HomeAssistant, entry: ConfigEntry, rejected_token: str
) -> tuple[str | None, str | None]:
    """Return ``(renewed, announced)`` after the hub rejected a token.

    ``renewed`` is a newer token than ``rejected_token`` to retry with: one
    written to the entry meanwhile, else the one announced to the Supervisor
    (then written to the entry, without a reload: the caller retries with
    it). ``announced`` is the token the hub announced for this entry's
    host and port (None outside the Supervisor or when there is none).
    """
    host = str(entry.data.get(CONF_HOST, ""))
    port = entry.data.get(CONF_PORT)
    announced: str | None = None
    if isinstance(port, int):
        for config in await async_announced_configs(hass):
            if (token := _announced_token(config, host, port)) is not None:
                announced = token
                break

    stored = entry.data.get(CONF_API_TOKEN)
    if isinstance(stored, str) and stored and stored != rejected_token:
        return stored, announced
    if announced is None or announced == rejected_token:
        return None, announced
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_API_TOKEN: announced}
    )
    _LOGGER.info(
        "The hub renewed the integration token; using the token it "
        "announced to the Supervisor"
    )
    return announced, announced
