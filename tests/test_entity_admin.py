"""Entity actions are admin-only, the camera hides PINs.

The privileged services are registered admin-only, but HA lets every user and
every token call entity actions. button.press (pairing), switch.turn_on/off
(areas) and datetime.set_value (guest expiry: self-extension) must therefore
check the calling user themselves. Automations/scripts (no user) stay allowed.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from aioresponses import aioresponses
from yarl import URL

from custom_components.halumar.const import DOMAIN
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import entity_registry as er

from ._ha import MockConfigEntry, MockUser
from .common import BASE_URL, GUEST_ID, make_status_payload, setup_integration

START_URL = f"{BASE_URL}/api/integration/start-pairing"
ACTIVATE_URL = f"{BASE_URL}/api/integration/areas/area-office/activate"
EXTEND_URL = f"{BASE_URL}/api/integration/clients/{GUEST_ID}/extend"
QR_URL = f"{BASE_URL}/api/integration/pairing-qr"


def _entity_id(hass: HomeAssistant, platform: str, unique_suffix: str) -> str:
    registry = er.async_get(hass)
    for entry in registry.entities.values():
        if entry.platform == DOMAIN and entry.unique_id.endswith(unique_suffix):
            assert entry.entity_id.startswith(f"{platform}.")
            return entry.entity_id
    raise AssertionError(f"no {platform} entity with suffix {unique_suffix}")


ACTIONS = [
    ("button", "press", "_hub_start_pairing", {}, START_URL),
    ("switch", "turn_on", "_hub_area_switch_area-office", {}, ACTIVATE_URL),
    (
        "datetime",
        "set_value",
        f"_client_{GUEST_ID}_guest_expiry",
        {"datetime": datetime(2027, 12, 31, 23, 59, tzinfo=timezone.utc)},
        EXTEND_URL,
    ),
]


@pytest.mark.parametrize(("platform", "service", "suffix", "data", "url"), ACTIONS)
async def test_non_admin_entity_action_rejected(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    platform,
    service,
    suffix,
    data,
    url,
) -> None:
    await setup_integration(hass, mock_api, config_entry)
    entity_id = _entity_id(hass, platform, suffix)
    non_admin = MockUser().add_to_hass(hass)
    assert not non_admin.is_admin

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            platform,
            service,
            {"entity_id": entity_id, **data},
            blocking=True,
            context=Context(user_id=non_admin.id),
        )
    assert ("POST", URL(url)) not in mock_api.requests


@pytest.mark.parametrize(("platform", "service", "suffix", "data", "url"), ACTIONS)
@pytest.mark.parametrize("as_admin", [True, False])
async def test_admin_and_automation_allowed(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    platform,
    service,
    suffix,
    data,
    url,
    as_admin,
) -> None:
    await setup_integration(hass, mock_api, config_entry)
    entity_id = _entity_id(hass, platform, suffix)
    mock_api.post(url, payload={"success": True}, repeat=True)
    context = None
    if as_admin:
        admin = MockUser(groups=None, is_owner=True).add_to_hass(hass)
        assert admin.is_admin
        context = Context(user_id=admin.id)

    await hass.services.async_call(
        platform,
        service,
        {"entity_id": entity_id, **data},
        blocking=True,
        context=context,
    )
    assert ("POST", URL(url)) in mock_api.requests


async def _camera_image(hass: HomeAssistant) -> bytes | None:
    from homeassistant.components.camera import async_get_image

    entity_id = _entity_id(hass, "camera", "_hub_pairing_qr")
    try:
        return (await async_get_image(hass, entity_id)).content
    except Exception:  # noqa: BLE001 - HA raises when no image is available
        return None


async def test_camera_refuses_image_that_may_carry_the_pin(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    payload = make_status_payload()
    payload["pairing_active"] = True
    await setup_integration(hass, mock_api, config_entry, payload)
    # an old hub: QR with PIN, no "omitted" marker
    mock_api.get(
        QR_URL,
        status=200,
        body=b"\x89PNG-with-pin",
        headers={"X-Pairing-Active": "true"},
    )
    assert await _camera_image(hass) is None


async def test_camera_shows_pin_free_status_image(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    payload = make_status_payload()
    payload["pairing_active"] = True
    await setup_integration(hass, mock_api, config_entry, payload)
    mock_api.get(
        QR_URL,
        status=200,
        body=b"\x89PNG-status",
        headers={"X-Pairing-Active": "true", "X-Pairing-Pin": "omitted"},
    )
    assert await _camera_image(hass) == b"\x89PNG-status"


async def test_camera_serves_the_standby_image_outside_pairing(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """The camera is on without a pairing window (no HTTP 503)."""
    await setup_integration(hass, mock_api, config_entry)
    mock_api.get(
        QR_URL,
        status=200,
        body=b"\x89PNG-standby",
        headers={"X-Pairing-Active": "false"},
    )
    entity_id = _entity_id(hass, "camera", "_hub_pairing_qr")
    assert hass.states.get(entity_id).state == "idle"
    assert await _camera_image(hass) == b"\x89PNG-standby"


@pytest.mark.parametrize(
    ("all_areas", "body"),
    [
        (False, {"is_guest": True}),
        (True, {"is_guest": True, "guest_tags": ["*"]}),
    ],
)
async def test_guest_button_follows_the_all_areas_option(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    all_areas,
    body,
) -> None:
    """Without the option the hub's guest profile decides (fail closed);
    with it the guest sees all areas. Duration: the hub's guest default."""
    hass.config_entries.async_update_entry(
        config_entry, options={"guest_button_all_areas": all_areas}
    )
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(START_URL, payload={"success": True})
    entity_id = _entity_id(hass, "button", "_hub_start_guest_pairing")
    await hass.services.async_call(
        "button", "press", {"entity_id": entity_id}, blocking=True
    )
    sent = mock_api.requests[("POST", URL(START_URL))][0].kwargs["json"]
    assert sent == body
