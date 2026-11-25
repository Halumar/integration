"""Switch platform for Halumar integration.

Creates one switch per area to activate/deactivate it.
Calls POST /api/integration/areas/{id}/activate or /deactivate.

One access switch per paired device that has access hours or is of
kind guest, house sitter or babysitter (status ``clients[].access.switch`` not
null): ``switch.<ns>_<device>_access``. Access = access hours (when set) AND
switch on; the hub stores the switch (POST /api/integration/access-switch).
Only HA admins and automations operate it; ``<ns>.control`` refuses it for
devices (``own_entity``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .coordinator import (
    MAX_ENTITIES_PER_POLL,
    HubConfigEntry,
    HubCoordinator,
    hub_call,
)
from .entity import (
    ClientEntity,
    HubAreaEntity,
    async_require_admin,
    prune_known_ids,
)

from .const import DOMAIN

ACCESS_ENDPOINT = "/api/integration/access-switch"
ACCESS_KEY = "access"
# Fixed object name: switch.<ns>_<device>_access.
ACCESS_OBJECT_NAME = "Access"

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

# Area changes go to the hub one at a time.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HubConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Halumar area switches."""
    coordinator = entry.runtime_data
    known_area_ids: set[str] = set()

    @callback
    def _async_add_new_entities() -> None:
        """Add switch entities for newly discovered areas."""
        if not coordinator.data:
            return
        prune_known_ids(known_area_ids, coordinator.data.get("areas", []))
        new_entities: list[SwitchEntity] = []
        for area in coordinator.data.get("areas", []):
            area_id = area.get("id")
            if area_id and area_id not in known_area_ids:
                known_area_ids.add(area_id)
                new_entities.append(HubAreaSwitch(coordinator, entry, area))
        if new_entities:
            async_add_entities(new_entities)

    # Add initial area switches + listen for new ones
    _async_add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_entities))

    known_access: set[str] = set()

    @callback
    def _async_sync_access() -> None:
        """Add or remove the access switches as the hub wants them."""
        if not coordinator.data:
            return
        clients = coordinator.data.get("clients", [])
        wanted = {c["id"]: c for c in clients if c.get("id") and has_access_switch(c)}
        new = [
            ClientAccessSwitch(coordinator, entry, client)
            for cid, client in wanted.items()
            if cid not in known_access
        ]
        known_access.update(wanted)
        if len(clients) < MAX_ENTITIES_PER_POLL:
            gone = known_access - set(wanted)
            ent_reg = er.async_get(hass)
            for cid in gone:
                uid = f"{entry.entry_id}_client_{cid}_{ACCESS_KEY}"
                entity_id = ent_reg.async_get_entity_id("switch", DOMAIN, uid)
                if entity_id:
                    ent_reg.async_remove(entity_id)
            known_access.difference_update(gone)
        if new:
            async_add_entities(new)

    _async_sync_access()
    entry.async_on_unload(coordinator.async_add_listener(_async_sync_access))


def has_access_switch(client: dict[str, Any]) -> bool:
    """The hub offers an access switch for this client."""
    access = client.get("access")
    return (
        isinstance(access, dict)
        and isinstance(access.get("switch"), bool)
        and not client.get("pending_deletion")
    )


class HubAreaSwitch(HubAreaEntity, SwitchEntity):
    """Switch to activate/deactivate a Halumar area."""

    def __init__(
        self,
        coordinator: HubCoordinator,
        entry: HubConfigEntry,
        area: dict[str, Any],
    ) -> None:
        """Initialize the area switch."""
        super().__init__(coordinator, entry, "switch", "area_switch_", area)

    @property
    def is_on(self) -> bool | None:
        """Return True if area is active."""
        area = self.area
        if area is None:
            return None
        return bool(area.get("is_active", False))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Activate the area."""
        await async_require_admin(self)
        await hub_call(
            "activate_area",
            self.coordinator.api_post(
                f"/api/integration/areas/{self._area_id}/activate"
            ),
            {"area_id": self._area_id},
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Deactivate the area."""
        await async_require_admin(self)
        await hub_call(
            "deactivate_area",
            self.coordinator.api_post(
                f"/api/integration/areas/{self._area_id}/deactivate"
            ),
            {"area_id": self._area_id},
        )
        await self.coordinator.async_request_refresh()


class ClientAccessSwitch(ClientEntity, SwitchEntity):
    """Access switch of a paired device: admins and automations only."""

    _attr_translation_key = ACCESS_KEY

    def __init__(
        self,
        coordinator: HubCoordinator,
        entry: HubConfigEntry,
        client: dict[str, Any],
    ) -> None:
        """Initialize unique id, device and switch.<ns>_<device>_access."""
        super().__init__(
            coordinator, entry, client, "switch", ACCESS_KEY, ACCESS_OBJECT_NAME
        )

    @property
    def _access(self) -> dict[str, Any] | None:
        client = self.client
        access = client.get("access") if client else None
        return access if isinstance(access, dict) else None

    @property
    def available(self) -> bool:
        """Only while the hub reports a switch for the device."""
        access = self._access
        return (
            super().available
            and access is not None
            and isinstance(access.get("switch"), bool)
        )

    @property
    def is_on(self) -> bool | None:
        """Switch state (the device's access also needs its access hours)."""
        access = self._access
        value = access.get("switch") if access else None
        return value if isinstance(value, bool) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Access right now: open, reason, until, opens_at, early_until."""
        access = self._access or {}
        early = access.get("early")
        return {
            "open": access.get("open"),
            "reason": access.get("reason"),
            "until": access.get("until"),
            "opens_at": access.get("opens_at"),
            "schedule": access.get("schedule"),
            # Opened early by a Yes to the device's request.
            "early_until": early.get("until") if isinstance(early, dict) else None,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Allow access (within the access hours)."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Close access."""
        await self._async_set(False)

    async def _async_set(self, on: bool) -> None:
        await async_require_admin(self)
        user_name = None
        context = self._context
        if context is not None and context.user_id is not None:
            user = await self.hass.auth.async_get_user(context.user_id)
            user_name = (user.name or None) if user else None
        name = str((self.client or {}).get("name") or self._client_id)
        answer = await hub_call(
            "access_switch",
            self.coordinator.api_post_json(
                ACCESS_ENDPOINT,
                {"client_id": self._client_id, "on": on, "user": user_name},
            ),
            {"device": name},
            status_keys={409: "access_switch_none"},
        )
        client = self.client
        if client is not None and isinstance(answer.get("access"), dict):
            client["access"] = answer["access"]
            self.async_write_ha_state()
        await self.coordinator.async_request_refresh()
