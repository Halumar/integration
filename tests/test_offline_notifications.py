"""Notifications while the hub is unreachable (offline_notifications.py).

When display_notification / clear_notifications / ack_notifications cannot
reach the hub (connection error, timeout, 502-504; never on a 4xx), the
integration writes the change into the device's notification sensor, keeps it
in a persistent queue and hands it over once the hub answers again, in order
and without duplicates (stable ids).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import aiohttp
import pytest
from aioresponses import CallbackResult, aioresponses
from yarl import URL

from custom_components.halumar.const import DOMAIN
from custom_components.halumar.offline_notifications import (
    DATA_OFFLINE_NOTIFICATIONS,
    MAX_QUEUED_PER_CLIENT,
    STORAGE_KEY,
    OfflineNotifications,
    broadcast_notification_id,
    hub_unreachable,
    notification_sensor_id,
)
from custom_components.halumar.coordinator import HubApiError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant, valid_entity_id
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.util import dt as dt_util

from ._ha import MockConfigEntry, MockUser
from .common import (
    BASE_URL,
    CLIENT_ID,
    GUEST_ID,
    STATUS_URL,
    make_status_payload,
    setup_integration,
)

NOTIFY_URL = f"{BASE_URL}/api/integration/notify"
HANDOVER_URL = f"{BASE_URL}/api/integration/notifications/handover"
ACK_URL = f"{BASE_URL}/api/integration/notifications/ack"
CLEAR_URL = f"{BASE_URL}/api/integration/clients/{CLIENT_ID}/clear_notifications"

# The sensor the hub wrote for "Wall Tablet" (make_status_payload).
HUB_SENSOR = f"sensor.{DOMAIN}_client_wall_tablet_11111111_notifications"
GUEST_SENSOR = f"sensor.{DOMAIN}_client_guest_phone_22222222_notifications"
HUB_ITEM = {
    "notification_id": "aaaaaaaa-0000-4000-8000-000000000001",
    "title": "From the hub",
    "message": "",
    "notification_type": "default",
    "popup": False,
    "popup_mode": "none",
    "scheduled_at": None,
    "priority": 50,
    "color": "none",
    "action_type": "none",
    "action_target": None,
    "show_time": True,
    "created_at": "2026-11-19T08:00:00+00:00",
    "status": "pending",
}


def _requests(mock_api: aioresponses, url: str) -> list[dict[str, Any]]:
    return [c.kwargs["json"] for c in mock_api.requests.get(("POST", URL(url)), [])]


def _queue(hass: HomeAssistant) -> OfflineNotifications:
    return hass.data[DATA_OFFLINE_NOTIFICATIONS]


def _hub_sensor(hass: HomeAssistant) -> None:
    hass.states.async_set(
        HUB_SENSOR,
        "1",
        {"count": 1, "notifications": [dict(HUB_ITEM)], "friendly_name": "Hub name"},
    )


async def _notify_down(
    hass: HomeAssistant,
    mock_api: aioresponses,
    data: dict[str, Any],
    exception: BaseException | None = None,
) -> None:
    mock_api.post(NOTIFY_URL, exception=exception or aiohttp.ClientConnectionError())
    await hass.services.async_call(DOMAIN, "display_notification", data, blocking=True)


async def _hub_back(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


def _items(hass: HomeAssistant, entity_id: str = HUB_SENSOR) -> list[dict[str, Any]]:
    state = hass.states.get(entity_id)
    assert state is not None
    return list(state.attributes["notifications"])


# ── Fallback on display_notification ─────────────────────────────────────


async def test_unreachable_hub_writes_the_sensor_and_hands_over(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry)
    _hub_sensor(hass)

    await _notify_down(
        hass,
        mock_api,
        {"client_id": CLIENT_ID, "title": "Waschmaschine", "color": "green"},
    )

    sent = _requests(mock_api, NOTIFY_URL)[0]
    state = hass.states.get(HUB_SENSOR)
    assert state is not None
    assert state.state == "2"
    assert state.attributes["friendly_name"] == "Hub name"
    items = state.attributes["notifications"]
    queued = items[0]
    # The id the hub did not answer to is the id of the queued notification.
    assert queued["notification_id"] == sent["notification_id"]
    assert queued["title"] == "Waschmaschine"
    assert queued["color"] == "green"
    assert queued["status"] == "pending"
    assert queued["popup"] is False
    assert items[1]["notification_id"] == HUB_ITEM["notification_id"]
    assert state.attributes["pending_handover"] == [sent["notification_id"]]
    assert _queue(hass).has_ops

    # The hub answers again: the next poll hands the queue over.
    mock_api.post(
        HANDOVER_URL,
        payload={
            "success": True,
            "created": [sent["notification_id"]],
            "duplicates": [],
            "skipped": [],
        },
    )
    await _hub_back(hass, config_entry)
    body = _requests(mock_api, HANDOVER_URL)[0]
    assert body["notifications"] == [
        {
            "client_id": CLIENT_ID,
            "notification_id": sent["notification_id"],
            "created_at": queued["created_at"],
            "status": "pending",
            "title": "Waschmaschine",
            "message": "",
            "notification_type": "default",
            "color": "green",
        }
    ]
    assert not _queue(hass).has_ops


@pytest.mark.parametrize(
    "exception",
    [
        TimeoutError(),
        aiohttp.ServerDisconnectedError(),
    ],
)
async def test_timeout_and_connect_errors_fall_back(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    exception: BaseException,
) -> None:
    await setup_integration(hass, mock_api, config_entry)
    await _notify_down(
        hass, mock_api, {"client_id": CLIENT_ID, "title": "x"}, exception
    )
    # No hub sensor yet: the id is built like the hub builds it.
    assert hass.states.get(HUB_SENSOR) is not None
    assert len(_queue(hass).ops) == 1


async def test_gateway_error_falls_back_but_client_errors_do_not(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(NOTIFY_URL, status=502, body="Bad Gateway")
    await hass.services.async_call(
        DOMAIN,
        "display_notification",
        {"client_id": CLIENT_ID, "title": "x"},
        blocking=True,
    )
    assert len(_queue(hass).ops) == 1

    # A 4xx is the hub's answer, not an outage: no fallback.
    queue = _queue(hass)
    mock_api.post(
        HANDOVER_URL, payload={"created": [], "duplicates": [], "skipped": []}
    )
    assert await queue.async_flush(config_entry.runtime_data)
    mock_api.post(NOTIFY_URL, status=404, payload={"detail": "Client not found"})
    with pytest.raises(ServiceValidationError, match="Client not found"):
        await hass.services.async_call(
            DOMAIN,
            "display_notification",
            {"client_id": CLIENT_ID, "title": "x"},
            blocking=True,
        )
    assert not queue.has_ops


async def test_broadcast_reaches_every_active_device(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry)
    await _notify_down(hass, mock_api, {"client_id": "all", "message": "Alle"})

    # The broadcast carries a broadcast id, each device's id is
    # derived from it exactly like the hub derives it.
    broadcast_id = _requests(mock_api, NOTIFY_URL)[0]["notification_id"]
    ids = {op["client_id"]: op["notification_id"] for op in _queue(hass).ops}
    assert ids == {
        CLIENT_ID: broadcast_notification_id(broadcast_id, CLIENT_ID),
        GUEST_ID: broadcast_notification_id(broadcast_id, GUEST_ID),
    }
    assert len(set(ids.values())) == 2
    assert _items(hass)[0]["message"] == "Alle"
    assert _items(hass, GUEST_SENSOR)[0]["notification_id"] == ids[GUEST_ID]


async def test_timed_out_broadcast_is_handed_over_as_duplicates(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """The hub took the broadcast but the answer timed out."""
    await setup_integration(hass, mock_api, config_entry)
    await _notify_down(
        hass, mock_api, {"client_id": "all", "message": "Alle"}, TimeoutError()
    )
    broadcast_id = _requests(mock_api, NOTIFY_URL)[0]["notification_id"]
    derived = [
        broadcast_notification_id(broadcast_id, CLIENT_ID),
        broadcast_notification_id(broadcast_id, GUEST_ID),
    ]
    # The hub already has both derived ids: it reports them as duplicates.
    mock_api.post(
        HANDOVER_URL,
        payload={"success": True, "created": [], "duplicates": derived, "skipped": []},
    )
    await _hub_back(hass, config_entry)
    handed = [
        n["notification_id"]
        for n in _requests(mock_api, HANDOVER_URL)[0]["notifications"]
    ]
    assert sorted(handed) == sorted(derived)
    assert not _queue(hass).has_ops


def test_broadcast_id_matches_the_contract_example() -> None:
    assert (
        broadcast_notification_id(
            "6f1c2a4e-8b3d-4c5e-9f70-1a2b3c4d5e6f",
            "3f2b8c1e-5d4a-4e6f-9a7b-1c2d3e4f5a6b",
        )
        == "e58ba18c-8fa7-5d79-87b1-4e45eb94b7b9"
    )


async def test_fallback_validates_like_the_hub(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry)
    # Paired just before the outage: not in the last snapshot, but the hub
    # already wrote its sensor.
    fresh = "44444444-4444-4444-4444-444444444444"
    fresh_sensor = f"sensor.{DOMAIN}_client_new_panel_44444444_notifications"
    hass.states.async_set(fresh_sensor, "0", {"count": 0, "notifications": []})
    await _notify_down(hass, mock_api, {"client_id": fresh, "title": "hi"})
    assert _items(hass, fresh_sensor)[0]["title"] == "hi"
    with pytest.raises(ServiceValidationError, match="does not know the device"):
        await _notify_down(
            hass,
            mock_api,
            {"client_id": "33333333-3333-3333-3333-333333333333", "title": "x"},
        )
    with pytest.raises(ServiceValidationError, match="direct requires"):
        await _notify_down(
            hass,
            mock_api,
            {"client_id": CLIENT_ID, "title": "x", "popup_mode": "direct"},
        )
    assert len(_queue(hass).ops) == 1


async def test_a_new_notification_never_overtakes_the_queue(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry)
    await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": "first"})

    # Hub back: the queue goes first, then the new notification.
    order: list[str] = []

    def _record(url: URL, **kwargs: Any) -> None:
        order.append(str(url))

    mock_api.post(
        HANDOVER_URL,
        payload={"created": [], "duplicates": [], "skipped": []},
        callback=_record,
    )
    mock_api.post(
        NOTIFY_URL, payload={"notification": {}, "ws_sent": True}, callback=_record
    )
    await hass.services.async_call(
        DOMAIN,
        "display_notification",
        {"client_id": CLIENT_ID, "title": "second"},
        blocking=True,
    )
    assert order == [HANDOVER_URL, NOTIFY_URL]

    # Hub still down while something is queued: the new one is queued too.
    await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": "third"})
    mock_api.post(HANDOVER_URL, exception=aiohttp.ClientConnectionError())
    await hass.services.async_call(
        DOMAIN,
        "display_notification",
        {"client_id": CLIENT_ID, "title": "fourth"},
        blocking=True,
    )
    assert [op["fields"]["title"] for op in _queue(hass).ops] == ["third", "fourth"]


# ── Acknowledgements and clear while the hub is down ─────────────────────


async def test_ack_while_down_is_confirmed_and_handed_over(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry)
    _hub_sensor(hass)
    await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": "queued"})
    queued_id = _queue(hass).ops[0]["notification_id"]

    mock_api.post(HANDOVER_URL, exception=aiohttp.ClientConnectionError())
    await hass.services.async_call(
        DOMAIN,
        "ack_notifications",
        {
            "client_id": CLIENT_ID,
            "notification_ids": [queued_id, HUB_ITEM["notification_id"], "unknown-1"],
        },
        blocking=True,
    )
    statuses = {i["notification_id"]: i["status"] for i in _items(hass)}
    assert statuses == {
        queued_id: "delivered",
        HUB_ITEM["notification_id"]: "delivered",
    }
    ops = _queue(hass).ops
    assert [op["op"] for op in ops] == ["notify", "ack"]
    assert ops[0]["status"] == "delivered"
    assert ops[1]["notification_ids"] == [HUB_ITEM["notification_id"]]

    # Acked again: nothing pending any more, nothing new queued.
    mock_api.post(HANDOVER_URL, exception=aiohttp.ClientConnectionError())
    await hass.services.async_call(
        DOMAIN,
        "ack_notifications",
        {"client_id": CLIENT_ID, "notification_ids": [queued_id]},
        blocking=True,
    )
    assert len(_queue(hass).ops) == 2

    mock_api.post(
        HANDOVER_URL, payload={"created": [queued_id], "duplicates": [], "skipped": []}
    )
    mock_api.post(
        ACK_URL, payload={"acked": [HUB_ITEM["notification_id"]], "skipped": []}
    )
    await _hub_back(hass, config_entry)
    assert (
        _requests(mock_api, HANDOVER_URL)[-1]["notifications"][0]["status"]
        == "delivered"
    )
    assert _requests(mock_api, ACK_URL) == [
        {"client_id": CLIENT_ID, "notification_ids": [HUB_ITEM["notification_id"]]}
    ]
    assert not _queue(hass).has_ops


async def test_ack_by_the_device_user_while_the_entry_waits_for_the_hub(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    hass_storage: dict[str, Any],
) -> None:
    """HA restarted while the hub is down: the entry retries its setup."""
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "key": STORAGE_KEY,
        "data": {
            "ops": [],
            "clients": {CLIENT_ID: {"name": "Wall Tablet", "active": True}},
        },
    }
    device_user = MockUser(name="device").add_to_hass(hass)
    hass_storage[f"{DOMAIN}.device_users.{config_entry.entry_id}"] = {
        "version": 1,
        "key": f"{DOMAIN}.device_users.{config_entry.entry_id}",
        "data": {"clients": {CLIENT_ID: {"user_id": device_user.id}}},
    }
    mock_api.get(STATUS_URL, exception=aiohttp.ClientConnectionError(), repeat=True)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY

    await hass.services.async_call(
        DOMAIN,
        "display_notification",
        {"client_id": CLIENT_ID, "title": "x"},
        blocking=True,
    )
    queued_id = _queue(hass).ops[0]["notification_id"]
    assert _items(hass)[0]["notification_id"] == queued_id

    await hass.services.async_call(
        DOMAIN,
        "ack_notifications",
        {"client_id": CLIENT_ID, "notification_ids": [queued_id]},
        blocking=True,
        context=Context(user_id=device_user.id),
    )
    assert _items(hass)[0]["status"] == "delivered"

    stranger = MockUser(name="stranger").add_to_hass(hass)
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "ack_notifications",
            {"client_id": CLIENT_ID, "notification_ids": [queued_id]},
            blocking=True,
            context=Context(user_id=stranger.id),
        )


async def test_clear_while_down_drops_queued_and_is_handed_over(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry)
    _hub_sensor(hass)
    await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": "gone"})

    mock_api.post(HANDOVER_URL, exception=aiohttp.ClientConnectionError())
    await hass.services.async_call(
        DOMAIN, "clear_notifications", {"client_id": CLIENT_ID}, blocking=True
    )
    state = hass.states.get(HUB_SENSOR)
    assert state is not None
    assert state.state == "0"
    assert state.attributes["notifications"] == []
    assert "pending_handover" not in state.attributes
    assert [op["op"] for op in _queue(hass).ops] == ["clear"]

    mock_api.post(CLEAR_URL, payload={"success": True, "cleared": 1})
    await _hub_back(hass, config_entry)
    assert _requests(mock_api, CLEAR_URL) == [{}]
    assert not _queue(hass).has_ops


async def test_clear_for_an_unreachable_hub_needs_a_known_device(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry)
    other = "33333333-3333-3333-3333-333333333333"
    mock_api.post(
        f"{BASE_URL}/api/integration/clients/{other}/clear_notifications",
        exception=aiohttp.ClientConnectionError(),
    )
    with pytest.raises(ServiceValidationError, match="does not know the device"):
        await hass.services.async_call(
            DOMAIN, "clear_notifications", {"client_id": other}, blocking=True
        )


# ── Watches (as on the hub: no hub channel, their phone shows them) ──────

PHONE_ID = "55555555-5555-4555-8555-555555555555"
WATCH_ID = "66666666-6666-4666-8666-666666666666"
PHONE_SENSOR = f"sensor.{DOMAIN}_client_iphone_kind_55555555_notifications"
WATCH_SENSOR = f"sensor.{DOMAIN}_client_watch_kind_66666666_notifications"


def _with_watch(*, phone_active: bool = True) -> dict[str, Any]:
    """Status with an iPhone and the watch it set up."""
    payload = make_status_payload()
    common = {
        "is_connected": False,
        "is_guest": False,
        "tags": [],
        "notification_ids": [],
        "notification_count": 0,
        "pending_deletion": False,
    }
    payload["clients"] += [
        {
            **common,
            "id": PHONE_ID,
            "name": "iPhone Kind",
            "slug": "iphone-kind",
            "is_active": phone_active,
            "platform": "ios",
            "set_up_by": None,
        },
        {
            **common,
            "id": WATCH_ID,
            "name": "Watch Kind",
            "slug": "watch-kind",
            "is_active": True,
            "platform": "watchos",
            "set_up_by": {
                "client_id": PHONE_ID,
                "name": "iPhone Kind",
                "method": "onboard",
            },
        },
    ]
    return payload


async def test_broadcast_while_down_leaves_watches_out(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Regression: hub down + "all" -> exactly one notification
    on the phone (the hub forwards a watch's item to its phone on handover, so
    an item for the watch was a second copy)."""
    await setup_integration(hass, mock_api, config_entry, _with_watch())
    await _notify_down(hass, mock_api, {"client_id": "all", "message": "Alle"})

    broadcast_id = _requests(mock_api, NOTIFY_URL)[0]["notification_id"]
    targets = [op["client_id"] for op in _queue(hass).ops]
    assert sorted(targets) == sorted([CLIENT_ID, GUEST_ID, PHONE_ID])
    assert WATCH_ID not in targets
    assert hass.states.get(WATCH_SENSOR) is None
    phone_items = _items(hass, PHONE_SENSOR)
    assert len(phone_items) == 1
    assert phone_items[0]["notification_id"] == broadcast_notification_id(
        broadcast_id, PHONE_ID
    )

    mock_api.post(
        HANDOVER_URL, payload={"created": [], "duplicates": [], "skipped": []}
    )
    await _hub_back(hass, config_entry)
    handed = _requests(mock_api, HANDOVER_URL)[0]["notifications"]
    assert [n["client_id"] for n in handed].count(PHONE_ID) == 1
    assert WATCH_ID not in {n["client_id"] for n in handed}
    assert not _queue(hass).has_ops


async def test_notification_for_a_watch_while_down_goes_to_its_phone(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry, _with_watch())
    await _notify_down(hass, mock_api, {"client_id": WATCH_ID, "title": "Tür"})

    sent_id = _requests(mock_api, NOTIFY_URL)[0]["notification_id"]
    ops = _queue(hass).ops
    # The id the hub would have stored under the phone: a call it took before
    # the answer timed out is handed over as a duplicate, not a second copy.
    assert [(op["client_id"], op["notification_id"]) for op in ops] == [
        (PHONE_ID, sent_id)
    ]
    assert [i["title"] for i in _items(hass, PHONE_SENSOR)] == ["Tür"]
    assert hass.states.get(WATCH_SENSOR) is None

    mock_api.post(
        HANDOVER_URL, payload={"created": [], "duplicates": [sent_id], "skipped": []}
    )
    await _hub_back(hass, config_entry)
    handed = _requests(mock_api, HANDOVER_URL)[0]["notifications"]
    assert [(n["client_id"], n["notification_id"]) for n in handed] == [
        (PHONE_ID, sent_id)
    ]
    assert not _queue(hass).has_ops


async def test_watch_without_its_phone_fails_while_down(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Like the hub's 422: nothing is stored for a watch whose phone is gone."""
    await setup_integration(
        hass, mock_api, config_entry, _with_watch(phone_active=False)
    )
    with pytest.raises(ServiceValidationError, match="no longer paired"):
        await _notify_down(hass, mock_api, {"client_id": WATCH_ID, "title": "x"})
    assert not _queue(hass).has_ops
    assert hass.states.get(WATCH_SENSOR) is None


async def test_watch_is_remembered_across_a_restart(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """The fallback works after an HA restart while the hub is down."""
    queue = OfflineNotifications(hass)
    queue.remember_clients(_with_watch())
    assert queue.recipient(WATCH_ID) == PHONE_ID
    assert queue.recipient(PHONE_ID) == PHONE_ID
    assert WATCH_ID not in queue.active_client_ids()
    await hass.async_block_till_done()
    await queue._store.async_save({"ops": [], "clients": queue._clients})  # noqa: SLF001

    restored = OfflineNotifications(hass)
    await restored.async_load()
    assert restored.is_watch(WATCH_ID)
    assert restored.recipient(WATCH_ID) == PHONE_ID
    # A snapshot of an older hub without platform: no device counts as a watch.
    restored.remember_clients(make_status_payload())
    assert restored.recipient(CLIENT_ID) == CLIENT_ID
    assert sorted(restored.active_client_ids()) == sorted([CLIENT_ID, GUEST_ID])


# ── Handover edge cases ──────────────────────────────────────────────────


async def test_refused_items_are_dropped_one_by_one(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry)
    await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": "ok"})
    await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": "bad"})
    bad_id = _queue(hass).ops[1]["notification_id"]

    # The batch is refused; item by item only the bad one is.
    mock_api.post(HANDOVER_URL, status=422, payload={"detail": "invalid"})
    mock_api.post(
        HANDOVER_URL, payload={"created": ["x"], "duplicates": [], "skipped": []}
    )
    mock_api.post(HANDOVER_URL, status=422, payload={"detail": "invalid"})
    await _hub_back(hass, config_entry)
    bodies = _requests(mock_api, HANDOVER_URL)
    # (The first, single-item attempt came with the second notification, while
    # the hub was still down.)
    assert [len(b["notifications"]) for b in bodies] == [1, 2, 1, 1]
    assert not _queue(hass).has_ops
    assert bad_id not in {i["notification_id"] for i in _items(hass)}


async def test_server_error_keeps_the_queue(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry)
    await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": "x"})
    mock_api.post(HANDOVER_URL, status=500, body="boom")
    await _hub_back(hass, config_entry)
    assert len(_queue(hass).ops) == 1


@pytest.mark.parametrize("status", [401, 403, 408, 429])
async def test_token_or_rate_limit_answer_pauses_the_handover(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    status: int,
) -> None:
    """A rejected token or a rate limit is not a refusal of the item."""
    await setup_integration(hass, mock_api, config_entry)
    for title in ("one", "two"):
        await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": title})
    queue = _queue(hass)
    assert len(queue.ops) == 2
    answers = [status]

    def handover(url: URL, **kwargs: Any) -> CallbackResult:
        if answers[0] != 200:
            return CallbackResult(status=answers[0], payload={"detail": "no"})
        n = len(kwargs["json"]["notifications"])
        return CallbackResult(payload={"created": ["x"] * n, "duplicates": []})

    mock_api.post(HANDOVER_URL, callback=handover, repeat=True)
    assert await queue.async_flush(config_entry.runtime_data) is False
    assert [op["fields"]["title"] for op in queue.ops] == ["one", "two"]
    assert {"one", "two"} <= {i["title"] for i in _items(hass)}

    # The next answer of the hub takes both, in one batch.
    answers[0] = 200
    assert await queue.async_flush(config_entry.runtime_data) is True
    assert not queue.has_ops
    assert len(_requests(mock_api, HANDOVER_URL)[-1]["notifications"]) == 2


@pytest.mark.parametrize("status", [400, 404, 409, 422])
async def test_refusal_of_a_single_item_drops_it(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    status: int,
) -> None:
    await setup_integration(hass, mock_api, config_entry)
    await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": "bad"})
    mock_api.post(HANDOVER_URL, status=status, payload={"detail": "no"}, repeat=True)
    assert await _queue(hass).async_flush(config_entry.runtime_data) is True
    assert not _queue(hass).has_ops
    assert "bad" not in {i["title"] for i in _items(hass)}


@pytest.mark.parametrize("status", [401, 429])
async def test_queued_clear_waits_for_a_token_or_rate_limit(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    status: int,
) -> None:
    await setup_integration(hass, mock_api, config_entry)
    _hub_sensor(hass)
    mock_api.post(CLEAR_URL, exception=aiohttp.ClientConnectionError())
    await hass.services.async_call(
        DOMAIN, "clear_notifications", {"client_id": CLIENT_ID}, blocking=True
    )
    queue = _queue(hass)
    assert [op["op"] for op in queue.ops] == ["clear"]
    mock_api.post(CLEAR_URL, status=status, payload={"detail": "no"}, repeat=True)
    assert await queue.async_flush(config_entry.runtime_data) is False
    assert [op["op"] for op in queue.ops] == ["clear"]


async def test_another_hub_held_sends_and_drops_nothing(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """While the status names another hub, the queue neither goes out nor
    shrinks; once the hub is confirmed it is handed over."""
    await setup_integration(hass, mock_api, config_entry)
    await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": "kept"})
    coordinator = config_entry.runtime_data
    coordinator.other_hub = "0b9e8d7c-6f5a-4e3d-9c2b-1a0f9e8d7c6b"
    mock_api.post(
        HANDOVER_URL, payload={"created": ["a"], "duplicates": [], "skipped": []}
    )
    queue = _queue(hass)
    assert await queue.async_flush(coordinator) is False
    assert len(queue.ops) == 1
    assert _requests(mock_api, HANDOVER_URL) == []

    coordinator.other_hub = None
    assert await queue.async_flush(coordinator) is True
    assert not queue.has_ops
    assert len(_requests(mock_api, HANDOVER_URL)) == 1


async def test_batch_too_large_is_handed_over_item_by_item(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """A batch can exceed the hub's body limit (413), one item cannot: the
    handover goes on one by one; a single item answered 413 is dropped."""
    await setup_integration(hass, mock_api, config_entry)
    for title in ("one", "two", "huge", "three"):
        await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": title})

    def handover(url: URL, **kwargs: Any) -> CallbackResult:
        items = kwargs["json"]["notifications"]
        if len(items) > 1 or items[0]["title"] == "huge":
            return CallbackResult(status=413, payload={"detail": "too large"})
        return CallbackResult(payload={"created": ["x"], "duplicates": []})

    mock_api.post(HANDOVER_URL, callback=handover, repeat=True)
    queue = _queue(hass)
    assert await queue.async_flush(config_entry.runtime_data) is True
    assert not queue.ops
    assert "huge" not in {i["title"] for i in _items(hass)}


async def test_an_item_the_hub_keeps_failing_is_set_aside(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One item the hub answers 500 to every time pauses the handover until it
    has failed SET_ASIDE_AFTER times over SET_ASIDE_AFTER_TIME; then it is no
    longer sent but stays on the device, and the rest goes through."""
    from custom_components.halumar import offline_notifications as module

    monkeypatch.setattr(module, "SET_ASIDE_AFTER_TIME", timedelta(0))
    await setup_integration(hass, mock_api, config_entry)
    for title in ("poison", "good"):
        await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": title})

    def handover(url: URL, **kwargs: Any) -> CallbackResult:
        titles = [n["title"] for n in kwargs["json"]["notifications"]]
        if "poison" in titles:
            return CallbackResult(status=500, payload={"detail": "boom"})
        return CallbackResult(payload={"created": ["x"], "duplicates": []})

    mock_api.post(HANDOVER_URL, callback=handover, repeat=True)
    queue = _queue(hass)
    coordinator = config_entry.runtime_data
    for _ in range(module.SET_ASIDE_AFTER - 1):
        assert await queue.async_flush(coordinator) is False
        assert [op["fields"]["title"] for op in queue.ops] == ["poison", "good"]
    assert "no longer sent" not in caplog.text
    assert await queue.async_flush(coordinator) is True
    assert [op["fields"]["title"] for op in queue.ops] == ["poison"]
    assert not queue.has_ops
    assert "HTTP 500" in caplog.text and "no longer sent" in caplog.text
    state = hass.states.get(HUB_SENSOR)
    assert state is not None
    assert "poison" in {i["title"] for i in state.attributes["notifications"]}
    assert queue.ops[0]["notification_id"] in state.attributes["pending_handover"]
    # It is not sent again; a new notification goes straight to the hub.
    sent = len(_requests(mock_api, HANDOVER_URL))
    assert await queue.async_flush(coordinator) is True
    assert len(_requests(mock_api, HANDOVER_URL)) == sent


async def test_set_aside_items_get_one_more_try_after_the_hub_recovers(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hub that failed writes for a while set items aside; once it takes a
    request again they are sent once more. The same answer again sets an
    item aside for good (no ping-pong)."""
    from custom_components.halumar import offline_notifications as module

    monkeypatch.setattr(module, "SET_ASIDE_AFTER_TIME", timedelta(0))
    monkeypatch.setattr(module, "SET_ASIDE_AFTER", 1)
    await setup_integration(hass, mock_api, config_entry)
    for title in ("a", "bad", "c"):
        await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": title})
    broken = [True]

    def handover(url: URL, **kwargs: Any) -> CallbackResult:
        titles = [n["title"] for n in kwargs["json"]["notifications"]]
        if broken[0] or "bad" in titles:
            return CallbackResult(status=500, payload={"detail": "boom"})
        return CallbackResult(payload={"created": titles, "duplicates": []})

    mock_api.post(HANDOVER_URL, callback=handover, repeat=True)
    queue = _queue(hass)
    coordinator = config_entry.runtime_data
    assert await queue.async_flush(coordinator) is True  # all three set aside
    assert all(op.get("set_aside") for op in queue.ops)
    broken[0] = False
    await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": "new"})
    assert await queue.async_flush(coordinator) is True
    assert [(op["fields"]["title"], bool(op.get("set_aside"))) for op in queue.ops] == [
        ("bad", True)
    ]
    sent = len(_requests(mock_api, HANDOVER_URL))
    assert await queue.async_flush(coordinator) is True
    assert len(_requests(mock_api, HANDOVER_URL)) == sent


async def test_only_set_aside_items_left_are_probed_after_a_while(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    monkeypatch: pytest.MonkeyPatch,
    freezer: Any,
) -> None:
    """Everything was set aside while the hub failed writes and nothing new
    comes: after SET_ASIDE_AFTER_TIME one item is sent as a probe; once the
    hub takes it, the others are handed over too."""
    from custom_components.halumar import offline_notifications as module

    monkeypatch.setattr(module, "SET_ASIDE_AFTER", 1)
    monkeypatch.setattr(module, "SET_ASIDE_AFTER_TIME", timedelta(0))
    await setup_integration(hass, mock_api, config_entry)
    for title in ("a", "b", "c"):
        await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": title})
    broken = [True]
    seen: list[list[str]] = []

    def handover(url: URL, **kwargs: Any) -> CallbackResult:
        titles = [n["title"] for n in kwargs["json"]["notifications"]]
        seen.append(titles)
        if broken[0]:
            return CallbackResult(status=500, payload={"detail": "boom"})
        return CallbackResult(payload={"created": titles, "duplicates": []})

    mock_api.post(HANDOVER_URL, callback=handover, repeat=True)
    queue = _queue(hass)
    coordinator = config_entry.runtime_data
    assert await queue.async_flush(coordinator) is True
    assert all(op.get("set_aside") for op in queue.ops)
    monkeypatch.setattr(module, "SET_ASIDE_AFTER_TIME", timedelta(minutes=10))
    seen.clear()
    # Not due yet: nothing is sent.
    await queue.async_flush(coordinator)
    assert seen == []
    # Due, hub still failing: one probe, set aside again.
    freezer.tick(timedelta(minutes=11))
    await queue.async_flush(coordinator)
    assert seen == [["a"]]
    assert all(op.get("set_aside") for op in queue.ops)
    # Due again, hub recovered: probe taken, the rest follows.
    broken[0] = False
    freezer.tick(timedelta(minutes=11))
    seen.clear()
    await queue.async_flush(coordinator)
    assert queue.ops == []
    assert sorted(t for batch in seen for t in batch) == ["a", "b", "c"]


def _set_aside(hass: HomeAssistant) -> dict[str, Any]:
    """The only queued notification, set aside long enough to be probed."""
    queue = _queue(hass)
    op = queue._ops[0]  # noqa: SLF001
    old = (dt_util.utcnow() - timedelta(minutes=11)).isoformat()
    op["set_aside"] = {"status": 500, "at": old}
    assert not queue.has_ops
    return op


async def test_a_direct_clear_drops_set_aside_notifications(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """A probe after a clear the hub took must not bring the notification
    back (as pending, with its popup)."""
    await setup_integration(hass, mock_api, config_entry)
    await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": "X"})
    _set_aside(hass)
    mock_api.post(CLEAR_URL, payload={"success": True}, repeat=True)
    await hass.services.async_call(
        DOMAIN, "clear_notifications", {"client_id": CLIENT_ID}, blocking=True
    )
    assert _queue(hass).ops == []
    assert "X" not in {i["title"] for i in _items(hass)}
    mock_api.post(
        HANDOVER_URL, payload={"created": ["x"], "duplicates": []}, repeat=True
    )
    await _queue(hass).async_flush(config_entry.runtime_data)
    assert _requests(mock_api, HANDOVER_URL) == []


async def test_a_direct_ack_hands_set_aside_notifications_over_as_delivered(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry)
    await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": "X"})
    await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": "Y"})
    queue = _queue(hass)
    first, second = queue._ops  # noqa: SLF001
    old = (dt_util.utcnow() - timedelta(minutes=11)).isoformat()
    first["set_aside"] = {"status": 500, "at": old}
    second["set_aside"] = {"status": 500, "at": old}
    mock_api.post(ACK_URL, payload={"acked": [], "skipped": []}, repeat=True)
    await hass.services.async_call(
        DOMAIN,
        "ack_notifications",
        {"client_id": CLIENT_ID, "notification_ids": [first["notification_id"]]},
        blocking=True,
    )
    statuses = {i["title"]: i["status"] for i in _items(hass)}
    assert statuses["X"] == "delivered" and statuses["Y"] == "pending"
    mock_api.post(
        HANDOVER_URL, payload={"created": ["x"], "duplicates": []}, repeat=True
    )
    before = len(_requests(mock_api, HANDOVER_URL))
    await queue.async_flush(config_entry.runtime_data)  # probe takes X
    handed = [
        (n["title"], n["status"])
        for body in _requests(mock_api, HANDOVER_URL)[before:]
        for n in body["notifications"]
    ]
    assert handed[0] == ("X", "delivered")
    assert ("Y", "pending") in handed


@pytest.mark.parametrize("status", [502, 503, 504])
async def test_a_proxy_answer_pauses_and_counts_nothing(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    status: int,
) -> None:
    await setup_integration(hass, mock_api, config_entry)
    await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": "a"})
    mock_api.post(HANDOVER_URL, status=status, body="Bad Gateway", repeat=True)
    queue = _queue(hass)
    assert await queue.async_flush(config_entry.runtime_data) is False
    assert "failures" not in queue.ops[0]


async def test_a_short_server_error_sets_nothing_aside(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Failures within SET_ASIDE_AFTER_TIME only pause."""
    await setup_integration(hass, mock_api, config_entry)
    await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": "x"})
    mock_api.post(HANDOVER_URL, status=503, payload={"detail": "busy"}, repeat=True)
    queue = _queue(hass)
    for _ in range(10):
        assert await queue.async_flush(config_entry.runtime_data) is False
    assert queue.has_ops and "set_aside" not in queue.ops[0]


async def test_expiry_names_the_real_cause(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup_integration(hass, mock_api, config_entry)
    await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": "x"})
    mock_api.post(HANDOVER_URL, status=429, payload={"detail": "slow"}, repeat=True)
    queue = _queue(hass)
    assert await queue.async_flush(config_entry.runtime_data) is False
    caplog.clear()
    freezer.tick(timedelta(hours=73))
    assert await queue.async_flush(config_entry.runtime_data) is True
    assert "the hub kept answering HTTP 429" in caplog.text
    assert "not reachable" not in caplog.text


# ── What the caller sees while the handover is paused ───────────────────


async def _paused(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    mock_api: aioresponses,
    status: int,
) -> None:
    """One notification queued while down, then the hub answers ``status``."""
    await setup_integration(hass, mock_api, entry)
    await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": "old"})
    mock_api.post(HANDOVER_URL, status=status, payload={"detail": "no"}, repeat=True)
    assert await _queue(hass).async_flush(entry.runtime_data) is False


def _reauth_flows(hass: HomeAssistant) -> list[Any]:
    return [
        f
        for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if f["context"]["source"] == "reauth"
    ]


@pytest.mark.parametrize("status", [403, 429, 500, 503])
async def test_paused_on_load_queues_succeeds_and_logs_once(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    await _paused(hass, config_entry, mock_api, status)
    caplog.clear()
    for title in ("new", "newer"):
        await hass.services.async_call(
            DOMAIN,
            "display_notification",
            {"client_id": CLIENT_ID, "title": title},
            blocking=True,
        )
    assert [op["fields"]["title"] for op in _queue(hass).ops] == [
        "old",
        "new",
        "newer",
    ]
    assert caplog.text.count(f"the hub answered HTTP {status}") == 1
    assert "not reachable" not in caplog.text
    assert not _reauth_flows(hass)


@pytest.mark.parametrize("status", [401])
async def test_paused_on_a_rejected_token_queues_raises_and_reauths(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    status: int,
) -> None:
    """The poll still accepts the token: the reauthentication is started."""
    await _paused(hass, config_entry, mock_api, status)
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            "display_notification",
            {"client_id": CLIENT_ID, "title": "new"},
            blocking=True,
        )
    assert err.value.translation_key == "queued_token_rejected"
    assert err.value.translation_placeholders == {"status": str(status)}
    assert [op["fields"]["title"] for op in _queue(hass).ops] == ["old", "new"]
    await hass.async_block_till_done()
    assert len(_reauth_flows(hass)) == 1
    for url in (CLEAR_URL, ACK_URL):
        mock_api.post(url, status=status, payload={"detail": "no"}, repeat=True)
    for service, data in (
        ("clear_notifications", {"client_id": CLIENT_ID}),
        ("ack_notifications", {"client_id": CLIENT_ID, "notification_ids": ["x"]}),
    ):
        with pytest.raises(HomeAssistantError) as err:
            await hass.services.async_call(DOMAIN, service, data, blocking=True)
        assert err.value.translation_key == "queued_token_rejected"
    await hass.async_block_till_done()
    assert len(_reauth_flows(hass)) == 1


async def test_rejected_token_runs_the_poll_path(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """The poll rejects the token too: its own path starts the reauth."""
    await setup_integration(hass, mock_api, config_entry, repeat=False)
    await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": "old"})
    mock_api.post(HANDOVER_URL, status=401, payload={"detail": "no"}, repeat=True)
    mock_api.get(STATUS_URL, status=401, repeat=True)
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            "display_notification",
            {"client_id": CLIENT_ID, "title": "new"},
            blocking=True,
        )
    assert err.value.translation_key == "queued_token_rejected"
    await hass.async_block_till_done()
    assert len(_reauth_flows(hass)) == 1
    assert len(_queue(hass).ops) == 2


@pytest.mark.parametrize(("status", "reauth"), [(401, 1), (403, 0)])
async def test_direct_call_with_a_rejected_token_raises_and_reauths(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    status: int,
    reauth: int,
) -> None:
    """Only a 401 is the token; a 403 (network path) starts no reauth."""
    await setup_integration(hass, mock_api, config_entry)
    mock_api.post(NOTIFY_URL, status=status, payload={"detail": "bad token"})
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "display_notification",
            {"client_id": CLIENT_ID, "title": "new"},
            blocking=True,
        )
    await hass.async_block_till_done()
    assert len(_reauth_flows(hass)) == reauth
    assert not _queue(hass).has_ops


async def test_paused_by_another_hub_queues_and_raises(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry)
    await _notify_down(hass, mock_api, {"client_id": CLIENT_ID, "title": "old"})
    config_entry.runtime_data.other_hub = "0b9e8d7c-6f5a-4e3d-9c2b-1a0f9e8d7c6b"
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            "display_notification",
            {"client_id": CLIENT_ID, "title": "new"},
            blocking=True,
        )
    assert err.value.translation_key == "other_hub_held"
    assert [op["fields"]["title"] for op in _queue(hass).ops] == ["old", "new"]


async def test_queue_limit_per_device(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry)
    queue = _queue(hass)
    for i in range(MAX_QUEUED_PER_CLIENT + 2):
        await queue.async_notify([CLIENT_ID], {"title": f"n{i}", "message": ""}, {})
    titles = [op["fields"]["title"] for op in queue.ops]
    assert len(titles) == MAX_QUEUED_PER_CLIENT
    assert titles[0] == "n2"
    assert len(_items(hass)) == MAX_QUEUED_PER_CLIENT


async def test_restart_restores_sensors_and_drops_expired(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    now = dt_util.utcnow()
    fresh = (now - timedelta(hours=1)).isoformat()
    old = (now - timedelta(hours=73)).isoformat()

    def _op(nid: str, created_at: str) -> dict[str, Any]:
        return {
            "op": "notify",
            "client_id": CLIENT_ID,
            "notification_id": nid,
            "created_at": created_at,
            "status": "pending",
            "fields": {"title": nid, "message": ""},
            "item": {**HUB_ITEM, "notification_id": nid, "created_at": created_at},
        }

    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "key": STORAGE_KEY,
        "data": {
            "ops": [_op("fresh", fresh), _op("old", old)],
            "clients": {CLIENT_ID: {"name": "Wall Tablet", "active": True}},
        },
    }
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, DOMAIN, {})
    assert [op["notification_id"] for op in _queue(hass).ops] == ["fresh"]
    state = hass.states.get(HUB_SENSOR)
    assert state is not None
    assert [i["notification_id"] for i in state.attributes["notifications"]] == [
        "fresh"
    ]
    assert state.attributes["pending_handover"] == ["fresh"]


def test_helpers() -> None:
    assert notification_sensor_id("Büro Tablet", CLIENT_ID) == (
        f"sensor.{DOMAIN}_client_buro_tablet_11111111_notifications"
    )
    assert notification_sensor_id("", CLIENT_ID).endswith(
        "_client_client_11111111_notifications"
    )
    # The cap at 48 right after a separator left "__" (HA 400).
    cut = notification_sensor_id("a" * 47 + " b", CLIENT_ID)
    assert cut == f"sensor.{DOMAIN}_client_{'a' * 47}_11111111_notifications"
    assert valid_entity_id(cut)
    for name in ("ab " * 30, "Straße – 1,5 m", "Ærø Łódź", "___", "-x-"):
        assert valid_entity_id(notification_sensor_id(name, CLIENT_ID)), name
    assert "_strasse_15_m_" in notification_sensor_id("Straße – 1,5 m", CLIENT_ID)
    assert hub_unreachable(TimeoutError())
    assert hub_unreachable(aiohttp.ClientConnectionError())
    assert hub_unreachable(HubApiError("x", status=503))
    assert not hub_unreachable(HubApiError("x", status=404))
    assert not hub_unreachable(HubApiError("x", status=500))
    assert not hub_unreachable(None)
