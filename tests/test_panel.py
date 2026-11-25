"""Wall panel as a Home Assistant device.

Entities on the existing client device as the hub wants them (panel_ha),
commands through POST /api/integration/clients/{id}/panel only for HA admins
and automations (with actor), translated errors with the hub's code, removal
when the hub no longer wants an entity, and the battery protection
blueprint.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from aioresponses import aioresponses
from yarl import URL

from custom_components.halumar.const import DOMAIN
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import entity_registry as er

from ._ha import MockConfigEntry, MockUser
from .common import (
    BASE_URL,
    CLIENT_ID,
    GUEST_ID,
    STATUS_URL,
    make_status_payload,
    setup_integration,
)

PANEL_URL = f"{BASE_URL}/api/integration/clients/{CLIENT_ID}/panel"
PANEL = {
    "screen": "dimmed",
    "brightness": 0.35,
    "brightness_mode": "auto",
    "actions": [
        "wake",
        "screensaver",
        "home",
        "reload",
        "set_brightness",
        "set_brightness_mode",
    ],
}
FULL = {
    "sensors": ["battery", "charging", "screen", "brightness"],
    "controls": [
        "brightness",
        "brightness_mode",
        "wake",
        "screensaver",
        "home",
        "reload",
    ],
}


def panel_payload(
    panel_ha: dict[str, Any] | None = FULL, **client: Any
) -> dict[str, Any]:
    payload = make_status_payload()
    tablet = payload["clients"][0]
    tablet.update(
        {
            "battery_state": "charging",
            "low_power": False,
            "panel": copy.deepcopy(PANEL),
            "panel_ha": copy.deepcopy(panel_ha),
            **client,
        }
    )
    payload["clients"][1]["panel_ha"] = None
    return payload


def ids(hass: HomeAssistant) -> dict[str, str]:
    """unique_id suffix -> entity_id of the tablet's entities."""
    out = {}
    for entry in er.async_get(hass).entities.values():
        if entry.platform == DOMAIN and f"_client_{CLIENT_ID}_" in entry.unique_id:
            out[
                f"{entry.domain}:{entry.unique_id.split(f'_client_{CLIENT_ID}_', 1)[1]}"
            ] = entry.entity_id
    return out


async def test_entities_on_the_client_device(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry, panel_payload())
    found = ids(hass)
    assert found["sensor:battery"] == "sensor.halumar_wall_tablet_battery"
    assert found["sensor:panel_screen"] == "sensor.halumar_wall_tablet_panel_screen"
    assert (
        found["sensor:panel_brightness"]
        == "sensor.halumar_wall_tablet_panel_brightness"
    )
    assert (
        found["binary_sensor:charging"] == "binary_sensor.halumar_wall_tablet_charging"
    )
    assert (
        found["number:panel_brightness_control"]
        == "number.halumar_wall_tablet_panel_brightness"
    )
    assert (
        found["select:panel_brightness_mode"]
        == "select.halumar_wall_tablet_panel_brightness_mode"
    )
    for key in ("wake", "screensaver", "home", "reload"):
        assert found[f"button:panel_{key}"] == f"button.halumar_wall_tablet_panel_{key}"
    assert "sensor:panel_illuminance" not in found
    # Same device as the other client entities.
    reg = er.async_get(hass)
    devices = {reg.async_get(e).device_id for e in found.values()}
    assert len(devices) == 1

    assert hass.states.get("sensor.halumar_wall_tablet_panel_screen").state == "dimmed"
    assert hass.states.get("sensor.halumar_wall_tablet_panel_brightness").state == "35"
    assert hass.states.get("binary_sensor.halumar_wall_tablet_charging").state == "on"
    assert (
        hass.states.get("number.halumar_wall_tablet_panel_brightness").state == "35.0"
    )
    # The guest (no panel) keeps its plain battery sensor and gets nothing else.
    guest = [
        e.unique_id
        for e in reg.entities.values()
        if e.platform == DOMAIN and f"_client_{GUEST_ID}_" in e.unique_id
    ]
    assert any(u.endswith("_battery") for u in guest)
    assert not any("panel" in u or u.endswith("_charging") for u in guest)


async def test_disconnected_panel_is_off_and_not_controllable(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(
        hass, mock_api, config_entry, panel_payload(is_connected=False)
    )
    assert hass.states.get("sensor.halumar_wall_tablet_panel_screen").state == "off"
    assert (
        hass.states.get("button.halumar_wall_tablet_panel_wake").state == "unavailable"
    )
    # Live values are unavailable; the battery keeps its last level.
    for entity_id in (
        "binary_sensor.halumar_wall_tablet_charging",
        "sensor.halumar_wall_tablet_panel_brightness",
    ):
        assert hass.states.get(entity_id).state == "unavailable"
    assert hass.states.get("sensor.halumar_wall_tablet_battery").state == "81"


APP = {"sensors": ["battery", "charging", "app", "brightness"], "controls": []}
PHONE = {"screen": "active", "panel_mode": False, "brightness": 0.62, "actions": []}


async def test_device_without_panel_gets_sensors_never_controls(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """A phone or a tablet without wall panel mode reports app active."""
    await setup_integration(
        hass,
        mock_api,
        config_entry,
        panel_payload(APP, panel=dict(PHONE), battery_state="unplugged"),
    )
    found = ids(hass)
    assert (
        found["binary_sensor:app_active"]
        == "binary_sensor.halumar_wall_tablet_app_active"
    )
    assert not [k for k in found if k.split(":")[0] in ("number", "select")]
    assert not [k for k in found if k.startswith("button:panel")]
    assert "sensor:panel_screen" not in found
    app = hass.states.get("binary_sensor.halumar_wall_tablet_app_active")
    assert app.state == "on"
    assert app.attributes["panel_mode"] is False
    assert app.attributes["friendly_name"].endswith("App active")
    assert hass.states.get("sensor.halumar_wall_tablet_panel_brightness").state == "62"
    assert hass.states.get("binary_sensor.halumar_wall_tablet_charging").state == "off"
    assert hass.states.get("sensor.halumar_wall_tablet_battery").state == "81"


async def test_app_in_background_and_disconnected(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    background = {"screen": "off", "panel_mode": False, "brightness": None}
    await setup_integration(
        hass,
        mock_api,
        config_entry,
        panel_payload(APP, panel=background),
        repeat=False,
    )
    assert (
        hass.states.get("binary_sensor.halumar_wall_tablet_app_active").state == "off"
    )
    assert (
        hass.states.get("sensor.halumar_wall_tablet_panel_brightness").state
        == "unknown"
    )
    mock_api.get(
        STATUS_URL,
        payload=panel_payload(APP, panel=background, is_connected=False),
        repeat=True,
    )
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    for entity_id in (
        "binary_sensor.halumar_wall_tablet_app_active",
        "binary_sensor.halumar_wall_tablet_charging",
        "sensor.halumar_wall_tablet_panel_brightness",
    ):
        assert hass.states.get(entity_id).state == "unavailable"
    assert hass.states.get("sensor.halumar_wall_tablet_battery").state == "81"


async def test_device_without_status_yet(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(
        hass, mock_api, config_entry, panel_payload(APP, panel=None)
    )
    assert (
        hass.states.get("binary_sensor.halumar_wall_tablet_app_active").state
        == "unknown"
    )


@pytest.mark.parametrize("as_admin", [True, False])
async def test_admin_and_automation_command_with_actor(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    as_admin: bool,
) -> None:
    await setup_integration(hass, mock_api, config_entry, panel_payload())
    mock_api.post(
        PANEL_URL,
        payload={
            "success": True,
            "client_id": CLIENT_ID,
            "action": "set_brightness",
            "panel": {**PANEL, "brightness": 0.6, "brightness_mode": "manual"},
        },
        repeat=True,
    )
    context = None
    if as_admin:
        admin = MockUser(groups=None, is_owner=True, name="Admin").add_to_hass(hass)
        context = Context(user_id=admin.id)
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.halumar_wall_tablet_panel_brightness", "value": 60},
        blocking=True,
        context=context,
    )
    calls = mock_api.requests[("POST", URL(PANEL_URL))]
    body = calls[-1].kwargs["json"]
    assert body["action"] == "set_brightness" and body["level"] == 0.6
    if as_admin:
        assert body["actor"] == {
            "kind": "ha_admin",
            "ha_user_id": admin.id,
            "name": "Admin",
        }
    else:
        assert body["actor"] == {"kind": "automation"}
    await hass.async_block_till_done()
    assert (
        hass.states.get("number.halumar_wall_tablet_panel_brightness").state == "60.0"
    )


@pytest.mark.parametrize(
    ("platform", "service", "entity_id", "data"),
    [
        (
            "number",
            "set_value",
            "number.halumar_wall_tablet_panel_brightness",
            {"value": 10},
        ),
        ("button", "press", "button.halumar_wall_tablet_panel_wake", {}),
        (
            "select",
            "select_option",
            "select.halumar_wall_tablet_panel_brightness_mode",
            {"option": "auto"},
        ),
    ],
)
async def test_non_admin_users_never_control_a_panel(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    platform: str,
    service: str,
    entity_id: str,
    data: dict[str, Any],
) -> None:
    """Also a device user with the control path direct (system-users) is refused."""
    await setup_integration(hass, mock_api, config_entry, panel_payload())
    if platform == "select":
        er.async_get(hass).async_update_entity(entity_id, disabled_by=None)
        await hass.config_entries.async_reload(config_entry.entry_id)
        await hass.async_block_till_done()
    user = MockUser().add_to_hass(hass)
    assert not user.is_admin
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            platform,
            service,
            {"entity_id": entity_id, **data},
            blocking=True,
            context=Context(user_id=user.id),
        )
    assert ("POST", URL(PANEL_URL)) not in mock_api.requests


@pytest.mark.parametrize(
    ("status", "code", "key"),
    [
        (422, "remote_off", "panel_remote_off"),
        (422, "not_pro", "panel_not_pro"),
        (422, "not_connected", "panel_not_connected"),
        (504, "timeout", "panel_timeout"),
        (404, "not_found", "panel_not_found"),
        (422, "brand_new", "panel_failed"),
    ],
)
async def test_errors_are_translated(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    status: int,
    code: str,
    key: str,
) -> None:
    await setup_integration(hass, mock_api, config_entry, panel_payload())
    mock_api.post(
        PANEL_URL, status=status, payload={"detail": {"code": code, "message": code}}
    )
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": "button.halumar_wall_tablet_panel_home"},
            blocking=True,
        )
    assert err.value.translation_key == key
    assert err.value.translation_placeholders == {"device": "Wall Tablet"}


async def test_entities_follow_panel_ha(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry, panel_payload(), repeat=False)
    assert "button:panel_wake" in ids(hass)
    coordinator = config_entry.runtime_data
    # panel_remote off and only the screen sensor: controls, battery, charging go.
    mock_api.get(
        STATUS_URL, payload=panel_payload({"sensors": ["screen"], "controls": []})
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    found = ids(hass)
    assert set(
        k for k in found if "panel" in k or k.endswith(("battery", "charging"))
    ) == {"sensor:panel_screen"}
    assert hass.states.get("button.halumar_wall_tablet_panel_wake") is None
    # No panel any more (kiosk off): the plain battery sensor comes back.
    mock_api.get(STATUS_URL, payload=panel_payload(None), repeat=True)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    found = ids(hass)
    assert "sensor:battery" in found
    assert "sensor:panel_screen" not in found
    assert hass.states.get("sensor.halumar_wall_tablet_battery").state == "81"


def test_blueprint_is_valid_yaml_with_inputs() -> None:
    from homeassistant.util.yaml import load_yaml_dict

    from custom_components.halumar import blueprint_install as blueprints

    data = load_yaml_dict(
        str(blueprints.SOURCE / "automation" / "panel_battery_protection.yaml")
    )
    meta = data["blueprint"]
    assert meta["domain"] == "automation"
    assert set(meta["input"]) == {"battery_sensor", "charger", "lower", "upper"}
    assert json.dumps(data, default=str)


async def test_client_without_id_gets_no_panel_entities(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Data set by hand (the status poll refuses such a client) adds nothing."""
    from custom_components.halumar.panel import panel_unique_ids

    await setup_integration(hass, mock_api, config_entry)
    coordinator = config_entry.runtime_data
    before = set(hass.states.async_entity_ids())
    data = make_status_payload()
    data["clients"].append({"name": "No id"})
    assert panel_unique_ids("e", {"clients": [{"name": "No id"}]}) == {}
    coordinator.async_set_updated_data(data)
    await hass.async_block_till_done()
    assert set(hass.states.async_entity_ids()) == before
