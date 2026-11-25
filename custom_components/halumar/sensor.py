"""Sensor platform for Halumar integration.

Creates sensors for:
- Hub: version, area count, connected clients, guest count
- Per area: entity count (with full area details in attributes)
- Per client: current area, device name, app version, battery, connection
  type, last seen, notifications

Privacy: all sensors are content-free. Notification sensors expose the count
as state plus a notification_ids attribute (never title/message/action);
the guests sensor exposes only client_id/is_connected/expires_at per guest
(never name/device_name/tags). Volatile id-list attributes are excluded
from the recorder via _unrecorded_attributes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    LIGHT_LUX,
    PERCENTAGE,
    EntityCategory,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback

from .const import HOME_AREA_ID, HOME_STATE
from .coordinator import HubConfigEntry, HubCoordinator
from .entity import ClientEntity, HubAreaEntity, HubEntity, prune_known_ids
from .panel import (
    SCREEN_STATES,
    PanelEntity,
    PanelStatusEntity,
    async_setup_panel_platform,
)

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

# The coordinator fetches all data; entity updates need no limit.
PARALLEL_UPDATES = 0

# HA states are limited to 255 characters; client-reported text is cut and
# cleaned here as a second line of defence (the hub limits too).
_STATE_MAX = 255


def _client_text(value: Any) -> str | None:
    """Client-controlled text for a state: no control characters, <= 255."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()
    return cleaned[:_STATE_MAX] or None


def _as_int(value: Any) -> int | None:
    """A count from the snapshot as int; None for anything that is not a number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HubConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Halumar sensor entities."""
    coordinator = entry.runtime_data
    known_area_ids: set[str] = set()
    known_client_ids: set[str] = set()

    @callback
    def _async_add_new_entities() -> None:
        """Add entities for newly discovered areas/clients."""
        if not coordinator.data:
            return
        # Forget removed areas/clients (their registry entries are removed by
        # the coordinator) so an id that comes back gets its entities again.
        prune_known_ids(known_area_ids, coordinator.data.get("areas", []))
        prune_known_ids(known_client_ids, coordinator.data.get("clients", []))
        new_entities: list[SensorEntity] = []
        for area in coordinator.data.get("areas", []):
            area_id = area.get("id")
            if area_id and area_id not in known_area_ids:
                known_area_ids.add(area_id)
                new_entities.append(HubAreaSensor(coordinator, entry, area))
        for client in coordinator.data.get("clients", []):
            client_id = client.get("id")
            if client_id and client_id not in known_client_ids:
                known_client_ids.add(client_id)
                new_entities.extend(
                    cls(coordinator, entry, client) for cls in CLIENT_SENSORS
                )
        if new_entities:
            async_add_entities(new_entities)

    # ── Hub sensors (always exactly 4, created once) ─────────────────────
    async_add_entities(
        [
            HubVersionSensor(coordinator, entry),
            HubAreaCountSensor(coordinator, entry),
            HubConnectedClientsSensor(coordinator, entry),
            HubGuestsSensor(coordinator, entry),
        ]
    )

    # Add initial area/client entities + listen for new ones
    _async_add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_entities))

    # Battery (every client; on a wall panel only with panel_sensors) and the
    # panel sensors the hub wants.
    async_setup_panel_platform(
        entry,
        "sensor",
        async_add_entities,
        {
            "battery": HubClientBatterySensor,
            "panel_screen": PanelScreenSensor,
            "panel_brightness": PanelBrightnessSensor,
            "panel_illuminance": PanelIlluminanceSensor,
            "battery_temperature": BatteryTemperatureSensor,
        },
    )


# ── Hub Sensors ──────────────────────────────────────────────────────────────


class HubVersionSensor(HubEntity, SensorEntity):
    """Sensor showing the Halumar Hub version."""

    _attr_translation_key = "version"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HubCoordinator, entry: HubConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, "sensor", "version", "Version")

    @property
    def native_value(self) -> str | None:
        """Return the hub version."""
        if self.coordinator.data:
            version = self.coordinator.data.get("hub", {}).get("version")
            return str(version) if version is not None else None
        return None


class HubAreaCountSensor(HubEntity, SensorEntity):
    """Sensor showing the total number of areas."""

    _attr_translation_key = "areas"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "areas"

    def __init__(self, coordinator: HubCoordinator, entry: HubConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, "sensor", "area_count", "Areas")

    @property
    def native_value(self) -> int | None:
        """Return the number of areas."""
        if self.coordinator.data:
            return _as_int(self.coordinator.data.get("area_count", 0))
        return None


class HubConnectedClientsSensor(HubEntity, SensorEntity):
    """Sensor showing the number of connected WebSocket clients."""

    _attr_translation_key = "connected_clients"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "clients"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HubCoordinator, entry: HubConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(
            coordinator, entry, "sensor", "connected_clients", "Connected Clients"
        )

    @property
    def native_value(self) -> int | None:
        """Return the number of connected clients."""
        if self.coordinator.data:
            return _as_int(
                self.coordinator.data.get("hub", {}).get("connected_clients", 0)
            )
        return None


class HubGuestsSensor(HubEntity, SensorEntity):
    """Sensor showing the number of active guest clients.

    State: number of active guests (int)
    Attributes (PII-free by contract, mirrors the hub's guests payload):
      - guests: array of {client_id, is_connected, expires_at} triples
    """

    _attr_translation_key = "guests"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "guests"
    # The guest list is a volatile id list with no history value; keep it
    # out of the recorder DB.
    _unrecorded_attributes = frozenset({"guests"})

    def __init__(self, coordinator: HubCoordinator, entry: HubConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, entry, "sensor", "guests", "Guests")

    @property
    def native_value(self) -> int | None:
        """Return the number of active guests."""
        if self.coordinator.data:
            return _as_int(self.coordinator.data.get("guest_count", 0))
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the content-free guest list."""
        if not self.coordinator.data:
            return None
        guests = self.coordinator.data.get("guests", [])
        # Content-free projection: ids and session state only. Guest
        # name/device_name/tags are deliberately never exposed here.
        return {
            "guests": [
                {
                    "client_id": g.get("client_id"),
                    "is_connected": g.get("is_connected", False),
                    "expires_at": g.get("expires_at"),
                }
                for g in guests
            ],
        }


# ── Area Sensors ─────────────────────────────────────────────────────────────


class HubAreaSensor(HubAreaEntity, SensorEntity):
    """Sensor for a Halumar area (entity count + details in attributes)."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "entities"

    def __init__(
        self,
        coordinator: HubCoordinator,
        entry: HubConfigEntry,
        area: dict[str, Any],
    ) -> None:
        """Initialize the area sensor."""
        super().__init__(coordinator, entry, "sensor", "area_", area)

    @property
    def native_value(self) -> int | None:
        """Return the number of entities in the area."""
        area = self.area
        if area:
            return _as_int(area.get("entity_count", 0))
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the area details."""
        area = self.area
        if not area:
            return None
        return {
            "area_id": area.get("id"),
            "name": area.get("name"),
            "slug": area.get("slug"),
            "is_active": area.get("is_active"),
            "grid_cols": area.get("grid_cols"),
            "tags": area.get("tags", []),
            "color": area.get("color"),
            "description": area.get("description"),
            "control_mode": area.get("control_mode"),
        }


# ── Client Sensors ───────────────────────────────────────────────────────────


class _ClientSensorBase(ClientEntity, SensorEntity):
    """Base class for per-client sensors."""

    _key: str
    _object_name: str

    def __init__(
        self,
        coordinator: HubCoordinator,
        entry: HubConfigEntry,
        client: dict[str, Any],
    ) -> None:
        """Initialize the client sensor."""
        super().__init__(
            coordinator, entry, client, "sensor", self._key, self._object_name
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return client_id, is_guest, and expires_at as extra attributes."""
        client = self.client
        attrs: dict[str, Any] = {"client_id": self._client_id}
        if client:
            attrs["is_guest"] = client.get("is_guest", False)
            if client.get("is_guest") and client.get("expires_at"):
                attrs["expires_at"] = client["expires_at"]
        return attrs


class HubClientCurrentAreaSensor(_ClientSensorBase):
    """Sensor showing which area a client is currently viewing.

    The start screen (reserved area id ``__home__``) is the translated state
    ``home`` ("Home" / "Startseite"); any other state is the area name.
    """

    _key = "current_area"
    _object_name = "Current Area"
    _attr_translation_key = "current_area"

    @property
    def native_value(self) -> str | None:
        """Return the name of the area the client shows, or "home"."""
        client = self.client
        if not client:
            return None
        if client.get("current_area_id") == HOME_AREA_ID:
            return HOME_STATE
        return _client_text(client.get("current_area_name"))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the client and area ids."""
        client = self.client
        if not client:
            return {"client_id": self._client_id}
        attrs = {
            "client_id": self._client_id,
            "area_id": _client_text(client.get("current_area_id")),
            "is_guest": client.get("is_guest", False),
        }
        if client.get("is_guest") and client.get("expires_at"):
            attrs["expires_at"] = client["expires_at"]
        return attrs


class HubClientDeviceNameSensor(_ClientSensorBase):
    """Sensor showing the client's device name (disabled by default)."""

    _key = "device_name"
    _object_name = "Device Name"
    _attr_translation_key = "device_name"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> str | None:
        """Return the device name the app reports."""
        client = self.client
        if client:
            return _client_text(client.get("device_name"))
        return None


class HubClientAppVersionSensor(_ClientSensorBase):
    """Sensor showing the client's app version (disabled by default)."""

    _key = "app_version"
    _object_name = "App Version"
    _attr_translation_key = "app_version"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> str | None:
        """Return the app version."""
        client = self.client
        if client:
            return _client_text(client.get("app_version"))
        return None


class HubClientBatterySensor(_ClientSensorBase):
    """Sensor showing the client's battery level (named by its device class)."""

    _key = "battery"
    _object_name = "Battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    @property
    def native_value(self) -> int | None:
        """Return the battery level in percent."""
        client = self.client
        if client:
            # The hub reports -1 for "unknown" (-1 … 100).
            level = _as_int(client.get("battery_level"))
            return level if level is not None and level >= 0 else None
        return None


class HubClientConnectionTypeSensor(_ClientSensorBase):
    """Sensor showing the client's connection type (disabled by default)."""

    _key = "connection_type"
    _object_name = "Connection Type"
    _attr_translation_key = "connection_type"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> str | None:
        """Return the connection type (lan, wan, disconnected …)."""
        client = self.client
        if client:
            return _client_text(client.get("connection_type")) or "disconnected"
        return None


class HubClientLastSeenSensor(_ClientSensorBase):
    """Sensor showing when the client was last seen (disabled by default)."""

    _key = "last_seen"
    _object_name = "Last Seen"
    _attr_translation_key = "last_seen"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> datetime | None:
        """Return the datetime of when the client was last seen.

        DeviceClass.TIMESTAMP requires a timezone-aware datetime object.
        """
        client = self.client
        last_seen = client.get("last_seen") if client else None
        if not isinstance(last_seen, str) or not last_seen:
            return None
        try:
            dt = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class HubClientNotificationsSensor(_ClientSensorBase):
    """Sensor showing the number of active notifications for a client.

    State: notification_count (int)
    Attributes (content-free by contract):
      - notification_ids: ids of the active notifications; the content
        itself never leaves the hub via the integration API

    Automations that need the content react to the id-list delta and let
    the client fetch it over its authenticated WS channel.
    """

    _key = "notifications"
    _object_name = "Notifications"
    _attr_translation_key = "notifications"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "notifications"
    # Volatile id list, changes on every notification event; keep it out
    # of the recorder DB.
    _unrecorded_attributes = frozenset({"notification_ids"})

    @property
    def native_value(self) -> int | None:
        """Return the number of active notifications."""
        client = self.client
        if client:
            return _as_int(client.get("notification_count", 0))
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the notification ids (never their content)."""
        client = self.client
        if not client:
            return {"client_id": self._client_id}
        attrs = {
            "client_id": self._client_id,
            "is_guest": client.get("is_guest", False),
            "notification_ids": list(client.get("notification_ids", [])),
        }
        if client.get("is_guest") and client.get("expires_at"):
            attrs["expires_at"] = client["expires_at"]
        return attrs


CLIENT_SENSORS: tuple[type[_ClientSensorBase], ...] = (
    HubClientCurrentAreaSensor,
    HubClientDeviceNameSensor,
    HubClientAppVersionSensor,
    HubClientConnectionTypeSensor,
    HubClientLastSeenSensor,
    HubClientNotificationsSensor,
)


# ── Wall panel sensors ────────────────────────────────────────────────


class PanelScreenSensor(PanelEntity, SensorEntity):
    """Screen state of a wall panel or TV; off while not connected."""

    _platform = "sensor"
    _key = "panel_screen"
    _object_name = "Panel Screen"
    _attr_translation_key = "panel_screen"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = SCREEN_STATES

    @property
    def native_value(self) -> str | None:
        """active, dimmed, screensaver, night or off."""
        panel = self.panel
        if not self.connected or panel is None:
            return "off"
        screen = panel.get("screen")
        return screen if screen in SCREEN_STATES else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Whether the wall panel mode is shown (panel_mode)."""
        panel = self.panel
        if panel is None or "panel_mode" not in panel:
            return None
        return {"panel_mode": panel["panel_mode"]}


class PanelBrightnessSensor(PanelStatusEntity, SensorEntity):
    """Actual backlight in percent (phones and tablets outside the wall panel:
    only while the app is in front, else unknown)."""

    _platform = "sensor"
    _key = "panel_brightness"
    _object_name = "Panel Brightness"
    _attr_translation_key = "panel_brightness"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> int | None:
        """Backlight 0–100 %."""
        value = (self.panel or {}).get("brightness")
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return round(value * 100)


class PanelIlluminanceSensor(PanelStatusEntity, SensorEntity):
    """Light sensor of an Android panel in lx."""

    _platform = "sensor"
    _key = "panel_illuminance"
    _object_name = "Panel Illuminance"
    _attr_device_class = SensorDeviceClass.ILLUMINANCE
    _attr_native_unit_of_measurement = LIGHT_LUX
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        """Illuminance in lx."""
        value = (self.panel or {}).get("illuminance")
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)


class BatteryTemperatureSensor(PanelStatusEntity, SensorEntity):
    """Battery temperature of an Android device in °C."""

    _platform = "sensor"
    _key = "battery_temperature"
    _object_name = "Battery Temperature"
    _attr_translation_key = "battery_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    @property
    def native_value(self) -> float | None:
        """Temperature in °C."""
        value = (self.panel or {}).get("battery_temperature")
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Battery health reported by Android."""
        health = (self.panel or {}).get("battery_health")
        return {"battery_health": health} if isinstance(health, str) else None
