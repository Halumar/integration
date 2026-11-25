"""Device tracker platform for Halumar integration.

One device tracker per paired client (contracts docs/location.md). The
position arrives through ``halumar.report_location`` (devices with a Home
Assistant key, home and away) or ``client_location`` from the hub (devices
on the hub path); both send the dispatcher signal SIGNAL_LOCATION.
The hub snapshot carries no position. The last fix survives a restart of
Home Assistant (restore state) while the rule ``location`` allows it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.const import ATTR_LATITUDE, ATTR_LONGITUDE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.restore_state import RestoreEntity

from .const import SIGNAL_LOCATION
from .coordinator import HubConfigEntry, HubCoordinator
from .enforcement import DATA_ENFORCEMENT
from .entity import ClientEntity, prune_known_ids
from .location import rule_of

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

# The coordinator fetches all data; entity updates need no limit.
PARALLEL_UPDATES = 0

ATTR_GPS_ACCURACY = "gps_accuracy"
# Attributes besides position and accuracy (names as in HA's mobile_app).
_EXTRA = ("altitude", "vertical_accuracy", "speed", "course")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HubConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Halumar device tracker entities."""
    coordinator = entry.runtime_data
    known_client_ids: set[str] = set()

    @callback
    def _async_add_new_entities() -> None:
        """Add device tracker entities for newly discovered clients."""
        if not coordinator.data:
            return
        prune_known_ids(known_client_ids, coordinator.data.get("clients", []))
        new_entities: list[TrackerEntity] = []
        for client in coordinator.data.get("clients", []):
            client_id = client.get("id")
            if client_id and client_id not in known_client_ids:
                known_client_ids.add(client_id)
                new_entities.append(HubClientTracker(coordinator, entry, client))
        if new_entities:
            async_add_entities(new_entities)

    # Add initial client trackers + listen for new ones
    _async_add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(_async_add_new_entities))


def _number(value: Any) -> float | None:
    """A number from a fix or a restored state, None unless it is one."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


class HubClientTracker(ClientEntity, TrackerEntity, RestoreEntity):
    """Device tracker for a Halumar client's GPS location."""

    _attr_translation_key = "location"

    def __init__(
        self,
        coordinator: HubCoordinator,
        entry: HubConfigEntry,
        client: dict[str, Any],
    ) -> None:
        """Initialize the tracker."""
        super().__init__(
            coordinator, entry, client, "device_tracker", "tracker", "Location"
        )
        self._fix: dict[str, Any] | None = None

    async def async_added_to_hass(self) -> None:
        """Restore the last fix and follow new ones."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and self._may_share():
            lat = _number(last.attributes.get(ATTR_LATITUDE))
            lon = _number(last.attributes.get(ATTR_LONGITUDE))
            if lat is not None and lon is not None:
                self._fix = {
                    "latitude": lat,
                    "longitude": lon,
                    "accuracy": _number(last.attributes.get(ATTR_GPS_ACCURACY)),
                    **{k: _number(last.attributes.get(k)) for k in _EXTRA},
                }
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_LOCATION, self._async_location)
        )

    def _may_share(self) -> bool:
        manager = self.hass.data.get(DATA_ENFORCEMENT)
        return manager is not None and rule_of(manager, self._client_id) != "off"

    @callback
    def _async_location(self, client_id: str, fix: dict[str, Any] | None) -> None:
        if client_id != self._client_id:
            return
        self._fix = fix
        self.async_write_ha_state()

    @property
    def source_type(self) -> SourceType:
        """Return the source type."""
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        """Return latitude."""
        return _number(self._fix.get("latitude")) if self._fix else None

    @property
    def longitude(self) -> float | None:
        """Return longitude."""
        return _number(self._fix.get("longitude")) if self._fix else None

    @property
    def location_accuracy(self) -> float:
        """Return the horizontal accuracy in m (0 = unknown)."""
        accuracy = _number(self._fix.get("accuracy")) if self._fix else None
        return accuracy if accuracy is not None else 0

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return client_id, battery_level and the extra values of the fix.

        battery_level is kept as an attribute for automations; the battery
        sensor is the primary source.
        """
        attrs: dict[str, Any] = {"client_id": self._client_id}
        client = self.client
        if client and client.get("battery_level") is not None:
            attrs["battery_level"] = client["battery_level"]
        if self._fix:
            for key in _EXTRA:
                value = _number(self._fix.get(key))
                if value is not None:
                    attrs[key] = value
        return attrs
