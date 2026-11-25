"""Tests for the Halumar WebSocket push client.

Covered: capability gate on hub.ws_push (old hubs
keep the plain 30s poll with no connection attempt), Bearer-authenticated
handshake, interval switching (300s while connected, back to 30s on
disconnect), JSON ping -> pong keepalive, events as pure refresh triggers,
exponential backoff with jitter, 403 handshake rejection without a reauth
flow, unload cancelling the task before the shared session closes, and the
pairing-expiry one-shot refresh timer.

The connected-behavior tests run against a real in-process aiohttp server
(aioresponses cannot speak the WebSocket protocol); the gate and pairing
timer tests use the plain aioresponses mock.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from aioresponses import aioresponses
from freezegun.api import FrozenDateTimeFactory

from custom_components.halumar.const import (
    CONF_API_TOKEN,
    CONF_USE_SSL,
    DOMAIN,
    ENTRY_UNIQUE_ID,
    SLOW_UPDATE_INTERVAL,
    UPDATE_INTERVAL,
)
from custom_components.halumar.push import HubPushClient
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from ._ha import (
    MockConfigEntry,
    async_fire_time_changed,
)
from .common import (
    HUB_INSTANCE_ID,
    STATUS_URL,
    TEST_TOKEN,
    make_status_payload,
    setup_integration,
)

WS_PUSH_HUB = {
    "version": "2026.12.0",
    "instance_id": HUB_INSTANCE_ID,
    "connected_clients": 1,
    "ws_push": True,
}


def _ws_push_payload() -> dict[str, Any]:
    """Return a status payload whose hub advertises the ws_push capability."""
    payload = make_status_payload()
    payload["hub"] = dict(WS_PUSH_HUB)
    return payload


async def _wait_for(condition, timeout: float = 5.0) -> None:
    """Poll a sync condition until true or fail the test after timeout."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


class PushTestServer:
    """A minimal hub stand-in: REST status + /api/ws push endpoint."""

    def __init__(self) -> None:
        """Build the app with the endpoints the push tests use."""
        self.app = web.Application()
        self.app.router.add_get("/api/integration/status", self._status)
        self.app.router.add_get("/api/ws", self._ws)
        self.server = TestServer(self.app)

        self.status_payload = _ws_push_payload()
        self.status_code = 200
        self.reject_handshake_with_403 = False
        self.close_with_4003_after_accept = False
        self.ws_attempts = 0
        self.ws_headers: list[dict[str, str]] = []
        self.ws: web.WebSocketResponse | None = None
        self.received: list[dict[str, Any]] = []
        self.connected = asyncio.Event()
        self.ws_closed = asyncio.Event()

    async def _status(self, request: web.Request) -> web.Response:
        if self.status_code != 200:
            return web.Response(status=self.status_code, text="boom")
        return web.json_response(self.status_payload)

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        self.ws_attempts += 1
        self.ws_headers.append(dict(request.headers))
        if self.reject_handshake_with_403:
            return web.Response(status=403, text="invalid integration token")

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if self.close_with_4003_after_accept:
            await ws.close(code=4003, message=b"auth rejected")
            return ws
        self.ws = ws
        await ws.send_json({"type": "auth_ok", "client_id": "integration"})
        self.connected.set()
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                self.received.append(json.loads(msg.data))
        self.ws_closed.set()
        return ws


async def _setup_against_server(
    hass: HomeAssistant, server: PushTestServer
) -> MockConfigEntry:
    """Start the server and set the integration up against it."""
    await server.server.start_server()
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ENTRY_UNIQUE_ID,
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: server.server.port,
            CONF_API_TOKEN: TEST_TOKEN,
            CONF_USE_SSL: False,
        },
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


# ── Capability gate (aioresponses, no WS server needed) ──────────────────


async def test_no_push_task_without_ws_push_capability(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """A hub without hub.ws_push gets a pure 30s poll: no push task."""
    await setup_integration(hass, mock_api, config_entry)

    coordinator = config_entry.runtime_data
    assert coordinator.push_task is None
    assert coordinator.update_interval == timedelta(seconds=UPDATE_INTERVAL)


async def test_push_task_started_when_capability_advertised(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """hub.ws_push truthy starts the background task; unload cancels it."""
    await setup_integration(hass, mock_api, config_entry, payload=_ws_push_payload())

    coordinator = config_entry.runtime_data
    task = coordinator.push_task
    assert task is not None
    assert not task.done()

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert task.cancelled()
    assert coordinator.push_task is None


async def test_push_task_starts_when_capability_appears_later(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """The gate runs per refresh, not once at setup.

    A hub updated to a ws_push-capable version while the entry stays
    loaded must start the push channel on the next successful poll; no
    entry reload (and no Supervisor re-discovery) required.
    """
    await setup_integration(hass, mock_api, config_entry, repeat=False)
    coordinator = config_entry.runtime_data
    assert coordinator.push_task is None

    # The hub was updated: the next poll advertises hub.ws_push.
    mock_api.get(STATUS_URL, status=200, payload=_ws_push_payload(), repeat=True)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    task = coordinator.push_task
    assert task is not None
    assert not task.done()

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert task.cancelled()


async def test_push_task_stops_when_capability_disappears(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """A snapshot without hub.ws_push cancels the task and restores 30s."""
    server = PushTestServer()
    try:
        entry = await _setup_against_server(hass, server)
        coordinator = entry.runtime_data
        await asyncio.wait_for(server.connected.wait(), 5)
        await _wait_for(
            lambda: (
                coordinator.update_interval == timedelta(seconds=SLOW_UPDATE_INTERVAL)
            )
        )

        task = coordinator.push_task
        assert task is not None

        # The capability disappears (e.g. the hub was downgraded).
        server.status_payload = make_status_payload()
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert coordinator.push_task is None
        assert task.done()
        assert coordinator.update_interval == timedelta(seconds=UPDATE_INTERVAL)

        # And a snapshot that re-advertises it starts a fresh task.
        server.status_payload = _ws_push_payload()
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        assert coordinator.push_task is not None
        assert coordinator.push_task is not task

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        await server.server.close()


async def test_failed_refresh_keeps_push_task(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """A failed poll never touches the running task or its slow interval.

    Reconnect/backoff on a flaky hub is the push client's own job; the
    per-refresh gate only acts on successful snapshots.
    """
    server = PushTestServer()
    try:
        entry = await _setup_against_server(hass, server)
        coordinator = entry.runtime_data
        await asyncio.wait_for(server.connected.wait(), 5)
        await _wait_for(
            lambda: (
                coordinator.update_interval == timedelta(seconds=SLOW_UPDATE_INTERVAL)
            )
        )

        task = coordinator.push_task
        server.status_code = 500
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        assert not coordinator.last_update_success
        assert coordinator.push_task is task
        assert not task.done()
        assert coordinator.update_interval == timedelta(seconds=SLOW_UPDATE_INTERVAL)

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        await server.server.close()


# ── Connected behavior (real WS server) ──────────────────────────────────


async def test_push_connects_pings_and_triggers_refresh(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """Full push lifecycle against a real server.

    Covers: Bearer handshake headers, auth_ok -> slow interval, JSON ping ->
    JSON pong, protocol chatter ignored, an event (including unknown types)
    triggering exactly one coordinator refresh, and unload cancelling the
    task (which restores the fast interval and closes the socket).
    """
    server = PushTestServer()
    try:
        entry = await _setup_against_server(hass, server)
        coordinator = entry.runtime_data

        await asyncio.wait_for(server.connected.wait(), 5)

        # The handshake reused the coordinator session's default headers.
        handshake = server.ws_headers[0]
        assert handshake["Authorization"] == f"Bearer {TEST_TOKEN}"
        assert "X-Hub-Integration-Version" in handshake

        # auth_ok relaxes the safety poll to the slow interval.
        await _wait_for(
            lambda: (
                coordinator.update_interval == timedelta(seconds=SLOW_UPDATE_INTERVAL)
            )
        )

        # A JSON ping is answered with a JSON pong (not a protocol pong).
        assert server.ws is not None
        await server.ws.send_json({"type": "ping"})
        await _wait_for(lambda: {"type": "pong"} in server.received)

        with patch.object(
            coordinator, "async_request_refresh", new=AsyncMock()
        ) as mock_refresh:
            # Protocol chatter must NOT trigger refreshes ...
            await server.ws.send_json({"type": "pong"})
            await server.ws.send_json({"type": "subscribed"})
            await server.ws.send_json({"type": "error", "message": "nope"})
            # ... but any event does — even a type this client never heard
            # of (forward compatibility: events are pure refresh triggers).
            await server.ws.send_json(
                {"type": "some_future_event", "data": {"client_id": "x"}}
            )
            await _wait_for(lambda: mock_refresh.await_count >= 1)
            # In-order processing: the chatter before the event was already
            # handled without a refresh, so the count is exactly one.
            assert mock_refresh.await_count == 1

        # Unload cancels the task before the session closes; the fast
        # interval is restored and the server sees the socket close.
        task = coordinator.push_task
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        assert task is not None and task.cancelled()
        assert coordinator.update_interval == timedelta(seconds=UPDATE_INTERVAL)
        await asyncio.wait_for(server.ws_closed.wait(), 5)
    finally:
        await server.server.close()


async def test_rejected_handshake_never_starts_reauth(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """A 403 WS handshake keeps retrying with backoff — never reauth.

    Token rotation on hub restart is healed by Supervisor re-discovery;
    reauth stays exclusive to the REST poll's 401 handling.
    """
    server = PushTestServer()
    server.reject_handshake_with_403 = True
    try:
        entry = await _setup_against_server(hass, server)
        coordinator = entry.runtime_data

        # Wait for at least two attempts so a backoff cycle demonstrably ran.
        await _wait_for(lambda: server.ws_attempts >= 2, timeout=10.0)

        assert entry.state is ConfigEntryState.LOADED
        assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        # Never authenticated: the poll interval was never relaxed.
        assert coordinator.update_interval == timedelta(seconds=UPDATE_INTERVAL)

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        await server.server.close()


async def test_close_4003_after_accept_never_starts_reauth(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """An accepted-then-closed (code 4003) session is a plain retry too."""
    server = PushTestServer()
    server.close_with_4003_after_accept = True
    try:
        entry = await _setup_against_server(hass, server)
        coordinator = entry.runtime_data

        await _wait_for(lambda: server.ws_attempts >= 2, timeout=10.0)

        assert entry.state is ConfigEntryState.LOADED
        assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        # auth_ok never arrived, so the interval was never relaxed.
        assert coordinator.update_interval == timedelta(seconds=UPDATE_INTERVAL)

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        await server.server.close()


async def test_reconnect_backoff_grows_exponentially_with_jitter(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """Delays follow 1, 2, 4, ... doubling to the 60s cap, each +/-20%."""
    server = PushTestServer()
    server.reject_handshake_with_403 = True
    delays: list[float] = []

    async def _instant_sleep(self: HubPushClient, delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0)

    try:
        with patch.object(HubPushClient, "_async_sleep", _instant_sleep):
            entry = await _setup_against_server(hass, server)
            await _wait_for(lambda: len(delays) >= 8, timeout=10.0)

            # 1 -> 2 -> 4 -> 8 -> 16 -> 32 -> 60 (cap) -> 60, each with
            # +/-20% jitter applied to the pre-doubling value.
            expected_bases = [1, 2, 4, 8, 16, 32, 60, 60]
            for delay, base in zip(delays[:8], expected_bases, strict=True):
                assert base * 0.8 <= delay <= base * 1.2, (delay, base)

            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
    finally:
        await server.server.close()


async def test_disconnect_restores_fast_interval_and_refreshes(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """A server-side close drops the poll back to 30s with a catch-up refresh."""
    server = PushTestServer()
    try:
        entry = await _setup_against_server(hass, server)
        coordinator = entry.runtime_data
        await asyncio.wait_for(server.connected.wait(), 5)
        await _wait_for(
            lambda: (
                coordinator.update_interval == timedelta(seconds=SLOW_UPDATE_INTERVAL)
            )
        )

        with patch.object(
            coordinator, "async_request_refresh", new=AsyncMock()
        ) as mock_refresh:
            assert server.ws is not None
            await server.ws.close()
            # Disconnect: interval back to fast + one immediate resync.
            await _wait_for(
                lambda: (
                    coordinator.update_interval == timedelta(seconds=UPDATE_INTERVAL)
                )
            )
            await _wait_for(lambda: mock_refresh.await_count >= 1)

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        await server.server.close()


async def test_silent_session_is_dropped_and_reconnected(
    hass: HomeAssistant, socket_enabled: None
) -> None:
    """A session that stays silent past the hub's ping interval counts
    as dead: the client closes it, polls fast again and reconnects."""
    server = PushTestServer()
    try:
        with patch("custom_components.halumar.push.PUSH_SILENCE_TIMEOUT", 0.3):
            entry = await _setup_against_server(hass, server)
            coordinator = entry.runtime_data
            await asyncio.wait_for(server.connected.wait(), 5)
            # The server never pings: the client gives up on the session.
            await asyncio.wait_for(server.ws_closed.wait(), 5)
            await _wait_for(
                lambda: (
                    coordinator.update_interval == timedelta(seconds=UPDATE_INTERVAL)
                )
            )
            await _wait_for(lambda: server.ws_attempts >= 2, timeout=10.0)

            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
    finally:
        await server.server.close()


# ── Pairing-expiry one-shot refresh (aioresponses) ───────────────────────


async def test_pairing_expiry_arms_one_shot_refresh(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An active pairing window arms a refresh just past its expiry.

    The pairing PIN lives in hub memory and expires without any event,
    so with the slow push-mode poll the pairing entities would otherwise
    stay stale. The WS connection itself is irrelevant here (it cannot even
    connect against the mock): the timer arms off the coordinator data.
    """
    # The window must be shorter than the poll interval: a poll that lands
    # first re-arms the one-shot from the fresh snapshot (by design), which
    # would push it past this test's tick.
    payload = _ws_push_payload()
    payload.update(
        {"pairing_active": True, "pairing_remaining": 10, "pairing_is_guest": False}
    )
    await setup_integration(hass, mock_api, config_entry, payload=payload)
    coordinator = config_entry.runtime_data

    with patch.object(
        coordinator, "async_request_refresh", new=AsyncMock()
    ) as mock_refresh:
        # Neither the scheduled poll nor failed WS connects go through
        # async_request_refresh, so any call here is the pairing one-shot.
        freezer.tick(timedelta(seconds=15))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert mock_refresh.await_count >= 1

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_no_pairing_timer_when_pairing_inactive(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Without an active pairing window nothing fires a one-shot refresh."""
    await setup_integration(hass, mock_api, config_entry, payload=_ws_push_payload())
    coordinator = config_entry.runtime_data

    with patch.object(
        coordinator, "async_request_refresh", new=AsyncMock()
    ) as mock_refresh:
        freezer.tick(timedelta(seconds=35))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert mock_refresh.await_count == 0

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
