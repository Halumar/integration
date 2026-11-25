"""DateTime platform for Halumar integration.

Creates an editable datetime entity for each guest client showing the
guest session expiry. Changing the value calls the hub's extend_guest
API to update the expiry in real time.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.core import HomeAssistant, callback

from .coordinator import HubConfigEntry, HubCoordinator, hub_call
from .entity import ClientEntity, async_require_admin, prune_known_ids

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

_LOGGER = logging.getLogger(__name__)

# Expiry changes go to the hub one at a time.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HubConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Halumar datetime entities for guest clients."""
    coordinator = entry.runtime_data
    known_guest_ids: set[str] = set()

    @callback
    def _async_add_new_entities() -> None:
        """Add datetime entities for newly discovered guest clients."""
        if not coordinator.data:
            return
        prune_known_ids(known_guest_ids, coordinator.data.get("clients", []))
        new_entities: list[DateTimeEntity] = []
        for client in coordinator.data.get("clients", []):
            client_id = client.get("id")
            if (
                client_id
                and client.get("is_guest")
                and client_id not in known_guest_ids
            ):
                known_guest_ids.add(client_id)
                new_entities.append(HubGuestExpiryDateTime(coordinator, entry, client))
        if new_entities:
            async_add_entities(new_entities)

    _async_add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_entities))


class HubGuestExpiryDateTime(ClientEntity, DateTimeEntity):
    """Editable datetime entity for a guest client's expiry.

    Displays the current expiry timestamp and allows changing it
    directly from the HA UI (e.g. via a datetime picker card).
    Setting a new value calls POST /api/integration/clients/{id}/extend
    on the hub.
    """

    _attr_translation_key = "guest_expiry"

    def __init__(
        self,
        coordinator: HubCoordinator,
        entry: HubConfigEntry,
        client: dict[str, Any],
    ) -> None:
        """Initialize the datetime entity."""
        super().__init__(
            coordinator, entry, client, "datetime", "guest_expiry", "Guest Expiry"
        )

    @property
    def native_value(self) -> datetime | None:
        """Return the current guest expiry as a datetime object."""
        client = self.client
        expires_at = client.get("expires_at") if client else None
        if not isinstance(expires_at, str) or not expires_at:
            return None
        try:
            dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return client_id as extra attribute."""
        return {"client_id": self._client_id}

    async def async_set_value(self, value: datetime) -> None:
        """Set a new guest expiry datetime.

        Called when the user changes the value in the HA UI. Forwards the
        new expiry to the hub via POST /api/integration/clients/{id}/extend.
        """
        # A guest must not extend its own access.
        await async_require_admin(self)
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        # The hub stores the expiry to the second (UTC).
        value = value.replace(microsecond=0)

        await hub_call(
            "update_guest_expiry",
            self.coordinator.api_post_json(
                f"/api/integration/clients/{self._client_id}/extend",
                {"expires_at": value.isoformat()},
            ),
            {"client_id": self._client_id},
        )
        _LOGGER.info(
            "Updated guest expiry for %s to %s", self._client_id, value.isoformat()
        )
        await self.coordinator.async_request_refresh()
