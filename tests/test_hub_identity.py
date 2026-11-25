"""Which hub: the status names it (``hub.instance_id``).

The entry stores the id on first sight. A status with another id is another
hub (hub reinstalled without a backup, entry pointed at another host): the
poll fails before anything is reconciled, so no device, HA user or token is
removed, and a repair issue asks the admin to confirm it in reconfigure or
reauthenticate. Only the confirmation stores the new id; from then on the new
hub reconciles as usual.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from aioresponses import aioresponses
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from custom_components.halumar.const import CONF_API_TOKEN, CONF_HUB_INSTANCE_ID, DOMAIN
from custom_components.halumar.coordinator import other_hub_issue_id
from custom_components.halumar.push import HubPushClient

from ._ha import MockUser
from .common import CLIENT_ID, GUEST_ID, HUB_INSTANCE_ID
from .test_device_users import (
    _device_users,
    _poll,
    _provisioned,
    _settle,
    _user_of,
)

OTHER_HUB = "0b9e8d7c-6f5a-4e3d-9c2b-1a0f9e8d7c6b"


@pytest.fixture
def existing_owner(hass: HomeAssistant) -> MockUser:
    """A human owner exists, as in every real installation."""
    return MockUser(is_owner=True, name="Primary Owner").add_to_hass(hass)


def _issue(hass: HomeAssistant, entry_id: str) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, other_hub_issue_id(entry_id))


async def test_first_status_stores_the_hub_id(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    entry, _hub = await _provisioned(hass, mock_api, CLIENT_ID)
    assert entry.data[CONF_HUB_INSTANCE_ID] == HUB_INSTANCE_ID
    assert _issue(hass, entry.entry_id) is None


async def test_another_hub_removes_nothing_until_confirmed(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """Another hub with no devices: every device, user and token stays."""
    entry, hub = await _provisioned(hass, mock_api, CLIENT_ID, GUEST_ID)
    users = {u.id for u in await _device_users(hass)}
    tokens = {
        t.id for u in await _device_users(hass) for t in u.refresh_tokens.values()
    }
    dev_reg = dr.async_get(hass)
    devices = {d.id for d in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)}
    assert len(users) == 2 and len(devices) > 1

    hub.snapshot["hub"]["instance_id"] = OTHER_HUB
    hub.snapshot["clients"] = []
    hub.snapshot["guests"] = []
    await _poll(hass, entry)

    coordinator = entry.runtime_data
    assert coordinator.last_update_success is False
    assert coordinator.other_hub == OTHER_HUB
    assert {u.id for u in await _device_users(hass)} == users
    assert {
        t.id for u in await _device_users(hass) for t in u.refresh_tokens.values()
    } == tokens
    assert {
        d.id for d in dr.async_entries_for_config_entry(dev_reg, entry.entry_id)
    } == devices
    assert entry.data[CONF_HUB_INSTANCE_ID] == HUB_INSTANCE_ID
    issue = _issue(hass, entry.entry_id)
    assert issue is not None and issue.translation_key == "other_hub"

    # The admin confirms the new hub in reconfigure (same address).
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"host": entry.data["host"], "port": entry.data["port"], "use_ssl": False},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "other_hub"
    assert entry.data[CONF_HUB_INSTANCE_ID] == HUB_INSTANCE_ID
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await _settle(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "other_hub_confirmed"
    assert entry.data[CONF_HUB_INSTANCE_ID] == OTHER_HUB

    # Only now the new hub's list counts: its unknown devices go.
    assert entry.runtime_data.last_update_success is True
    assert _issue(hass, entry.entry_id) is None
    assert await _device_users(hass) == []


async def test_reauth_with_another_hub_asks_first(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    entry, hub = await _provisioned(hass, mock_api, CLIENT_ID)
    user = await _user_of(hass, entry, CLIENT_ID)
    hub.snapshot["hub"]["instance_id"] = OTHER_HUB

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_TOKEN: "new-token"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "other_hub"
    assert entry.data[CONF_API_TOKEN] != "new-token"
    assert await hass.auth.async_get_user(user.id) is not None

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await _settle(hass)
    assert result["reason"] == "other_hub_confirmed"
    assert entry.data[CONF_API_TOKEN] == "new-token"
    assert entry.data[CONF_HUB_INSTANCE_ID] == OTHER_HUB


async def test_reconfigure_to_the_same_hub_needs_no_confirmation(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    entry, _hub = await _provisioned(hass, mock_api, CLIENT_ID)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"host": entry.data["host"], "port": entry.data["port"], "use_ssl": False},
    )
    await _settle(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HUB_INSTANCE_ID] == HUB_INSTANCE_ID


@pytest.mark.parametrize("value", [None, "", "not-a-uuid", 7])
async def test_status_without_a_hub_id_is_a_failed_poll(
    hass: HomeAssistant,
    mock_api: aioresponses,
    existing_owner: MockUser,
    value: Any,
) -> None:
    entry, hub = await _provisioned(hass, mock_api, CLIENT_ID)
    users = {u.id for u in await _device_users(hass)}
    hub.snapshot["hub"]["instance_id"] = value
    hub.snapshot["clients"] = []
    await _poll(hass, entry)
    assert entry.runtime_data.last_update_success is False
    assert {u.id for u in await _device_users(hass)} == users
    assert entry.data[CONF_HUB_INSTANCE_ID] == HUB_INSTANCE_ID


class _Socket:
    def __init__(self, frames: list[str]) -> None:
        self.frames = frames
        self.sent: list[Any] = []

    async def receive(self) -> Any:
        import aiohttp  # noqa: PLC0415

        if not self.frames:
            return type("M", (), {"type": aiohttp.WSMsgType.CLOSED, "data": None})()
        return type(
            "M", (), {"type": aiohttp.WSMsgType.TEXT, "data": self.frames.pop(0)}
        )()

    async def send_json(self, data: Any) -> None:
        self.sent.append(data)


async def test_push_session_of_another_hub_applies_nothing(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """While the status names another hub, its push frames change nothing."""
    entry, hub = await _provisioned(hass, mock_api, CLIENT_ID)
    hub.snapshot["hub"]["instance_id"] = OTHER_HUB
    await _poll(hass, entry)
    coordinator = entry.runtime_data
    assert coordinator.other_hub == OTHER_HUB
    device_users = coordinator.device_users
    device_users.async_schedule_push = AsyncMock()  # type: ignore[method-assign]
    coordinator.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    socket = _Socket(
        [
            '{"type": "ping"}',
            f'{{"type": "deprovision_client", "data": {{"client_id": "{CLIENT_ID}"}}}}',
            '{"type": "enforcement_rules", "data": {"snapshots": [], '
            '"control_paths": {}, "removed": [], "complete": true}}',
            '{"type": "client_updated"}',
        ]
    )
    await HubPushClient(hass, coordinator)._pump(socket)  # noqa: SLF001
    await _settle(hass)
    assert socket.sent == [{"type": "pong"}]
    device_users.async_schedule_push.assert_not_called()
    coordinator.async_request_refresh.assert_not_called()
    assert len(await _device_users(hass)) == 1


async def test_nothing_goes_to_another_hub_until_confirmed(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """Events stay queued, the external URL is not reported, admin actions fail
    naming the repair, the push session does not connect; only the status poll
    still asks (that is how the original hub is noticed when it returns)."""
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.halumar.coordinator import OtherHubError
    from custom_components.halumar.enforcement import DATA_ENFORCEMENT
    from custom_components.halumar.external_url import ExternalUrlReporter

    entry, hub = await _provisioned(hass, mock_api, CLIENT_ID)
    hub.snapshot["hub"]["instance_id"] = OTHER_HUB
    await _poll(hass, entry)
    coordinator = entry.runtime_data
    assert coordinator.other_hub == OTHER_HUB
    before = {key for key, calls in mock_api.requests.items() for _ in calls}
    counts = {key: len(calls) for key, calls in mock_api.requests.items()}

    events = hass.data[DATA_ENFORCEMENT].events
    events.record("integration_started", version="test")
    queued = len(events._queue)  # noqa: SLF001
    await events.async_flush()
    assert len(events._queue) == queued  # noqa: SLF001

    assert await ExternalUrlReporter(hass, entry, coordinator).async_report() is False

    with pytest.raises(OtherHubError):
        await coordinator.post_client_token(CLIENT_ID, "u" * 32, "t", "bearer")
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(DOMAIN, "start_pairing", {}, blocking=True)
    assert err.value.translation_key == "other_hub_held"

    socket = AsyncMock()
    coordinator.session.ws_connect = socket  # type: ignore[method-assign]
    await HubPushClient(hass, coordinator)._run_one_connection()  # noqa: SLF001
    socket.assert_not_called()

    # Only the status poll went out meanwhile.
    for key, calls in mock_api.requests.items():
        if len(calls) != counts.get(key, 0):
            assert key[1].path == "/api/integration/status", key
    assert before

    # The original hub is back: everything flows again, the queue is delivered.
    hub.snapshot["hub"]["instance_id"] = HUB_INSTANCE_ID
    mock_api.post(
        f"{coordinator.base_url}/api/integration/events", payload={}, repeat=True
    )
    await _poll(hass, entry)
    assert coordinator.other_hub is None and _issue(hass, entry.entry_id) is None
    await events.async_flush()
    assert len(events._queue) == 0  # noqa: SLF001
