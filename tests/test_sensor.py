"""Tests for entity shape and privacy: ids, states, attributes, recorder."""

from __future__ import annotations

import json
from unittest.mock import patch

from aioresponses import aioresponses

from custom_components.halumar.binary_sensor import HubPairingActiveSensor
from custom_components.halumar.camera import HubPairingQRCamera
from custom_components.halumar.const import DOMAIN
from custom_components.halumar.sensor import (
    HubClientNotificationsSensor,
    HubGuestsSensor,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from ._ha import MockConfigEntry
from .common import (
    ALL_SECRETS,
    CLIENT_ID,
    GUEST_EXPIRES_AT,
    GUEST_ID,
    make_status_payload,
    setup_integration,
)

# Attribute keys HA adds on its own; everything else must be ours by intent.
_HA_META_KEYS = {
    "friendly_name",
    "icon",
    "unit_of_measurement",
    "state_class",
    "device_class",
    "attribution",
    "entity_picture",
    "supported_features",
    "access_token",
    "frontend_stream_type",
}


def _custom_keys(state) -> set[str]:
    """Return the integration-provided attribute keys of a state."""
    return set(state.attributes) - _HA_META_KEYS


# ── Entity ids and unique ids (contract) ─────────────────────────


async def test_hub_entity_and_unique_ids(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Hub entities land in the halumar_hub_* namespace with contract unique_ids."""
    await setup_integration(hass, mock_api, config_entry)
    ent_reg = er.async_get(hass)
    eid = config_entry.entry_id

    expected = {
        "sensor.halumar_hub_version": f"{eid}_hub_version",
        "sensor.halumar_hub_areas": f"{eid}_hub_area_count",
        "sensor.halumar_hub_connected_clients": f"{eid}_hub_connected_clients",
        "sensor.halumar_hub_guests": f"{eid}_hub_guests",
        "sensor.halumar_hub_area_kitchen": f"{eid}_hub_area_area-kitchen",
        "switch.halumar_hub_area_kitchen": f"{eid}_hub_area_switch_area-kitchen",
        "binary_sensor.halumar_hub_pairing_active": f"{eid}_hub_pairing_active",
        "button.halumar_hub_start_pairing": f"{eid}_hub_start_pairing",
        "button.halumar_hub_start_guest_pairing": f"{eid}_hub_start_guest_pairing",
        "button.halumar_hub_cancel_pairing": f"{eid}_hub_cancel_pairing",
        "camera.halumar_hub_pairing_qr_code": f"{eid}_hub_pairing_qr",
    }
    for entity_id, unique_id in expected.items():
        reg_entry = ent_reg.async_get(entity_id)
        assert reg_entry is not None, f"missing {entity_id}"
        assert reg_entry.unique_id == unique_id

    assert hass.states.get("sensor.halumar_hub_version").state == "2026.12.0"
    assert hass.states.get("sensor.halumar_hub_areas").state == "2"
    assert hass.states.get("sensor.halumar_hub_connected_clients").state == "1"


async def test_client_entity_and_unique_ids(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Client entities keep the {entry_id}_client_{id}_{key} unique_id scheme."""
    await setup_integration(hass, mock_api, config_entry)
    ent_reg = er.async_get(hass)
    eid = config_entry.entry_id

    reg_entry = ent_reg.async_get("sensor.halumar_wall_tablet_notifications")
    assert reg_entry is not None
    assert reg_entry.unique_id == f"{eid}_client_{CLIENT_ID}_notifications"

    reg_entry = ent_reg.async_get("binary_sensor.halumar_wall_tablet_connected")
    assert reg_entry is not None
    assert reg_entry.unique_id == f"{eid}_client_{CLIENT_ID}_connected"


async def test_entity_ids_do_not_depend_on_the_brand(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Entity ids come from the namespace, not from the (display) device names."""
    with (
        patch("custom_components.halumar.entity.HUB_DEVICE_NAME", "Other Brand Hub"),
        patch("custom_components.halumar.entity.BRAND_NAME", "Other Brand"),
    ):
        await setup_integration(hass, mock_api, config_entry)
    ent_reg = er.async_get(hass)
    for entity_id in (
        "sensor.halumar_hub_version",
        "switch.halumar_hub_area_kitchen",
        "camera.halumar_hub_pairing_qr_code",
        "sensor.halumar_wall_tablet_notifications",
        "binary_sensor.halumar_wall_tablet_connected",
        "sensor.halumar_guest_guest_phone_notifications",
    ):
        assert ent_reg.async_get(entity_id) is not None, f"missing {entity_id}"
    names = {
        ident[1]: device.name
        for device in dr.async_entries_for_config_entry(
            dr.async_get(hass), config_entry.entry_id
        )
        for ident in device.identifiers
        if ident[0] == DOMAIN
    }
    assert names["halumar_hub"] == "Other Brand Hub"


# ── Privacy shape: guests sensor ─────────────────────────────────────────


async def test_guests_sensor_projection_is_content_free(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """The guests attribute carries exactly client_id/is_connected/expires_at.

    The payload's guest entries include name/device_name/tags on purpose;
    the sensor must strip them.
    """
    await setup_integration(hass, mock_api, config_entry)
    state = hass.states.get("sensor.halumar_hub_guests")
    assert state is not None
    assert state.state == "1"
    assert _custom_keys(state) == {"guests"}
    assert state.attributes["guests"] == [
        {
            "client_id": GUEST_ID,
            "is_connected": False,
            "expires_at": GUEST_EXPIRES_AT,
        }
    ]


# ── Privacy shape: client notifications sensor ───────────────────────────


async def test_notifications_sensor_shape(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Notification sensors expose ids and counts, never content."""
    await setup_integration(hass, mock_api, config_entry)

    state = hass.states.get("sensor.halumar_wall_tablet_notifications")
    assert state is not None
    assert state.state == "2"
    assert _custom_keys(state) == {"client_id", "is_guest", "notification_ids"}
    assert state.attributes["client_id"] == CLIENT_ID
    assert state.attributes["is_guest"] is False
    assert state.attributes["notification_ids"] == ["n-1", "n-2"]

    guest_state = hass.states.get("sensor.halumar_guest_guest_phone_notifications")
    assert guest_state is not None
    assert guest_state.state == "0"
    assert _custom_keys(guest_state) == {
        "client_id",
        "is_guest",
        "notification_ids",
        "expires_at",
    }
    assert guest_state.attributes["is_guest"] is True
    assert guest_state.attributes["expires_at"] == GUEST_EXPIRES_AT


# ── Privacy shape: pairing entities ──────────────────────────────────────


async def test_pairing_entities_never_expose_a_pin(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Pairing sensor/camera expose window metadata only, even if a PIN leaks in."""
    payload = make_status_payload()
    payload.update(
        {"pairing_active": True, "pairing_remaining": 300, "pairing_is_guest": True}
    )
    await setup_integration(hass, mock_api, config_entry, payload=payload)

    state = hass.states.get("binary_sensor.halumar_hub_pairing_active")
    assert state is not None
    assert state.state == "on"
    assert _custom_keys(state) == {"pairing_remaining", "is_guest_pairing"}
    assert state.attributes["pairing_remaining"] == 300
    assert state.attributes["is_guest_pairing"] is True

    cam = hass.states.get("camera.halumar_hub_pairing_qr_code")
    assert cam is not None
    assert cam.attributes["pairing_active"] is True
    assert cam.attributes["pairing_remaining"] == 300
    assert cam.attributes["is_guest_pairing"] is True
    assert not any("pin" in key.lower() for key in cam.attributes)


# ── Recorder exclusions ──────────────────────────────────────────────────


def test_unrecorded_attribute_declarations() -> None:
    """Volatile id-list/window attributes are excluded from the recorder."""
    assert HubGuestsSensor._unrecorded_attributes == frozenset({"guests"})
    assert HubClientNotificationsSensor._unrecorded_attributes == frozenset(
        {"notification_ids"}
    )
    assert HubPairingActiveSensor._unrecorded_attributes == frozenset(
        {"pairing_remaining", "is_guest_pairing"}
    )
    assert HubPairingQRCamera._unrecorded_attributes == frozenset(
        {"pairing_active", "is_guest_pairing", "pairing_remaining"}
    )
    # The Entity machinery actually picked the declarations up
    assert (
        "guests" in HubGuestsSensor._Entity__combined_unrecorded_attributes  # type: ignore[attr-defined]
    )


# ── Adversarial: nothing sensitive in any entity ─────────────────────────


async def test_no_sensitive_content_in_any_entity(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """No entity may surface PIN, notification content, or HA tokens.

    The mocked payload plants all of these; if any entity forwards raw
    payload fields, this fails and names the entity.
    """
    payload = make_status_payload()
    payload.update(
        {"pairing_active": True, "pairing_remaining": 300, "pairing_is_guest": False}
    )
    await setup_integration(hass, mock_api, config_entry, payload=payload)

    for state in hass.states.async_all():
        blob = json.dumps(
            {"state": state.state, "attributes": dict(state.attributes)},
            default=str,
        )
        for needle in ALL_SECRETS:
            assert needle not in blob, f"{needle!r} leaked via {state.entity_id}"


# ── Client devices: hub link and tracker attributes (HA deprecations) ────


async def test_client_devices_link_to_hub_and_tracker_keeps_battery(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Client devices hang off the hub device; the tracker keeps battery_level.

    Guards the replacements for the deprecated DeviceInfo.via_device
    (HA 2027.8) and the deprecated TrackerEntity.battery_level override
    (HA 2027.7) on both the minimum and the current HA release.
    """
    await setup_integration(hass, mock_api, config_entry)
    dev_reg = dr.async_get(hass)
    devices = {
        identifier[1]: device
        for device in dr.async_entries_for_config_entry(dev_reg, config_entry.entry_id)
        for identifier in device.identifiers
    }
    hub = devices["halumar_hub"]
    for client_id in (CLIENT_ID, GUEST_ID):
        assert devices[f"halumar_client_{client_id}"].via_device_id == hub.id

    ent_reg = er.async_get(hass)
    eid = config_entry.entry_id
    tracker = ent_reg.async_get_entity_id(
        "device_tracker", DOMAIN, f"{eid}_client_{CLIENT_ID}_tracker"
    )
    state = hass.states.get(tracker)
    assert state.attributes["client_id"] == CLIENT_ID
    assert state.attributes["battery_level"] == 81

    guest_tracker = ent_reg.async_get_entity_id(
        "device_tracker", DOMAIN, f"{eid}_client_{GUEST_ID}_tracker"
    )
    assert "battery_level" not in hass.states.get(guest_tracker).attributes
