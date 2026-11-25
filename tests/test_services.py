"""Tests for the Halumar services: endpoints, payloads, headers, guards."""

from __future__ import annotations

import re
import uuid
from typing import Any

import aiohttp
import pytest
import voluptuous as vol
from aiohttp import web
from aiohttp.test_utils import TestServer
from aioresponses import aioresponses
from yarl import URL

from custom_components.halumar.services import privileged_calls
from custom_components.halumar.const import (
    CONF_API_TOKEN,
    CONF_USE_SSL,
    DOMAIN,
    ENTRY_UNIQUE_ID,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.helpers import entity_registry as er

from ._ha import MockConfigEntry, MockUser
from .common import (
    HUB_INSTANCE_ID,
    BASE_URL,
    CLIENT_ID,
    GUEST_ID,
    MANIFEST_VERSION,
    STATUS_URL,
    TEST_TOKEN,
    setup_integration,
)

NOTIFY_URL = f"{BASE_URL}/api/integration/notify"
KICK_URL = f"{BASE_URL}/api/integration/clients/{CLIENT_ID}/kick"
CANCEL_URL = f"{BASE_URL}/api/integration/pairing/cancel"
START_PAIRING_URL = f"{BASE_URL}/api/integration/start-pairing"
ACK_URL = f"{BASE_URL}/api/integration/notifications/ack"

ALL_SERVICE_NAMES = {
    "navigate_to_area",
    "show_entity",
    "display_notification",
    "clear_notifications",
    "ack_notifications",
    "refresh_client",
    "kick_client",
    "force_refresh",
    "start_pairing",
    "start_guest_pairing",
    "cancel_pairing",
    "manage_client_tags",
    "extend_guest",
    "control",
    "apply_rules",
    "report_location",
    "panel_announce",
    "panel_show_camera",
}


def _posted_json(mock_api: aioresponses, method: str, url: str) -> dict[str, Any]:
    """Return the JSON body of the single recorded request to url."""
    calls = mock_api.requests[(method, URL(url))]
    assert len(calls) == 1
    return calls[0].kwargs["json"]


def _status_poll_count(mock_api: aioresponses) -> int:
    """Return how many times the status endpoint has been polled."""
    return len(mock_api.requests.get(("GET", URL(STATUS_URL)), []))


async def test_all_services_registered(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Setup registers the full 17-action surface."""
    await setup_integration(hass, mock_api, config_entry)
    registered = set(hass.services.async_services()[DOMAIN])
    assert registered == ALL_SERVICE_NAMES


# ── display_notification ─────────────────────────────────────────────────


async def test_display_notification_minimal_payload(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Only caller-provided fields (plus the schema default type) are sent."""
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(NOTIFY_URL, payload={"sent": 1, "ws_sent": True})

    await hass.services.async_call(
        DOMAIN,
        "display_notification",
        {"client_id": CLIENT_ID, "title": "Hello", "message": "World"},
        blocking=True,
    )

    body = _posted_json(mock_api, "POST", NOTIFY_URL)
    # A notification to one device carries a stable UUID chosen by the
    # integration (idempotent retries and handover).
    assert re.fullmatch(
        r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", body.pop("notification_id")
    )
    assert body == {
        "client_id": CLIENT_ID,
        "title": "Hello",
        "message": "World",
        "notification_type": "default",
    }


async def test_display_notification_full_payload_broadcast(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """All optional fields forward verbatim; "all" broadcasts."""
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(NOTIFY_URL, payload={"sent": 2})

    data = {
        "client_id": "all",
        "title": "Dinner",
        "message": "Ready in 5",
        "notification_type": "sticky",
        "icon": "mdi:bell",
        "icon_svg": "<svg/>",
        "popup_mode": "both",
        "scheduled_at": "2026-07-03T20:00:00+00:00",
        "priority": 80,
        "color": "red",
        "action_type": "area",
        "action_target": "area-kitchen",
        "show_time": True,
    }
    await hass.services.async_call(DOMAIN, "display_notification", data, blocking=True)

    body = _posted_json(mock_api, "POST", NOTIFY_URL)
    # A broadcast id makes a retried broadcast idempotent.
    assert str(uuid.UUID(body.pop("notification_id"))) != ""
    assert body == data
    assert "popup" not in body  # not a field of the notification request


async def test_display_notification_requires_title_or_message(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """Without title and message the call fails before any request."""
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(NOTIFY_URL, payload={"sent": 0})

    with pytest.raises(ServiceValidationError, match="needs a title or a message"):
        await hass.services.async_call(
            DOMAIN, "display_notification", {"client_id": CLIENT_ID}, blocking=True
        )

    assert ("POST", URL(NOTIFY_URL)) not in mock_api.requests


# ── ack_notifications ────────────────────────────────────────────────────


async def test_ack_notifications_posts_batch(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """ack_notifications forwards client_id + ids to the ack endpoint."""
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(
        ACK_URL,
        payload={
            "success": True,
            "client_id": CLIENT_ID,
            "acked": ["n-1", "n-2"],
            "skipped": [],
        },
    )

    await hass.services.async_call(
        DOMAIN,
        "ack_notifications",
        {"client_id": CLIENT_ID, "notification_ids": ["n-1", "n-2"]},
        blocking=True,
    )

    assert _posted_json(mock_api, "POST", ACK_URL) == {
        "client_id": CLIENT_ID,
        "notification_ids": ["n-1", "n-2"],
    }


async def test_ack_notifications_unknown_client_is_a_validation_error(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """A 404 means an unknown client (the hub >= MIN_ADDON_VERSION has the
    endpoint), not an outdated hub."""
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(ACK_URL, status=404, payload={"detail": "Client not found"})

    with pytest.raises(ServiceValidationError, match="Client not found") as exc:
        await hass.services.async_call(
            DOMAIN,
            "ack_notifications",
            {"client_id": CLIENT_ID, "notification_ids": ["n-1"]},
            blocking=True,
        )
    assert "too old" not in str(exc.value)


async def test_ack_notifications_other_http_error_raises(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Non-404 failures raise a generic (non-version) HomeAssistantError."""
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(ACK_URL, status=500, body="boom")

    with pytest.raises(HomeAssistantError, match="Could not confirm the notifications"):
        await hass.services.async_call(
            DOMAIN,
            "ack_notifications",
            {"client_id": CLIENT_ID, "notification_ids": ["n-1"]},
            blocking=True,
        )


@pytest.mark.parametrize(
    "data",
    [
        {"client_id": CLIENT_ID, "notification_ids": []},  # 0 ids
        {  # 51 ids
            "client_id": CLIENT_ID,
            "notification_ids": [f"n-{i}" for i in range(51)],
        },
        {"client_id": CLIENT_ID, "notification_ids": ["../etc"]},  # bad id
        {"client_id": "a/b", "notification_ids": ["n-1"]},  # bad client id
        {"client_id": CLIENT_ID},  # ids missing entirely
    ],
)
async def test_ack_notifications_schema_rejects_bad_input(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    data: dict,
) -> None:
    """0/51 ids and malformed ids are rejected before any request is made."""
    await setup_integration(hass, mock_api, config_entry)

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(DOMAIN, "ack_notifications", data, blocking=True)
    assert ("POST", URL(ACK_URL)) not in mock_api.requests


async def test_ack_notifications_only_for_the_own_device_user(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """ack_notifications is not admin-only, but only the device's own HA user,
    an admin or a system call may acknowledge for a device."""
    await setup_integration(hass, mock_api, config_entry)
    payload = {
        "success": True,
        "client_id": CLIENT_ID,
        "acked": ["n-1"],
        "skipped": [],
    }
    data = {"client_id": CLIENT_ID, "notification_ids": ["n-1"]}
    # A default MockUser has no groups and is not an owner => non-admin.
    device_user = MockUser().add_to_hass(hass)
    foreign = MockUser().add_to_hass(hass)
    assert not device_user.is_admin and not foreign.is_admin
    config_entry.runtime_data.device_users._records[CLIENT_ID] = {
        "user_id": device_user.id
    }

    # The non-admin context is rejected by an admin-only service ...
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "kick_client",
            {"client_id": CLIENT_ID},
            blocking=True,
            context=Context(user_id=device_user.id),
        )

    # ... another device (e.g. a guest) cannot acknowledge for this one ...
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "ack_notifications",
            data,
            blocking=True,
            context=Context(user_id=foreign.id),
        )
    assert ("POST", URL(ACK_URL)) not in mock_api.requests

    # ... the device's own user, an admin and a system call can.
    admin = MockUser(groups=None, is_owner=True).add_to_hass(hass)
    for context in (
        Context(user_id=device_user.id),
        Context(user_id=admin.id),
        Context(),
    ):
        mock_api.post(ACK_URL, payload=payload)
        await hass.services.async_call(
            DOMAIN, "ack_notifications", data, blocking=True, context=context
        )
    calls = mock_api.requests[("POST", URL(ACK_URL))]
    assert len(calls) == 3
    assert all(call.kwargs["json"] == data for call in calls)


# ── kick_client / cancel_pairing / start_guest_pairing ───────────────────


async def test_kick_client_hits_endpoint_and_refreshes(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """kick_client POSTs to the kick endpoint then re-polls the status."""
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(KICK_URL, payload={"closed": 1})
    polls_before = _status_poll_count(mock_api)

    await hass.services.async_call(
        DOMAIN, "kick_client", {"client_id": CLIENT_ID}, blocking=True
    )
    await hass.async_block_till_done()

    assert len(mock_api.requests[("POST", URL(KICK_URL))]) == 1
    assert _status_poll_count(mock_api) == polls_before + 1


async def test_kick_client_failure_raises(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """A non-200 kick response surfaces as HomeAssistantError."""
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(KICK_URL, status=404, body="unknown client")

    with pytest.raises(HomeAssistantError, match="Could not disconnect device"):
        await hass.services.async_call(
            DOMAIN, "kick_client", {"client_id": CLIENT_ID}, blocking=True
        )


async def test_cancel_pairing_hits_endpoint_and_refreshes(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """cancel_pairing POSTs to the cancel endpoint then re-polls the status."""
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(CANCEL_URL, payload={"cancelled": True})
    polls_before = _status_poll_count(mock_api)

    await hass.services.async_call(DOMAIN, "cancel_pairing", {}, blocking=True)
    await hass.async_block_till_done()

    assert len(mock_api.requests[("POST", URL(CANCEL_URL))]) == 1
    assert _status_poll_count(mock_api) == polls_before + 1


async def test_start_guest_pairing_payload(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """start_guest_pairing maps service fields onto the start-pairing body."""
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(START_PAIRING_URL, payload={"ok": True})

    await hass.services.async_call(
        DOMAIN,
        "start_guest_pairing",
        {"expires_hours": 48, "tags": ["guest"]},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert _posted_json(mock_api, "POST", START_PAIRING_URL) == {
        "is_guest": True,
        "guest_expires_hours": 48,
        "guest_tags": ["guest"],
    }


async def test_start_guest_pairing_defaults(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Called bare, the hub decides: guest default profile and duration."""
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(START_PAIRING_URL, payload={"ok": True})

    await hass.services.async_call(DOMAIN, "start_guest_pairing", {}, blocking=True)
    await hass.async_block_till_done()

    assert _posted_json(mock_api, "POST", START_PAIRING_URL) == {"is_guest": True}


async def test_start_guest_pairing_all_areas(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """all_areas sends the marker "*"; it is exclusive and "*" is reserved."""
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(START_PAIRING_URL, payload={"ok": True})

    await hass.services.async_call(
        DOMAIN, "start_guest_pairing", {"all_areas": True}, blocking=True
    )
    await hass.async_block_till_done()
    assert _posted_json(mock_api, "POST", START_PAIRING_URL)["guest_tags"] == ["*"]

    for data in ({"all_areas": True, "tags": ["guest"]}, {"tags": ["*"]}):
        privileged_calls(hass).last_call.clear()
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN, "start_guest_pairing", data, blocking=True
            )
    assert len(mock_api.requests[("POST", URL(START_PAIRING_URL))]) == 1


async def test_start_guest_pairing_with_profile_hours_and_tags(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Profile name, duration (max 90 days) and tags travel in the body."""
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(START_PAIRING_URL, payload={"ok": True})

    await hass.services.async_call(
        DOMAIN,
        "start_guest_pairing",
        {"profile": "House sitter", "expires_hours": 2160, "tags": ["guest"]},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert _posted_json(mock_api, "POST", START_PAIRING_URL) == {
        "is_guest": True,
        "guest_expires_hours": 2160,
        "guest_tags": ["guest"],
        "profile": "House sitter",
    }
    privileged_calls(hass).last_call.clear()
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, "start_guest_pairing", {"expires_hours": 2161}, blocking=True
        )


async def test_start_pairing_payload_with_and_without_profile(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """start_pairing POSTs is_guest false, plus the profile name when given."""
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(START_PAIRING_URL, payload={"ok": True}, repeat=True)

    await hass.services.async_call(
        DOMAIN, "start_pairing", {"profile": "Wandpanel"}, blocking=True
    )
    await hass.async_block_till_done()

    calls = mock_api.requests[("POST", URL(START_PAIRING_URL))]
    assert calls[0].kwargs["json"] == {"is_guest": False, "profile": "Wandpanel"}

    # Called bare (after the cooldown) the body carries no profile key.
    privileged_calls(hass).last_call.clear()
    await hass.services.async_call(DOMAIN, "start_pairing", {}, blocking=True)
    await hass.async_block_till_done()
    assert calls[1].kwargs["json"] == {"is_guest": False}

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, "start_pairing", {"template": "Wall Tablet"}, blocking=True
        )


async def test_start_pairing_403_maps_to_disabled_message(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """The hub's pairing toggle (403) surfaces a clear error message."""
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(START_PAIRING_URL, status=403, body="pairing disabled")

    with pytest.raises(
        HomeAssistantError, match="through Home Assistant is turned off"
    ):
        await hass.services.async_call(DOMAIN, "start_pairing", {}, blocking=True)


async def test_start_guest_pairing_rate_limited(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Back-to-back guest pairing calls trip the cooldown guard."""
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(START_PAIRING_URL, payload={"ok": True}, repeat=True)

    await hass.services.async_call(DOMAIN, "start_guest_pairing", {}, blocking=True)
    with pytest.raises(HomeAssistantError, match="called too often"):
        await hass.services.async_call(DOMAIN, "start_guest_pairing", {}, blocking=True)


# ── manage_client_tags / extend_guest ────────────────────────────────────


async def test_manage_client_tags_add_merges_with_current(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Tag adds merge with the client's current tags before the POST."""
    await setup_integration(hass, mock_api, config_entry)
    tags_url = f"{BASE_URL}/api/integration/clients/{CLIENT_ID}/tags"
    mock_api.post(tags_url, payload={"ok": True})

    await hass.services.async_call(
        DOMAIN,
        "manage_client_tags",
        {"client_id": CLIENT_ID, "action": "add", "tags": ["kiosk"]},
        blocking=True,
    )
    await hass.async_block_till_done()

    body = _posted_json(mock_api, "POST", tags_url)
    assert sorted(body["tags"]) == ["kiosk", "tablet"]


async def test_extend_guest_hours(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """extend_guest POSTs the hours payload to the extend endpoint."""
    await setup_integration(hass, mock_api, config_entry)
    extend_url = f"{BASE_URL}/api/integration/clients/{GUEST_ID}/extend"
    mock_api.post(extend_url, payload={"expires_at": "2026-07-05T10:00:00+00:00"})

    await hass.services.async_call(
        DOMAIN,
        "extend_guest",
        {"client_id": GUEST_ID, "hours": 4},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert _posted_json(mock_api, "POST", extend_url) == {"hours": 4.0}


async def test_extend_guest_accepts_hub_only_fields_in_answer(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """The optional Hub only fields of the extend answer are informational:
    the call succeeds and nothing else is sent."""
    await setup_integration(hass, mock_api, config_entry)
    extend_url = f"{BASE_URL}/api/integration/clients/{GUEST_ID}/extend"
    mock_api.post(
        extend_url,
        payload={
            "client_id": GUEST_ID,
            "expires_at": "2026-07-05T10:00:00Z",
            "hub_remote_now": False,
            "hub_remote_at": "2026-07-05T08:00:00Z",
        },
    )

    await hass.services.async_call(
        DOMAIN,
        "extend_guest",
        {"client_id": GUEST_ID, "hours": 4},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert _posted_json(mock_api, "POST", extend_url) == {"hours": 4.0}
    assert len(mock_api.requests[("POST", URL(extend_url))]) == 1


async def test_guest_expiry_datetime_posts_extend(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """The guest-expiry datetime entity POSTs to the integration extend endpoint."""
    await setup_integration(hass, mock_api, config_entry)
    extend_url = f"{BASE_URL}/api/integration/clients/{GUEST_ID}/extend"
    mock_api.post(extend_url, payload={"expires_at": "2026-07-05T10:00:00+00:00"})

    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id(
        "datetime", DOMAIN, f"{config_entry.entry_id}_client_{GUEST_ID}_guest_expiry"
    )
    assert entity_id is not None

    await hass.services.async_call(
        "datetime",
        "set_value",
        {"entity_id": entity_id, "datetime": "2026-07-05T10:00:30+00:00"},
        blocking=True,
    )
    await hass.async_block_till_done()

    # The hub keeps the expiry to the second: nothing truncated.
    assert _posted_json(mock_api, "POST", extend_url) == {
        "expires_at": "2026-07-05T10:00:30+00:00"
    }


# ── Client command services ──────────────────────────────────────────────


async def test_navigate_show_clear_refresh_endpoints(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """The per-client command services POST to their own endpoints."""
    await setup_integration(hass, mock_api, config_entry)
    base = f"{BASE_URL}/api/integration/clients/{CLIENT_ID}"
    mock_api.post(f"{base}/navigate_to_area", payload={"ok": True})
    mock_api.post(f"{base}/show_entity", payload={"ok": True})
    mock_api.post(f"{base}/clear_notifications", payload={"ok": True})
    mock_api.post(f"{base}/refresh", payload={"ok": True})

    await hass.services.async_call(
        DOMAIN,
        "navigate_to_area",
        {"client_id": CLIENT_ID, "area_id": "area-kitchen"},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN,
        "show_entity",
        {"client_id": CLIENT_ID, "entity_id": "light.kitchen", "control_mode": "full"},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN, "clear_notifications", {"client_id": CLIENT_ID}, blocking=True
    )
    await hass.services.async_call(
        DOMAIN, "refresh_client", {"client_id": CLIENT_ID}, blocking=True
    )

    assert _posted_json(mock_api, "POST", f"{base}/navigate_to_area") == {
        "area_id": "area-kitchen"
    }
    assert _posted_json(mock_api, "POST", f"{base}/show_entity") == {
        "entity_id": "light.kitchen",
        "control_mode": "full",
    }
    assert _posted_json(mock_api, "POST", f"{base}/clear_notifications") == {}
    assert _posted_json(mock_api, "POST", f"{base}/refresh") == {}


async def test_navigate_to_area_home_posts_home_sentinel(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """home=true navigates to the start screen and overrides area_id."""
    await setup_integration(hass, mock_api, config_entry)
    navigate_url = f"{BASE_URL}/api/integration/clients/{CLIENT_ID}/navigate_to_area"
    mock_api.post(navigate_url, payload={"ok": True})

    await hass.services.async_call(
        DOMAIN,
        "navigate_to_area",
        {"client_id": CLIENT_ID, "area_id": "area-kitchen", "home": True},
        blocking=True,
    )

    assert _posted_json(mock_api, "POST", navigate_url) == {"area_id": "__home__"}


async def test_navigate_to_area_requires_area_or_home(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Neither area_id nor home given fails cleanly before any request."""
    await setup_integration(hass, mock_api, config_entry)
    navigate_url = f"{BASE_URL}/api/integration/clients/{CLIENT_ID}/navigate_to_area"

    with pytest.raises(HomeAssistantError, match="Select an area"):
        await hass.services.async_call(
            DOMAIN, "navigate_to_area", {"client_id": CLIENT_ID}, blocking=True
        )
    assert ("POST", URL(navigate_url)) not in mock_api.requests


async def test_force_refresh_repolls_status(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """force_refresh triggers an immediate coordinator poll."""
    await setup_integration(hass, mock_api, config_entry)
    polls_before = _status_poll_count(mock_api)

    await hass.services.async_call(DOMAIN, "force_refresh", {}, blocking=True)
    await hass.async_block_till_done()

    assert _status_poll_count(mock_api) == polls_before + 1


# ── ID validation (path-traversal hardening) ─────────────────────────────


@pytest.mark.parametrize("bad_id", ["..", ".", "a/b", "../kick", "a b", ""])
async def test_services_reject_malformed_client_ids(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    bad_id: str,
) -> None:
    """IDs that could change the request path are rejected up front."""
    await setup_integration(hass, mock_api, config_entry)

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, "kick_client", {"client_id": bad_id}, blocking=True
        )
    # The malformed id was rejected up front, so no request reached the hub
    # (the external URL report at setup is no client command).
    assert not [
        key
        for key in mock_api.requests
        if key[0] == "POST" and not str(key[1]).endswith("/api/integration/ha-url")
    ]


# ── Version header on the wire ───────────────────────────────────────────


async def test_every_rest_request_carries_auth_and_version_headers(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """Full-stack check against a real local server.

    aioresponses cannot observe session-default headers, so this test runs
    the coordinator and a service call against an in-process aiohttp server
    and asserts every received request carried the Authorization and
    X-Hub-Integration-Version headers.
    """
    seen: list[tuple[str, str, dict[str, str]]] = []

    async def handler(request: web.Request) -> web.Response:
        seen.append((request.method, request.path, dict(request.headers)))
        if request.path == "/api/integration/status":
            return web.json_response(
                {
                    "hub": {
                        "version": "2026.12.0",
                        "instance_id": HUB_INSTANCE_ID,
                        "connected_clients": 0,
                    },
                    "area_count": 0,
                    "client_count": 0,
                    "guest_count": 0,
                    "areas": [],
                    "clients": [],
                    "guests": [],
                    "provisioning": [],
                    "ha_user_holds": [],
                    "pairing_active": False,
                    "pairing_remaining": 0,
                    "pairing_is_guest": False,
                }
            )
        return web.json_response({"ok": True, "cancelled": True})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id=ENTRY_UNIQUE_ID,
            data={
                CONF_HOST: "127.0.0.1",
                CONF_PORT: server.port,
                CONF_API_TOKEN: TEST_TOKEN,
                CONF_USE_SSL: False,
            },
        )
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED

        await hass.services.async_call(DOMAIN, "cancel_pairing", {}, blocking=True)
        await hass.async_block_till_done()

        assert ("GET", "/api/integration/status") in {(m, p) for m, p, _ in seen}
        assert ("POST", "/api/integration/pairing/cancel") in {
            (m, p) for m, p, _ in seen
        }
        for method, path, headers in seen:
            assert headers["Authorization"] == f"Bearer {TEST_TOKEN}", (method, path)
            assert headers["X-Hub-Integration-Version"] == MANIFEST_VERSION, (
                method,
                path,
            )

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        await server.close()


# ── Errors are never swallowed ──────────────────────────────

CLIENT_BASE = f"{BASE_URL}/api/integration/clients/{CLIENT_ID}"


@pytest.mark.parametrize(
    ("service", "data", "url"),
    [
        (
            "navigate_to_area",
            {"client_id": CLIENT_ID, "area_id": "area-kitchen"},
            f"{CLIENT_BASE}/navigate_to_area",
        ),
        (
            "show_entity",
            {"client_id": CLIENT_ID, "entity_id": "light.kitchen"},
            f"{CLIENT_BASE}/show_entity",
        ),
        (
            "display_notification",
            {"client_id": CLIENT_ID, "title": "Hi"},
            NOTIFY_URL,
        ),
        (
            "clear_notifications",
            {"client_id": CLIENT_ID},
            f"{CLIENT_BASE}/clear_notifications",
        ),
        ("refresh_client", {"client_id": CLIENT_ID}, f"{CLIENT_BASE}/refresh"),
        (
            "manage_client_tags",
            {"client_id": CLIENT_ID, "action": "set", "tags": ["x"]},
            f"{CLIENT_BASE}/tags",
        ),
        ("extend_guest", {"client_id": CLIENT_ID, "hours": 2}, f"{CLIENT_BASE}/extend"),
    ],
)
async def test_unknown_client_fails_the_service_call(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    service: str,
    data: dict[str, Any],
    url: str,
) -> None:
    """A 404 from the hub is a ServiceValidationError with its reason."""
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(url, status=404, payload={"detail": "Client not found"})

    with pytest.raises(ServiceValidationError, match=r"Client not found \(HTTP 404\)"):
        await hass.services.async_call(DOMAIN, service, data, blocking=True)


@pytest.mark.parametrize("status", [422, 500, 503])
async def test_other_add_on_errors_fail_the_service_call(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    status: int,
) -> None:
    """Every other failed answer raises HomeAssistantError (not a validation error)."""
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(
        f"{CLIENT_BASE}/refresh",
        status=status,
        payload={"detail": "Client is not connected via WebSocket"},
    )

    with pytest.raises(HomeAssistantError, match="not connected") as exc:
        await hass.services.async_call(
            DOMAIN, "refresh_client", {"client_id": CLIENT_ID}, blocking=True
        )
    assert not isinstance(exc.value, ServiceValidationError)


async def test_unreachable_add_on_fails_the_service_call(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """A connection error is reported, not logged and dropped."""
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(
        f"{CLIENT_BASE}/refresh", exception=aiohttp.ClientConnectionError("down")
    )

    with pytest.raises(HomeAssistantError, match="not reachable"):
        await hass.services.async_call(
            DOMAIN, "refresh_client", {"client_id": CLIENT_ID}, blocking=True
        )


async def test_area_switch_failure_is_a_clean_error(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """switch.turn_on raises HomeAssistantError, not the raw API error."""
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(
        f"{BASE_URL}/api/integration/areas/area-office/activate",
        status=404,
        payload={"detail": "Area not found"},
    )
    entity_id = er.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, f"{config_entry.entry_id}_hub_area_switch_area-office"
    )

    with pytest.raises(ServiceValidationError, match="Area not found"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": entity_id}, blocking=True
        )


async def test_force_refresh_without_entry_fails(hass: HomeAssistant) -> None:
    """Calling a service while no entry is loaded is an error, not a no-op."""
    from homeassistant.setup import async_setup_component

    # The integration is loaded (actions registered in async_setup), but no
    # config entry is set up.
    assert await async_setup_component(hass, DOMAIN, {})
    assert hass.services.has_service(DOMAIN, "force_refresh")
    with pytest.raises(ServiceValidationError, match="not set up or not loaded"):
        await hass.services.async_call(DOMAIN, "force_refresh", {}, blocking=True)
