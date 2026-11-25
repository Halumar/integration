"""Tests for entity naming, defaults and edge cases of the snapshot data."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import aiohttp
import pytest
from aioresponses import aioresponses

from custom_components.halumar.brand import BRAND_NAME
from custom_components.halumar.const import DOMAIN
from custom_components.halumar.sensor import (
    CLIENT_SENSORS,
    HubClientAppVersionSensor,
    HubClientBatterySensor,
    HubClientConnectionTypeSensor,
    HubClientDeviceNameSensor,
    HubClientLastSeenSensor,
    _client_text,
)
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from ._ha import MockConfigEntry
from .common import (
    BASE_URL,
    CLIENT_ID,
    GUEST_ID,
    make_status_payload,
    setup_integration,
)

DISABLED_BY_DEFAULT = ("device_name", "app_version", "connection_type", "last_seen")


async def test_entity_names_come_from_translations(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Entities have a translation key and are named in context of their device."""
    await setup_integration(hass, mock_api, config_entry)
    ent_reg = er.async_get(hass)

    entries = er.async_entries_for_config_entry(ent_reg, config_entry.entry_id)
    assert entries
    for reg_entry in entries:
        # Battery (device class name) is the only entity without its own key.
        if reg_entry.unique_id.endswith("_battery"):
            assert reg_entry.translation_key is None
            assert reg_entry.original_device_class == "battery"
        else:
            assert reg_entry.translation_key, reg_entry.entity_id
        assert reg_entry.has_entity_name

    assert hass.states.get("sensor.halumar_hub_areas").name == f"{BRAND_NAME} Hub Areas"
    assert (
        hass.states.get("sensor.halumar_hub_area_kitchen").name
        == f"{BRAND_NAME} Hub Area: Kitchen"
    )
    assert (
        hass.states.get("binary_sensor.halumar_wall_tablet_connected").name
        == f"{BRAND_NAME}: Wall Tablet Connected"
    )
    assert (
        hass.states.get("datetime.halumar_guest_guest_phone_guest_expiry").name
        == f"{BRAND_NAME} Guest: Guest Phone Guest access until"
    )


async def test_diagnostic_client_entities_are_disabled_by_default(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Rarely used per-device sensors are registered but disabled."""
    await setup_integration(hass, mock_api, config_entry)
    ent_reg = er.async_get(hass)
    eid = config_entry.entry_id

    for key in DISABLED_BY_DEFAULT:
        entity_id = ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{eid}_client_{CLIENT_ID}_{key}"
        )
        assert entity_id is not None
        reg_entry = ent_reg.async_get(entity_id)
        assert reg_entry is not None
        assert reg_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(entity_id) is None

    for key in ("current_area", "battery", "notifications"):
        entity_id = ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{eid}_client_{CLIENT_ID}_{key}"
        )
        assert entity_id is not None
        assert ent_reg.async_get(entity_id).disabled_by is None  # type: ignore[union-attr]


async def test_disabled_client_sensors_read_the_snapshot(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """The disabled-by-default sensors report correct values once enabled."""
    payload = make_status_payload()
    payload["clients"][0]["last_seen"] = "2026-07-03T09:59:00Z"
    payload["clients"][0]["device_name"] = "iPad\x00 Pro\n"
    await setup_integration(hass, mock_api, config_entry, payload=payload)
    coordinator = config_entry.runtime_data
    client = coordinator.client_by_id[CLIENT_ID]
    guest = coordinator.client_by_id[GUEST_ID]

    def make(cls: type, data: dict[str, Any]) -> Any:
        return cls(coordinator, config_entry, data)

    assert make(HubClientDeviceNameSensor, client).native_value == "iPad Pro"
    assert make(HubClientAppVersionSensor, client).native_value == "1.2.3"
    assert make(HubClientConnectionTypeSensor, client).native_value == "lan"
    assert make(HubClientConnectionTypeSensor, guest).native_value == "disconnected"
    assert make(HubClientLastSeenSensor, client).native_value == datetime(
        2026, 7, 3, 9, 59, tzinfo=UTC
    )

    # A device the snapshot no longer lists reports nothing.
    gone = {"id": "gone-client", "name": "Gone"}
    for cls in CLIENT_SENSORS:
        sensor = make(cls, gone)
        assert sensor.native_value is None
        assert sensor.extra_state_attributes == {"client_id": "gone-client"}


@pytest.mark.parametrize(
    ("last_seen", "expected"),
    [
        ("2026-07-03T09:59:00", datetime(2026, 7, 3, 9, 59, tzinfo=UTC)),
        ("not a date", None),
        (None, None),
        (42, None),
    ],
)
async def test_last_seen_parsing(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    last_seen: Any,
    expected: datetime | None,
) -> None:
    """Naive timestamps are UTC; anything unparsable is unknown."""
    payload = make_status_payload()
    payload["clients"][0]["last_seen"] = last_seen
    await setup_integration(hass, mock_api, config_entry, payload=payload)
    client = config_entry.runtime_data.client_by_id[CLIENT_ID]
    sensor = HubClientLastSeenSensor(config_entry.runtime_data, config_entry, client)
    assert sensor.native_value == expected


@pytest.mark.parametrize(
    ("level", "expected"),
    [(81, 81), (-1, None), (True, None), ("80", None), (55.6, 55)],
)
async def test_battery_values(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    level: Any,
    expected: int | None,
) -> None:
    """The hub's -1 (unknown), booleans and strings never become a level."""
    payload = make_status_payload()
    payload["clients"][0]["battery_level"] = level
    await setup_integration(hass, mock_api, config_entry, payload=payload)
    client = config_entry.runtime_data.client_by_id[CLIENT_ID]
    sensor = HubClientBatterySensor(config_entry.runtime_data, config_entry, client)
    assert sensor.native_value == expected


def test_client_text_cleans_and_cuts() -> None:
    """Client text loses control characters and is cut to 255 characters."""
    assert _client_text(None) is None
    assert _client_text(12) == "12"
    assert _client_text("\x07  ") is None
    assert _client_text("a" * 300) == "a" * 255


async def test_snapshot_edge_cases(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Area icons, pending deletion, odd values and missing coordinates."""
    payload = make_status_payload()
    payload["areas"][0]["icon"] = "mdi:chef-hat"
    payload["clients"][0]["pending_deletion"] = True
    payload["clients"][0]["pending_deletion_at"] = "2026-07-03T10:00:00+00:00"
    payload["clients"][1]["expires_at"] = "invalid"
    payload["area_count"] = "two"
    await setup_integration(hass, mock_api, config_entry, payload=payload)

    assert (
        hass.states.get("sensor.halumar_hub_area_kitchen").attributes["icon"]
        == "mdi:chef-hat"
    )
    # The area's control mode as the hub sends it.
    assert (
        hass.states.get("sensor.halumar_hub_area_kitchen").attributes["control_mode"]
        is None
    )
    office = hass.states.get("sensor.halumar_hub_area_office")
    assert office is not None and office.attributes["control_mode"] == "basic"
    assert hass.states.get("switch.halumar_hub_area_kitchen").attributes["icon"] == (
        "mdi:chef-hat"
    )
    connected = hass.states.get("binary_sensor.halumar_wall_tablet_connected")
    assert connected.attributes["pending_deletion"] is True
    assert connected.attributes["pending_deletion_at"] == "2026-07-03T10:00:00+00:00"
    tracker = hass.states.get("device_tracker.halumar_wall_tablet_location")
    assert "latitude" not in tracker.attributes
    assert (
        hass.states.get("datetime.halumar_guest_guest_phone_guest_expiry").state
        == STATE_UNKNOWN
    )
    assert hass.states.get("sensor.halumar_hub_areas").state == STATE_UNKNOWN


async def test_entities_without_snapshot_data(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """An empty snapshot adds nothing and every entity reports unknown."""
    await setup_integration(hass, mock_api, config_entry)
    coordinator = config_entry.runtime_data
    before = len(hass.states.async_entity_ids())

    coordinator.async_set_updated_data({})
    await hass.async_block_till_done()

    assert len(hass.states.async_entity_ids()) == before
    for entity_id in (
        "sensor.halumar_hub_version",
        "sensor.halumar_hub_areas",
        "sensor.halumar_hub_connected_clients",
        "sensor.halumar_hub_guests",
        "sensor.halumar_hub_area_kitchen",
        "switch.halumar_hub_area_kitchen",
        "binary_sensor.halumar_hub_pairing_active",
    ):
        assert hass.states.get(entity_id).state == STATE_UNKNOWN, entity_id
    camera = hass.states.get("camera.halumar_hub_pairing_qr_code")
    assert camera.state == "idle"
    assert camera.attributes["pairing_active"] is False
    assert hass.states.get("button.halumar_hub_cancel_pairing").state == "unavailable"


async def test_area_switch_turn_off_and_failures(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Turning an area off calls deactivate; failures raise translated errors."""
    await setup_integration(hass, mock_api, config_entry)
    url = f"{BASE_URL}/api/integration/areas/area-kitchen/deactivate"
    mock_api.post(url, status=200, payload={"ok": True})
    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": "switch.halumar_hub_area_kitchen"},
        blocking=True,
    )
    assert ("POST", aiohttp.client.URL(url)) in mock_api.requests

    mock_api.post(url, status=500, body="boom")
    with pytest.raises(HomeAssistantError, match="Could not turn off area"):
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": "switch.halumar_hub_area_kitchen"},
            blocking=True,
        )


async def test_cancel_pairing_button(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """The cancel button is only available during pairing and cancels it."""
    payload = make_status_payload()
    payload["pairing_active"] = True
    payload["pairing_remaining"] = 120
    await setup_integration(hass, mock_api, config_entry, payload=payload)
    entity_id = "button.halumar_hub_cancel_pairing"
    assert hass.states.get(entity_id).state != "unavailable"

    url = f"{BASE_URL}/api/integration/pairing/cancel"
    mock_api.post(url, status=200, payload={"cancelled": True})
    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id}, blocking=True
    )
    assert ("POST", aiohttp.client.URL(url)) in mock_api.requests

    mock_api.post(url, exception=aiohttp.ClientConnectionError("down"))
    with pytest.raises(HomeAssistantError, match="not reachable"):
        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )


async def test_guest_expiry_naive_value_and_unknown_guest(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """A naive expiry is sent as UTC; a 404 is a validation error."""
    await setup_integration(hass, mock_api, config_entry)
    entity_id = "datetime.halumar_guest_guest_phone_guest_expiry"
    url = f"{BASE_URL}/api/integration/clients/{GUEST_ID}/extend"
    mock_api.post(url, status=404, payload={"detail": "Client not found"})

    entity = hass.data["entity_components"]["datetime"].get_entity(entity_id)
    with pytest.raises(ServiceValidationError, match="Client not found"):
        await entity.async_set_value(datetime(2026, 8, 1, 12, 0, 0, 500))
    request = mock_api.requests[("POST", aiohttp.client.URL(url))][0]
    assert request.kwargs["json"] == {"expires_at": "2026-08-01T12:00:00+00:00"}


async def test_current_area_on_the_home_screen(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """The reserved area id __home__ is the translated state "home"."""
    from homeassistant.helpers import translation

    payload = make_status_payload()
    payload["clients"][0]["current_area_id"] = "__home__"
    payload["clients"][0]["current_area_name"] = None
    await setup_integration(hass, mock_api, config_entry, payload=payload)

    state = hass.states.get("sensor.halumar_wall_tablet_current_area")
    assert state.state == "home"
    assert state.attributes["area_id"] == "__home__"

    key = f"component.{DOMAIN}.entity.sensor.current_area.state.home"
    for language, text in (("en", "Home"), ("de", "Startseite")):
        strings = await translation.async_get_translations(
            hass, language, "entity", [DOMAIN]
        )
        assert strings[key] == text

    # An area name is still shown as it is.
    payload["clients"][0]["current_area_id"] = "area-kitchen"
    payload["clients"][0]["current_area_name"] = "Kitchen"
    config_entry.runtime_data.async_set_updated_data(
        config_entry.runtime_data._process_snapshot(  # noqa: SLF001
            payload, datetime.now(UTC)
        )
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.halumar_wall_tablet_current_area").state == "Kitchen"
