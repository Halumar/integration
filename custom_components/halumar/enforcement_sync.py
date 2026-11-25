"""Keeping the rule snapshots in line with the hub.

The hub pushes ``enforcement_rules`` over the WS session (push.py); the
status poll carries ``enforcement.rules_hashes`` as backstop. When those
differ from the stored snapshots the integration fetches the complete set
(``GET /api/integration/enforcement-rules``) once per distinct set of
hashes, so a snapshot it rejects is not fetched on every poll.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .enforcement import DATA_ENFORCEMENT, EnforcementManager

if TYPE_CHECKING:
    from .coordinator import HubConfigEntry, HubCoordinator
    from .device_users import DeviceUserManager

_LOGGER = logging.getLogger(__name__)

RULES_ENDPOINT = "/api/integration/enforcement-rules"
MSG_ENFORCEMENT_RULES = "enforcement_rules"
MSG_HA_EVENT = "ha_event"


def remote_hashes(data: Any) -> dict[str, str] | None:
    """client_id -> rules_hash from a status snapshot (None = not delivered)."""
    enforcement = data.get("enforcement") if isinstance(data, dict) else None
    hashes = enforcement.get("rules_hashes") if isinstance(enforcement, dict) else None
    if not isinstance(hashes, dict):
        return None
    return {
        k: v for k, v in hashes.items() if isinstance(k, str) and isinstance(v, str)
    }


@callback
def async_setup_rule_sync(
    hass: HomeAssistant,
    entry: HubConfigEntry,
    coordinator: HubCoordinator,
    device_users: DeviceUserManager,
) -> None:
    """Fetch the complete set when the poll reports other hashes."""
    manager: EnforcementManager = hass.data[DATA_ENFORCEMENT]
    state: dict[str, Any] = {"fetched_for": None, "running": False}

    async def _fetch(target: dict[str, str]) -> None:
        state["running"] = True
        try:
            answer = await coordinator.api_get_json(RULES_ENDPOINT)
            snapshots = answer.get("snapshots")
            if not isinstance(snapshots, list):
                # No set at all: a failed fetch, never an empty set.
                raise TypeError("answer without a list of snapshots")
            await manager.async_apply(
                snapshots, [], True, device_users, answer.get("control_paths")
            )
            state["fetched_for"] = target
        except Exception as err:  # noqa: BLE001 - retried on the next poll
            _LOGGER.debug("Rule snapshots not fetched: %s", err)
        finally:
            state["running"] = False

    @callback
    def _check() -> None:
        if not coordinator.last_update_success:
            return
        manager.async_status_updated()
        if manager.unlisted():
            entry.async_create_background_task(
                hass, manager.async_forget_unlisted(), name=f"{DOMAIN}-rule-unlisted"
            )
        if state["running"]:
            return
        target = remote_hashes(coordinator.data)
        if (
            target is None
            or target == manager.hashes()
            or target == state["fetched_for"]
        ):
            return
        entry.async_create_background_task(
            hass, _fetch(target), name=f"{DOMAIN}-rule-sync"
        )

    entry.async_on_unload(coordinator.async_add_listener(_check))
    _check()


@callback
def async_handle_push(
    hass: HomeAssistant, device_users: DeviceUserManager | None, message: dict[str, Any]
) -> bool:
    """Handle enforcement_rules / ha_event from the push session; False = other type."""
    manager: EnforcementManager | None = hass.data.get(DATA_ENFORCEMENT)
    msg_type = message.get("type")
    if manager is None or msg_type not in (MSG_ENFORCEMENT_RULES, MSG_HA_EVENT):
        return False
    data = message.get("data")
    if msg_type == MSG_HA_EVENT:
        if not manager.events.async_fire_hub_event(data):
            _LOGGER.debug("Ignoring invalid ha_event")
        return True
    if not isinstance(data, dict) or device_users is None:
        return True
    if not isinstance(data.get("snapshots"), list):
        _LOGGER.warning("Ignoring enforcement_rules without a list of snapshots")
        return True
    hass.async_create_background_task(
        manager.async_apply(
            data.get("snapshots"),
            data.get("removed"),
            data.get("complete") is True,
            device_users,
            data.get("control_paths"),
        ),
        name=f"{DOMAIN}-enforcement-rules",
    )
    return True
