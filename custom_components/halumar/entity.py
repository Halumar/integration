"""Base entities and shared entity helpers for the Halumar integration."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .brand import BRAND_NAME
from .const import (
    CLIENT_DEVICE_ID_PREFIX,
    DOMAIN,
    HUB_DEVICE_ID,
    HUB_DEVICE_NAME,
    HUB_OBJECT_ID_PREFIX,
    MANUFACTURER,
    MODEL_CLIENT,
    MODEL_HUB,
    OBJECT_ID_PREFIX,
)
from .coordinator import MAX_ENTITIES_PER_POLL, HubConfigEntry, HubCoordinator

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
MAX_OWNER_LABEL = 50


async def async_require_admin(entity: Entity) -> None:
    """Raise Unauthorized unless the user behind the current action is an admin.

    The privileged services are registered admin-only, but the same actions
    are reachable as entity actions (button.press, switch.turn_on/off,
    datetime.set_value) which HA lets every user and every token call. A
    guest could start a pairing or extend its own access.
    Calls without a user (automations, scripts run by HA) stay allowed:
    only admins can create those.
    """
    context = entity._context  # noqa: SLF001 - the action's context
    if context is None or context.user_id is None:
        return
    user = await entity.hass.auth.async_get_user(context.user_id)
    if user is None or not user.is_admin:
        raise Unauthorized(
            context=context, entity_id=entity.entity_id, permission=POLICY_CONTROL
        )


def prune_known_ids(known: set[str], items: list[dict[str, Any]]) -> None:
    """Drop ids from a platform's "already added" set that left the snapshot.

    The coordinator removes the registry entries of deleted clients and
    areas; forgetting the id here lets an id that comes back (restored
    backup, re-created area) get its entities again without a reload.
    """
    if len(items) >= MAX_ENTITIES_PER_POLL:
        return  # possibly truncated: a missing id does not mean it is gone
    known.intersection_update(item.get("id") for item in items if item.get("id"))


def hub_entity_id(platform: str, name: str) -> str:
    """Requested entity id of a hub entity: <platform>.<domain>_hub_<name>.

    Built from the namespace and a fixed English object name, never from
    the (translated) display name or the device name, so a language or
    brand change leaves every entity id untouched.
    """
    return f"{platform}.{HUB_OBJECT_ID_PREFIX}{slugify(name)}"


def client_entity_id(platform: str, client: dict[str, Any], name: str) -> str:
    """Requested entity id of a client entity: <platform>.<domain>_[guest_]<client>_<name>."""
    kind = "guest_" if client.get("is_guest") else ""
    client_slug = slugify(client.get("name") or "client")
    return f"{platform}.{OBJECT_ID_PREFIX}{kind}{client_slug}_{slugify(name)}"


def client_device_identifier(client_id: str) -> str:
    """Device registry identifier of a client (shared with the hub)."""
    return f"{CLIENT_DEVICE_ID_PREFIX}{client_id}"


def owner_device_label(device: str, owner: str | None) -> str:
    """'<owner> <device>' without repeating the owner.

    "Mum iPhone" owned by "Mum" stays "Mum iPhone", and a device
    named like its owner ("Kind" of "Kind") stays "Kind".
    """
    name = device.casefold()
    if (
        owner
        and name != owner.casefold()
        and not name.startswith(f"{owner.casefold()} ")
    ):
        return f"{owner} {device}"
    return device


def client_device_name(client: dict[str, Any]) -> str:
    """Registry name of a client device: '<brand>[ Guest]: [<owner> ]<device>'.

    Entities of the device have entity names, so their display names follow
    ("Halumar: Mum iPhone Access").
    """
    device = str(client.get("name") or "Unknown")
    owner = client.get("owner_label")
    if isinstance(owner, str):
        owner = _CONTROL_CHARS.sub("", owner).strip()[:MAX_OWNER_LABEL] or None
    else:
        owner = None
    label = owner_device_label(device, owner)
    if client.get("is_guest"):
        return f"{BRAND_NAME} Guest: {label}"
    return f"{BRAND_NAME}: {label}"


@callback
def async_sync_client_device_names(
    hass: HomeAssistant, entry_id: str, clients: list[dict[str, Any]]
) -> None:
    """Follow renames on the hub (device name, owner) in the device registry.

    Only the integration's name changes; a name the user gave in Home
    Assistant (name_by_user) stays and entity ids never change.
    """
    by_id = {c["id"]: c for c in clients if c.get("id")}
    dev_reg = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(dev_reg, entry_id):
        for domain, identifier in device.identifiers:
            if domain != DOMAIN or not identifier.startswith(CLIENT_DEVICE_ID_PREFIX):
                continue
            client = by_id.get(identifier[len(CLIENT_DEVICE_ID_PREFIX) :])
            if client is None:
                continue
            name = client_device_name(client)
            if device.name != name:
                dev_reg.async_update_device(device.id, name=name)


def client_device_info(
    coordinator: HubCoordinator, client: dict[str, Any]
) -> DeviceInfo:
    """Device info for a client or guest, linked to the hub device."""
    return DeviceInfo(
        identifiers={(DOMAIN, client_device_identifier(client["id"]))},
        name=client_device_name(client),
        manufacturer=MANUFACTURER,
        model=client.get("device_name") or MODEL_CLIENT,
        sw_version=client.get("app_version"),
        **via_hub_device(coordinator),
    )


def hub_device_info(coordinator: HubCoordinator) -> DeviceInfo:
    """Device info for the hub device (single source for all platforms)."""
    version = "unknown"
    if coordinator.data:
        version = coordinator.data.get("hub", {}).get("version", "unknown")
    return DeviceInfo(
        identifiers={(DOMAIN, HUB_DEVICE_ID)},
        name=HUB_DEVICE_NAME,
        manufacturer=MANUFACTURER,
        model=MODEL_HUB,
        sw_version=version,
        configuration_url=coordinator.base_url,
    )


# DeviceInfo(via_device=(domain, identifier)) is deprecated since HA 2026.x
# (removed in 2027.8) in favour of via_device_id (the registry id). The
# 2025.11 minimum only knows via_device (DeviceInfo is a TypedDict there, a
# dataclass from HA 2026.x on).
_SUPPORTS_VIA_DEVICE_ID = "via_device_id" in (
    getattr(DeviceInfo, "__dataclass_fields__", None)
    or getattr(DeviceInfo, "__annotations__", {})
)


@callback
def async_register_hub_device(
    hass: HomeAssistant, entry: HubConfigEntry, coordinator: HubCoordinator
) -> None:
    """Register the hub device up front so client devices can link to it."""
    info = hub_device_info(coordinator)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers=info["identifiers"],
        name=info["name"],
        manufacturer=info["manufacturer"],
        model=info["model"],
        sw_version=info["sw_version"],
        configuration_url=info["configuration_url"],
    )
    coordinator.hub_device_id = device.id


def via_hub_device(coordinator: HubCoordinator) -> dict[str, Any]:
    """DeviceInfo kwargs that link a client device to the hub device."""
    if _SUPPORTS_VIA_DEVICE_ID and coordinator.hub_device_id:
        return {"via_device_id": coordinator.hub_device_id}
    return {"via_device": (DOMAIN, HUB_DEVICE_ID)}  # pragma: no cover - HA < 2026


class HubEntity(CoordinatorEntity[HubCoordinator]):
    """Base of every entity on the hub device.

    The display name comes from the translation key (entity-translations),
    the entity id from the fixed English ``object_name``.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HubCoordinator,
        entry: HubConfigEntry,
        platform: str,
        key: str,
        object_name: str,
    ) -> None:
        """Initialize unique id, device and requested entity id."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_hub_{key}"
        self._attr_device_info = hub_device_info(coordinator)
        self.entity_id = hub_entity_id(platform, object_name)


class HubAreaEntity(HubEntity):
    """Base of the per-area entities (one sensor and one switch per area)."""

    _attr_translation_key = "area"

    def __init__(
        self,
        coordinator: HubCoordinator,
        entry: HubConfigEntry,
        platform: str,
        key_prefix: str,
        area: dict[str, Any],
    ) -> None:
        """Name the entity after the area ("Area: <name>")."""
        area_name = str(area.get("name") or "Unknown")
        super().__init__(
            coordinator,
            entry,
            platform,
            f"{key_prefix}{area['id']}",
            f"Area: {area_name}",
        )
        self._area_id: str = area["id"]
        self._attr_translation_placeholders = {"area_name": area_name}
        # The area's own icon wins over the default from icons.json.
        if area.get("icon"):
            self._attr_icon = area["icon"]

    @property
    def area(self) -> dict[str, Any] | None:
        """This area in the current snapshot (None when gone)."""
        if not self.coordinator.data:
            return None
        for area in self.coordinator.data.get("areas", []):
            if area.get("id") == self._area_id:
                return area  # type: ignore[no-any-return]
        return None


class ClientEntity(CoordinatorEntity[HubCoordinator]):
    """Base of every entity on a client (paired device) device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HubCoordinator,
        entry: HubConfigEntry,
        client: dict[str, Any],
        platform: str,
        key: str,
        object_name: str,
    ) -> None:
        """Initialize unique id, device and requested entity id."""
        super().__init__(coordinator)
        self._client_id: str = client["id"]
        self._attr_unique_id = f"{entry.entry_id}_client_{client['id']}_{key}"
        self._attr_device_info = client_device_info(coordinator, client)
        self.entity_id = client_entity_id(platform, client, object_name)

    @property
    def client(self) -> dict[str, Any] | None:
        """This client in the current snapshot (None when gone)."""
        return self.coordinator.client_by_id.get(self._client_id)
