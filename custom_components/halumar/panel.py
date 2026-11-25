"""Wall panel as a Home Assistant device.

Panel entities hang on the existing client device. Which ones exist is
decided by the hub (``clients[].panel_ha`` in the status: rules ``kiosk``,
``panel_remote``, ``panel_sensors``, platform, never guests); this module
adds and removes them as that list changes. Every device except
watches has sensors (battery, charging, "app active" or the panel's screen
state, brightness); control entities stay with wall panels and TVs (Apple TV,
Android TV). Live values are unavailable while the app is not connected.

Commands go to ``POST /api/integration/clients/{id}/panel``. Only Home
Assistant administrators and calls without a user (automations, scripts)
may press, set or select them (``async_require_admin``); ``<ns>.control``
refuses every entity of this integration for devices (``own_entity``), so no
device, not even a kids panel with all areas, controls another panel.

The admin actions ``<ns>.panel_announce`` (text, volume, chime) and ``<ns>.panel_show_camera``
(a camera entity) send the panel commands ``announce`` and ``show_camera``
through the same endpoint; the sensor ``battery_temperature`` (Android) with
the attribute ``battery_health``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import aiohttp

from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .coordinator import (
    MAX_ENTITIES_PER_POLL,
    HubApiError,
    HubConfigEntry,
    HubCoordinator,
)
from .entity import ClientEntity, async_require_admin

_LOGGER = logging.getLogger(__name__)

PANEL_ENDPOINT = "/api/integration/clients/{client_id}/panel"

# Entity key per platform and the panel_ha item that creates it.
SENSOR_KEYS = {
    "battery": "battery",
    "panel_screen": "screen",
    "panel_brightness": "brightness",
    "panel_illuminance": "illuminance",
    "battery_temperature": "battery_temperature",
}
BINARY_SENSOR_KEYS = {"charging": "charging", "app_active": "app"}
NUMBER_KEYS = {"panel_brightness_control": "brightness"}
SELECT_KEYS = {"panel_brightness_mode": "brightness_mode"}
BUTTON_KEYS = {
    "panel_wake": "wake",
    "panel_screensaver": "screensaver",
    "panel_home": "home",
    "panel_reload": "reload",
}
# platform -> key -> (panel_ha list, item)
PANEL_KEYS: dict[str, dict[str, tuple[str, str]]] = {
    "sensor": {k: ("sensors", v) for k, v in SENSOR_KEYS.items()},
    "binary_sensor": {k: ("sensors", v) for k, v in BINARY_SENSOR_KEYS.items()},
    "number": {k: ("controls", v) for k, v in NUMBER_KEYS.items()},
    "select": {k: ("controls", v) for k, v in SELECT_KEYS.items()},
    "button": {k: ("controls", v) for k, v in BUTTON_KEYS.items()},
}

SCREEN_STATES = ["active", "dimmed", "screensaver", "night", "off"]
BRIGHTNESS_MODES = ["auto", "manual"]
ERROR_CODES = frozenset(
    {
        "not_found",
        "deactivated",
        "guest",
        "not_a_panel",
        "remote_off",
        "unsupported",
        "not_connected",
        "timeout",
        "invalid",
        "not_in_panel_mode",
        "not_pro",
        "failed",
        "announce_off",
        "not_visible",
    }
)


def wanted(platform: str, key: str, client: dict[str, Any]) -> bool:
    """Whether the client should have the entity ``key`` of ``platform``.

    The battery sensor exists for every client that is no panel (unchanged
    since before the wall panel entities); on a panel it follows ``panel_sensors``.
    """
    spec = PANEL_KEYS[platform].get(key)
    if spec is None:
        return False
    panel_ha = client.get("panel_ha")
    if not isinstance(panel_ha, dict):
        return key == "battery"
    items = panel_ha.get(spec[0])
    return isinstance(items, list) and spec[1] in items


def panel_unique_ids(entry_id: str, data: dict[str, Any]) -> dict[str, bool]:
    """unique_id -> wanted, for every governed panel key of every client."""
    out: dict[str, bool] = {}
    for client in data.get("clients", []):
        cid = client.get("id")
        if not cid:
            continue
        for platform, keys in PANEL_KEYS.items():
            for key in keys:
                uid = f"{entry_id}_client_{cid}_{key}"
                out[f"{platform}:{uid}"] = wanted(platform, key, client)
    return out


@callback
def async_remove_unwanted(
    hass: HomeAssistant, entry_id: str, data: dict[str, Any], *, truncated: bool
) -> None:
    """Remove registry entries of panel entities the hub no longer wants."""
    if truncated:
        return
    wanted_by_uid = panel_unique_ids(entry_id, data)
    ent_reg = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(ent_reg, entry_id):
        state = wanted_by_uid.get(f"{entity.domain}:{entity.unique_id}")
        if state is False:
            _LOGGER.info("Removing panel entity %s (not wanted)", entity.entity_id)
            ent_reg.async_remove(entity.entity_id)


@callback
def async_setup_panel_platform(
    entry: HubConfigEntry,
    platform: str,
    async_add_entities: Callable[[list[Any]], None],
    factories: dict[
        str, Callable[[HubCoordinator, HubConfigEntry, dict[str, Any]], Entity]
    ],
) -> None:
    """Add panel entities of ``platform`` as the hub wants them."""
    coordinator = entry.runtime_data
    known: set[tuple[str, str]] = set()

    @callback
    def _sync() -> None:
        if not coordinator.data:
            return
        clients = coordinator.data.get("clients", [])
        complete = len(clients) < MAX_ENTITIES_PER_POLL
        current: set[tuple[str, str]] = set()
        new: list[Entity] = []
        for client in clients:
            cid = client.get("id")
            if not cid:
                continue
            for key, factory in factories.items():
                if not wanted(platform, key, client):
                    continue
                current.add((cid, key))
                if (cid, key) not in known:
                    known.add((cid, key))
                    new.append(factory(coordinator, entry, client))
        if complete:
            known.intersection_update(current)
        if new:
            async_add_entities(new)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


async def async_actor(entity: Entity) -> dict[str, Any]:
    """Who triggers the command (for the hub's log); never a device user."""
    return await async_actor_of(entity.hass, entity._context)  # noqa: SLF001


async def async_actor_of(
    hass: HomeAssistant, context: Context | None
) -> dict[str, Any]:
    """Actor of a context: automation without a user, else the HA admin."""
    if context is None or context.user_id is None:
        return {"kind": "automation"}
    user = await hass.auth.async_get_user(context.user_id)
    return {
        "kind": "ha_admin",
        "ha_user_id": context.user_id,
        "name": (user.name if user and user.name else None),
    }


class PanelEntity(ClientEntity):
    """Base of the panel entities of one client."""

    _key: str
    _object_name: str
    _platform: str

    def __init__(
        self, coordinator: HubCoordinator, entry: HubConfigEntry, client: dict[str, Any]
    ) -> None:
        """Initialize unique id, device and requested entity id."""
        super().__init__(
            coordinator, entry, client, self._platform, self._key, self._object_name
        )

    @property
    def panel(self) -> dict[str, Any] | None:
        """Last panel status of this client (None when none was reported)."""
        client = self.client
        panel = client.get("panel") if client else None
        return panel if isinstance(panel, dict) else None

    @property
    def connected(self) -> bool:
        client = self.client
        return bool(client and client.get("is_connected"))


class PanelStatusEntity(PanelEntity):
    """A live value of the app: unavailable while the app is not connected."""

    @property
    def available(self) -> bool:
        """Only while the app has its hub connection."""
        return super().available and self.connected


class PanelControlEntity(PanelEntity):
    """A panel entity that sends commands: admins and automations only."""

    @property
    def available(self) -> bool:
        """Only while the panel is connected and reports a panel status."""
        return super().available and self.connected and self.panel is not None

    async def async_command(self, action: str, **args: Any) -> None:
        """Send one panel command through the hub; raise a translated error."""
        await async_require_admin(self)
        body: dict[str, Any] = {
            "action": action,
            **args,
            "actor": await async_actor(self),
        }
        try:
            answer = await self.coordinator.api_post_json(
                PANEL_ENDPOINT.format(client_id=self._client_id), body
            )
        except HubApiError as err:
            code = err.code if err.code in ERROR_CODES else "failed"
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=f"panel_{code}",
                translation_placeholders={"device": self._device_name()},
            ) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="not_reachable",
                translation_placeholders={"error": str(err) or type(err).__name__},
            ) from err
        client = self.client
        if client is not None and isinstance(answer.get("panel"), dict):
            client["panel"] = answer["panel"]
            self.coordinator.async_update_listeners()

    def _device_name(self) -> str:
        client = self.client
        return str((client or {}).get("name") or self._client_id)


async def async_send_panel_action(
    hass: HomeAssistant,
    coordinator: HubCoordinator,
    context: Context | None,
    client_id: str,
    action: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Send ``announce`` / ``show_camera`` from an admin action.

    The action is registered admin-only (HA admins and automations); the hub
    checks the device, the rule and the camera and answers with a contract
    code, raised here as a translated error.
    """
    body: dict[str, Any] = {
        "action": action,
        **args,
        "actor": await async_actor_of(hass, context),
    }
    device = next(
        (
            str(c.get("name") or client_id)
            for c in (coordinator.data or {}).get("clients", [])
            if c.get("id") == client_id
        ),
        client_id,
    )
    try:
        return await coordinator.api_post_json(
            PANEL_ENDPOINT.format(client_id=client_id), body
        )
    except HubApiError as err:
        code = err.code if err.code in ERROR_CODES else "failed"
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key=f"panel_{code}",
            translation_placeholders={"device": device},
        ) from err
    except (aiohttp.ClientError, TimeoutError) as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="not_reachable",
            translation_placeholders={"error": str(err) or type(err).__name__},
        ) from err
