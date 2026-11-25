"""Binary sensor platform for Halumar integration.

Creates:
- One binary sensor per paired client showing connected/disconnected status
- Charging and "app active" per client as the hub wants them
- A hub binary sensor showing whether a pairing window is currently open
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback

from .coordinator import HubConfigEntry, HubCoordinator
from .entity import ClientEntity, HubEntity, prune_known_ids
from .panel import PanelStatusEntity, async_setup_panel_platform

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

# The coordinator fetches all data; entity updates need no limit.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HubConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Halumar binary sensor entities."""
    coordinator = entry.runtime_data
    known_client_ids: set[str] = set()

    @callback
    def _async_add_new_entities() -> None:
        """Add binary sensor entities for newly discovered clients."""
        if not coordinator.data:
            return
        prune_known_ids(known_client_ids, coordinator.data.get("clients", []))
        new_entities: list[BinarySensorEntity] = []
        for client in coordinator.data.get("clients", []):
            client_id = client.get("id")
            if client_id and client_id not in known_client_ids:
                known_client_ids.add(client_id)
                new_entities.append(
                    HubClientConnectedSensor(coordinator, entry, client)
                )
        if new_entities:
            async_add_entities(new_entities)

    # ── Hub binary sensor (created once) ─────────────────────────────────
    async_add_entities([HubPairingActiveSensor(coordinator, entry)])

    # Add initial client binary sensors + listen for new ones
    _async_add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_entities))

    # Charging and app active as the hub wants them.
    async_setup_panel_platform(
        entry,
        "binary_sensor",
        async_add_entities,
        {"charging": PanelChargingSensor, "app_active": PanelAppActiveSensor},
    )


class HubPairingActiveSensor(HubEntity, BinarySensorEntity):
    """Binary sensor showing whether a pairing window is currently open.

    On = a pairing PIN is active on the hub. The PIN itself is never
    exposed (the pairing QR camera carries it); only non-secret window
    metadata appears as attributes.
    """

    _attr_translation_key = "pairing_active"
    # Transient pairing-window metadata has no history value; keep it out
    # of the recorder DB (same as the pairing QR camera).
    _unrecorded_attributes = frozenset({"pairing_remaining", "is_guest_pairing"})

    def __init__(self, coordinator: HubCoordinator, entry: HubConfigEntry) -> None:
        """Initialize the binary sensor."""
        super().__init__(
            coordinator, entry, "binary_sensor", "pairing_active", "Pairing Active"
        )

    @property
    def is_on(self) -> bool | None:
        """Return True while a pairing window is open."""
        if self.coordinator.data:
            return bool(self.coordinator.data.get("pairing_active", False))
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return non-secret pairing window metadata."""
        if not self.coordinator.data:
            return None
        return {
            "pairing_remaining": self.coordinator.data.get("pairing_remaining", 0),
            "is_guest_pairing": self.coordinator.data.get("pairing_is_guest", False),
        }


class HubClientConnectedSensor(ClientEntity, BinarySensorEntity):
    """Binary sensor showing if a client is connected via WebSocket."""

    _attr_translation_key = "connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(
        self,
        coordinator: HubCoordinator,
        entry: HubConfigEntry,
        client: dict[str, Any],
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(
            coordinator, entry, client, "binary_sensor", "connected", "Connected"
        )

    @property
    def is_on(self) -> bool | None:
        """Return True if client is connected."""
        client = self.client
        if client:
            return bool(client.get("is_connected", False))
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes."""
        client = self.client
        if not client:
            return None
        attrs: dict[str, Any] = {
            "client_id": client.get("id"),
            "client_name": client.get("name"),
            "last_seen": client.get("last_seen"),
            "is_active": client.get("is_active"),
            "is_guest": client.get("is_guest", False),
        }
        if client.get("is_guest") and client.get("expires_at"):
            attrs["expires_at"] = client["expires_at"]
        if client.get("pending_deletion"):
            # Client was deleted in the hub but hasn't come back online to
            # reset yet ("waiting for device"). Surface it so the lingering
            # device is recognisable instead of looking merely disconnected.
            attrs["pending_deletion"] = True
            if client.get("pending_deletion_at"):
                attrs["pending_deletion_at"] = client["pending_deletion_at"]
        return attrs


class PanelChargingSensor(PanelStatusEntity, BinarySensorEntity):
    """Whether the device is on power (charging or full)."""

    _platform = "binary_sensor"
    _key = "charging"
    _object_name = "Charging"
    # Own key: the German name "Lädt" is not the device class name
    # ("Ladestatus"); the English name is the same.
    # pylint: disable-next=home-assistant-redundant-translation-key
    _attr_translation_key = "charging"
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    @property
    def is_on(self) -> bool | None:
        """On for charging and full, off for unplugged, unknown otherwise."""
        client = self.client
        state = client.get("battery_state") if client else None
        if state in ("charging", "full"):
            return True
        if state == "unplugged":
            return False
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """battery_state and low_power as reported."""
        client = self.client
        if not client:
            return None
        return {
            "battery_state": client.get("battery_state"),
            "low_power": client.get("low_power"),
        }


class PanelAppActiveSensor(PanelStatusEntity, BinarySensorEntity):
    """Whether the app is in front (devices that are no wall panel).

    On while the app reports a screen other than ``off``; unavailable while it
    is not connected (the app never runs in the background just to report).
    """

    _platform = "binary_sensor"
    _key = "app_active"
    _object_name = "App Active"
    _attr_translation_key = "app_active"

    @property
    def is_on(self) -> bool | None:
        """On while the app is in front, off after it left the foreground."""
        screen = (self.panel or {}).get("screen")
        if screen is None:
            return None
        return bool(screen != "off")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Whether the wall panel mode is shown (panel_mode)."""
        panel = self.panel
        if panel is None or "panel_mode" not in panel:
            return None
        return {"panel_mode": panel["panel_mode"]}
