"""Reservations before the v1.0 freeze.

The battery temperature sensor (Android, attribute battery_health), the admin
actions panel_announce / panel_show_camera through the hub's panel endpoint
(admins and automations only, translated errors announce_off / not_visible),
and the hub's tag_scanned event fired as <ns>_event and <ns>_tag_scanned.
"""

from __future__ import annotations

import pytest
from aioresponses import aioresponses
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from yarl import URL

from custom_components.halumar.const import DOMAIN
from custom_components.halumar.enforcement_sync import async_handle_push

from ._ha import MockConfigEntry, MockUser, async_capture_events
from .common import CLIENT_ID, setup_integration
from .test_device_users import existing_owner  # noqa: F401 - fixture
from .test_enforce import Env, env  # noqa: F401 - fixture
from .test_panel import FULL, PANEL, PANEL_URL, panel_payload

try:
    import probatio as vol
except ImportError:  # pragma: no cover - HA < 2026.9
    import voluptuous as vol  # type: ignore[no-redef]


def _ok(action: str) -> dict:
    return {"success": True, "client_id": CLIENT_ID, "action": action, "panel": PANEL}


async def test_battery_temperature_sensor(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    ha = {**FULL, "sensors": [*FULL["sensors"], "battery_temperature"]}
    await setup_integration(
        hass,
        mock_api,
        config_entry,
        panel_payload(
            ha, panel={**PANEL, "battery_temperature": 31.5, "battery_health": "good"}
        ),
    )
    state = hass.states.get("sensor.halumar_wall_tablet_battery_temperature")
    assert state is not None
    assert float(state.state) == 31.5
    assert state.attributes["battery_health"] == "good"
    assert state.attributes["unit_of_measurement"] == "°C"


async def test_no_battery_temperature_without_panel_ha(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry, panel_payload())
    assert hass.states.get("sensor.halumar_wall_tablet_battery_temperature") is None


@pytest.mark.parametrize("as_admin", [True, False])
async def test_announce_by_admin_or_automation(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    as_admin: bool,
) -> None:
    await setup_integration(hass, mock_api, config_entry, panel_payload())
    mock_api.post(PANEL_URL, payload=_ok("announce"), repeat=True)
    context = None
    if as_admin:
        admin = MockUser(groups=None, is_owner=True, name="Admin").add_to_hass(hass)
        context = Context(user_id=admin.id)
    await hass.services.async_call(
        DOMAIN,
        "panel_announce",
        {
            "client_id": CLIENT_ID,
            "text": "Es hat geklingelt",
            "volume": 60,
            "chime": False,
        },
        blocking=True,
        context=context,
    )
    body = mock_api.requests[("POST", URL(PANEL_URL))][-1].kwargs["json"]
    assert body["action"] == "announce" and body["text"] == "Es hat geklingelt"
    assert body["volume"] == 60 and body["chime"] is False
    assert body["actor"]["kind"] == ("ha_admin" if as_admin else "automation")


async def test_non_admin_cannot_announce(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry, panel_payload())
    user = MockUser().add_to_hass(hass)
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "panel_announce",
            {"client_id": CLIENT_ID, "text": "Hallo"},
            blocking=True,
            context=Context(user_id=user.id),
        )
    assert ("POST", URL(PANEL_URL)) not in mock_api.requests


async def test_show_camera_and_errors(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry, panel_payload())
    mock_api.post(PANEL_URL, payload=_ok("show_camera"))
    await hass.services.async_call(
        DOMAIN,
        "panel_show_camera",
        {"client_id": CLIENT_ID, "entity_id": "camera.front_door"},
        blocking=True,
    )
    body = mock_api.requests[("POST", URL(PANEL_URL))][-1].kwargs["json"]
    assert body == {
        "action": "show_camera",
        "entity_id": "camera.front_door",
        "actor": {"kind": "automation"},
    }
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            "panel_show_camera",
            {"client_id": CLIENT_ID, "entity_id": "light.kitchen"},
            blocking=True,
        )
    for code, key in (
        ("not_visible", "panel_not_visible"),
        ("announce_off", "panel_announce_off"),
        ("brand_new", "panel_failed"),
    ):
        mock_api.post(
            PANEL_URL, status=422, payload={"detail": {"code": code, "message": code}}
        )
        with pytest.raises(HomeAssistantError) as err:
            await hass.services.async_call(
                DOMAIN,
                "panel_show_camera",
                {"client_id": CLIENT_ID, "entity_id": "camera.garage"},
                blocking=True,
            )
        assert err.value.translation_key == key
        assert err.value.translation_placeholders == {"device": "Wall Tablet"}


async def test_tag_scanned_is_fired_twice(env: Env, hass: HomeAssistant) -> None:  # noqa: F811
    events = async_capture_events(hass, f"{DOMAIN}_event")
    tags = async_capture_events(hass, f"{DOMAIN}_tag_scanned")
    data = {
        "kind": "tag_scanned",
        "category": "activity",
        "at": "2026-11-21T11:00:00+00:00",
        "client_id": CLIENT_ID,
        "device_name": "Küche",
        "tag_id": "q8Zr3vT1yL0pXw5nB2cD4e",
        "tag_name": "Flur Licht",
        "result": "ok",
    }
    assert async_handle_push(hass, None, {"type": "ha_event", "data": data})
    await hass.async_block_till_done()
    assert events[0].data["tag_name"] == "Flur Licht"
    assert events[0].data["result"] == "ok"
    assert tags[0].data == {
        "tag_id": "q8Zr3vT1yL0pXw5nB2cD4e",
        "tag_name": "Flur Licht",
        "result": "ok",
        "client_id": CLIENT_ID,
        "device_name": "Küche",
        "at": "2026-11-21T11:00:00+00:00",
    }
