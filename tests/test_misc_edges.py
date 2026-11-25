"""Remaining edge cases: actions, camera, compliance view, device removal."""

from __future__ import annotations

from typing import Any

import aiohttp
import pytest
from aioresponses import aioresponses
from homeassistant import config_entries
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component

from custom_components.halumar import async_remove_config_entry_device, compliance
from custom_components.halumar.config_flow import HubConfigFlow
from custom_components.halumar.const import DOMAIN
from custom_components.halumar.coordinator import (
    HubApiError,
    HubCoordinator,
    _error_detail,
    area_id_of_unique_id,
)
from custom_components.halumar.entity import async_require_admin
from custom_components.halumar.services import (
    SERVICE_MANAGE_CLIENT_TAGS,
    _privileged_lock,
)

from ._ha import MockConfigEntry, MockUser
from .common import (
    BASE_URL,
    CLIENT_ID,
    GUEST_ID,
    make_status_payload,
    setup_integration,
)

# ── Actions ──────────────────────────────────────────────────────────────


async def test_extend_guest_with_expiry_date(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """expires_at is forwarded; an invalid date is rejected by the schema."""
    await setup_integration(hass, mock_api, config_entry)
    url = f"{BASE_URL}/api/integration/clients/{GUEST_ID}/extend"
    mock_api.post(url, payload={"expires_at": "2026-08-01T00:00:00+00:00"})
    await hass.services.async_call(
        DOMAIN,
        "extend_guest",
        {"client_id": GUEST_ID, "expires_at": "2026-08-01T00:00:00+00:00"},
        blocking=True,
    )
    request = mock_api.requests[("POST", aiohttp.client.URL(url))][0]
    assert request.kwargs["json"] == {"expires_at": "2026-08-01T00:00:00+00:00"}

    with pytest.raises(ServiceValidationError, match="hours or an expiry"):
        await hass.services.async_call(
            DOMAIN, "extend_guest", {"client_id": GUEST_ID}, blocking=True
        )

    with pytest.raises(Exception, match="ISO 8601"):
        await hass.services.async_call(
            DOMAIN,
            "extend_guest",
            {"client_id": GUEST_ID, "expires_at": "tomorrow"},
            blocking=True,
        )


async def test_manage_tags_remove_unknown_client_and_busy(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """remove drops tags; unknown clients and a running update are errors."""
    await setup_integration(hass, mock_api, config_entry)
    url = f"{BASE_URL}/api/integration/clients/{CLIENT_ID}/tags"
    mock_api.post(url, payload={"success": True})
    await hass.services.async_call(
        DOMAIN,
        "manage_client_tags",
        {"client_id": CLIENT_ID, "action": "remove", "tags": ["tablet"]},
        blocking=True,
    )
    request = mock_api.requests[("POST", aiohttp.client.URL(url))][0]
    assert request.kwargs["json"] == {"tags": []}

    from custom_components.halumar.services import privileged_calls

    privileged_calls(hass).last_call.clear()
    with pytest.raises(ServiceValidationError, match="does not know the device"):
        await hass.services.async_call(
            DOMAIN,
            "manage_client_tags",
            {"client_id": "unknown-client", "action": "add", "tags": ["x"]},
            blocking=True,
        )

    lock = _privileged_lock(hass, SERVICE_MANAGE_CLIENT_TAGS)
    await lock.acquire()
    try:
        with pytest.raises(HomeAssistantError, match="already running"):
            await hass.services.async_call(
                DOMAIN,
                "manage_client_tags",
                {"client_id": CLIENT_ID, "action": "set", "tags": []},
                blocking=True,
            )
    finally:
        lock.release()


async def test_manage_tags_without_snapshot(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """add/remove need the current tags: no snapshot is an error."""
    await setup_integration(hass, mock_api, config_entry)
    config_entry.runtime_data.data = {}
    with pytest.raises(HomeAssistantError, match="No data from the"):
        await hass.services.async_call(
            DOMAIN,
            "manage_client_tags",
            {"client_id": CLIENT_ID, "action": "add", "tags": ["x"]},
            blocking=True,
        )


async def test_actions_exist_before_any_entry(hass: HomeAssistant) -> None:
    """The integration registers its actions when it loads (action-setup)."""
    assert await async_setup_component(hass, DOMAIN, {})
    services = hass.services.async_services_for_domain(DOMAIN)
    assert len(services) == 18


# ── Entity actions need an admin ─────────────────────────────────────────


async def test_require_admin_rejects_unknown_and_non_admin_users(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """The helper rejects non-admins and unknown users, allows system calls."""
    await setup_integration(hass, mock_api, config_entry)
    entity = hass.data["entity_components"]["button"].get_entity(
        "button.halumar_hub_start_pairing"
    )
    entity.async_set_context(Context())
    await async_require_admin(entity)

    user = MockUser().add_to_hass(hass)
    entity.async_set_context(Context(user_id=user.id))
    with pytest.raises(Unauthorized):
        await async_require_admin(entity)
    entity.async_set_context(Context(user_id="unknown"))
    with pytest.raises(Unauthorized):
        await async_require_admin(entity)


# ── Camera ───────────────────────────────────────────────────────────────


async def test_camera_keeps_the_last_image_on_errors(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Network errors and non-200 answers return the cached image."""
    await setup_integration(hass, mock_api, config_entry)
    camera = hass.data["entity_components"]["camera"].get_entity(
        "camera.halumar_hub_pairing_qr_code"
    )
    url = f"{BASE_URL}/api/integration/pairing-qr"
    mock_api.get(url, body=b"PNG1", headers={"X-Pairing-Active": "false"})
    assert await camera.async_camera_image() == b"PNG1"
    mock_api.get(url, exception=aiohttp.ClientConnectionError("down"))
    assert await camera.async_camera_image() == b"PNG1"
    mock_api.get(url, status=503)
    assert await camera.async_camera_image() == b"PNG1"

    # Without a snapshot the camera does not claim to stream.
    config_entry.runtime_data.data = None
    assert camera.is_streaming is False


# ── Coordinator helpers ──────────────────────────────────────────────────


def test_error_detail_shapes() -> None:
    """The hub's reason is taken from every FastAPI error shape."""
    assert _error_detail('{"detail": {"message": "m"}}', 409) == "m (HTTP 409)"
    assert _error_detail('{"detail": {"code": "c"}}', 409) == "c (HTTP 409)"
    assert _error_detail('{"detail": [{"msg": "bad"}]}', 422) == "bad (HTTP 422)"
    assert _error_detail("", 500) == "HTTP 500 (HTTP 500)"


def test_area_id_of_unique_id() -> None:
    """Only per-area entities of this entry map to an area."""
    assert area_id_of_unique_id("e1", "e2_hub_area_x") is None
    assert area_id_of_unique_id("e1", None) is None
    assert area_id_of_unique_id("e1", "e1_hub_area_count") is None
    assert area_id_of_unique_id("e1", "e1_hub_area_switch_a") == "a"
    assert area_id_of_unique_id("e1", "e1_hub_version") is None


async def test_malformed_action_answer(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """A 200 answer without JSON is an error; non-object JSON is empty."""
    await setup_integration(hass, mock_api, config_entry)
    coordinator: HubCoordinator = config_entry.runtime_data
    url = f"{BASE_URL}/api/integration/pairing/cancel"
    mock_api.post(url, status=200, body="not json")
    with pytest.raises(HubApiError, match="malformed"):
        await coordinator.api_post("/api/integration/pairing/cancel")
    mock_api.post(url, status=200, payload=[1])
    assert await coordinator.api_post("/api/integration/pairing/cancel") == {}


async def test_area_events_and_disconnect_events(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Disconnect and area change of a known client fire their events."""
    await setup_integration(hass, mock_api, config_entry)
    coordinator: HubCoordinator = config_entry.runtime_data
    events: list[Any] = []

    @callback
    def record(event: Any) -> None:
        # A callback runs in the event loop in firing order (a plain
        # function would run in the executor, in any order).
        events.append(event)

    hass.bus.async_listen(f"{DOMAIN}_client_area_changed", record)
    hass.bus.async_listen(f"{DOMAIN}_client_disconnected", record)

    payload = make_status_payload()
    payload["clients"][0]["current_area_id"] = "area-office"
    payload["clients"][0]["current_area_name"] = "Office"
    coordinator._fire_events(payload)  # noqa: SLF001
    payload = make_status_payload()
    payload["clients"][0]["is_connected"] = False
    coordinator._fire_events(payload)  # noqa: SLF001
    await hass.async_block_till_done()

    assert [e.event_type for e in events] == [
        f"{DOMAIN}_client_area_changed",
        f"{DOMAIN}_client_disconnected",
    ]
    assert events[0].data["new_area_name"] == "Office"


# ── Config flow ──────────────────────────────────────────────────────────


async def test_hassio_confirm_without_discovery_aborts(hass: HomeAssistant) -> None:
    """The confirm step cannot run without discovery data."""
    flow = HubConfigFlow()
    flow.hass = hass
    flow.context = {"source": config_entries.SOURCE_HASSIO}
    result = await flow.async_step_hassio_confirm()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_discovery_info"


# ── Device removal ───────────────────────────────────────────────────────


async def test_remove_config_entry_device_rules(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Only devices of clients the hub no longer lists may be deleted."""
    await setup_integration(hass, mock_api, config_entry)
    dev_reg = dr.async_get(hass)

    def device(*identifiers: tuple[str, str]) -> dr.DeviceEntry:
        return dev_reg.async_get_or_create(
            config_entry_id=config_entry.entry_id, identifiers=set(identifiers)
        )

    hub = device((DOMAIN, "halumar_hub"))
    known = device((DOMAIN, f"halumar_client_{CLIENT_ID}"))
    gone = device(("other", "x"), (DOMAIN, "halumar_client_gone"))
    foreign = device(("other", "y"))
    assert not await async_remove_config_entry_device(hass, config_entry, hub)
    assert not await async_remove_config_entry_device(hass, config_entry, known)
    assert await async_remove_config_entry_device(hass, config_entry, gone)
    assert await async_remove_config_entry_device(hass, config_entry, foreign)

    config_entry.runtime_data.clients_truncated = True
    assert not await async_remove_config_entry_device(hass, config_entry, gone)


# ── Compliance view ──────────────────────────────────────────────────────


async def test_compliance_view_registration_and_rate_limit_pruning(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """The view is registered once; the IP table is pruned when full."""
    await setup_integration(hass, mock_api, config_entry)
    compliance.async_register_compliance_view(hass)  # second call: no-op
    assert compliance._async_get_coordinator(hass) is config_entry.runtime_data  # noqa: SLF001

    seen_at = compliance.last_requests(hass)
    seen_at.clear()
    for i in range(compliance._RATE_LIMIT_MAX_TRACKED):  # noqa: SLF001
        seen_at[f"2001:db8::{i:x}"] = 0.0
    assert compliance._rate_limited(hass, "192.0.2.1") is False  # noqa: SLF001
    assert len(seen_at) == 1

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert compliance._async_get_coordinator(hass) is None  # noqa: SLF001
