"""Edge cases of the WebSocket push client, driven with stub objects."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant

from custom_components.halumar import push
from custom_components.halumar.push import HubPushClient


class StubCoordinator:
    """What HubPushClient uses of the coordinator."""

    base_url = "http://127.0.0.1:8099"

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data
        self.device_users = None
        self.other_hub: str | None = None
        self.update_interval = None
        self.async_request_refresh = AsyncMock()
        self.listeners: list[Any] = []

    def async_add_listener(self, listener: Any) -> Any:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)


def _msg(kind: aiohttp.WSMsgType, data: Any = None) -> MagicMock:
    msg = MagicMock()
    msg.type = kind
    msg.data = data
    return msg


async def test_identity_change_refreshes_for_the_device_name(
    hass: HomeAssistant,
) -> None:
    """A rename on the hub renames the HA user and refreshes (device name)."""
    coordinator = StubCoordinator()
    coordinator.device_users = MagicMock()
    client = HubPushClient(hass, coordinator)  # type: ignore[arg-type]
    ws = MagicMock()
    ws.receive = AsyncMock(
        side_effect=[
            _msg(aiohttp.WSMsgType.TEXT, json.dumps({"type": "provision_client"})),
            _msg(
                aiohttp.WSMsgType.TEXT, json.dumps({"type": "update_client_identity"})
            ),
            _msg(aiohttp.WSMsgType.CLOSED),
        ]
    )
    await client._pump(ws)
    assert coordinator.device_users.async_schedule_push.call_count == 2
    assert coordinator.async_request_refresh.await_count == 1


async def test_auth_ok_timeout_and_unexpected_first_message(
    hass: HomeAssistant,
) -> None:
    """No auth_ok (timeout, other message, close) fails the attempt quietly."""
    client = HubPushClient(hass, StubCoordinator())  # type: ignore[arg-type]
    ws = MagicMock()
    ws.receive = AsyncMock(side_effect=TimeoutError)
    assert await client._wait_for_auth_ok(ws) is False  # noqa: SLF001
    ws.receive = AsyncMock(return_value=_msg(aiohttp.WSMsgType.TEXT, '{"type":"x"}'))
    assert await client._wait_for_auth_ok(ws) is False  # noqa: SLF001
    ws.receive = AsyncMock(return_value=_msg(aiohttp.WSMsgType.CLOSE))
    assert await client._wait_for_auth_ok(ws) is False  # noqa: SLF001


async def test_pump_skips_binary_and_garbage(hass: HomeAssistant) -> None:
    """Binary frames, non-JSON and protocol chatter trigger no refresh."""
    coordinator = StubCoordinator()
    client = HubPushClient(hass, coordinator)  # type: ignore[arg-type]
    ws = MagicMock()
    ws.receive = AsyncMock(
        side_effect=[
            _msg(aiohttp.WSMsgType.BINARY, b"\x00"),
            _msg(aiohttp.WSMsgType.TEXT, "not json"),
            _msg(aiohttp.WSMsgType.TEXT, json.dumps([1, 2])),
            _msg(aiohttp.WSMsgType.TEXT, json.dumps({"type": "pong"})),
            _msg(aiohttp.WSMsgType.TEXT, json.dumps({"type": "provision_client"})),
            _msg(aiohttp.WSMsgType.CLOSED),
        ]
    )
    await client._pump(ws)  # noqa: SLF001
    # Only the device-user message (without the feature) refreshed.
    assert coordinator.async_request_refresh.await_count == 1
    assert HubPushClient._decode(None) is None  # noqa: SLF001


async def test_pairing_timer_rearms_and_tolerates_bad_values(
    hass: HomeAssistant,
) -> None:
    """Re-arming cancels the previous timer; a bad remaining time means 0 s."""
    coordinator = StubCoordinator({"pairing_active": True, "pairing_remaining": "x"})
    client = HubPushClient(hass, coordinator)  # type: ignore[arg-type]
    with patch.object(push, "async_call_later") as call_later:
        cancel = MagicMock()
        call_later.return_value = cancel
        client._arm_pairing_expiry_timer()  # noqa: SLF001
        assert call_later.call_args.args[1] == push.PAIRING_EXPIRY_GRACE
        client._arm_pairing_expiry_timer()  # noqa: SLF001
        cancel.assert_called_once()


async def test_run_survives_unexpected_errors_and_resets_backoff(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """An unexpected error is logged; a long-lived session resets the backoff;
    cancelling restores the fast interval and stops the pairing timer."""
    coordinator = StubCoordinator({"pairing_active": True, "pairing_remaining": 60})
    client = HubPushClient(hass, coordinator)  # type: ignore[arg-type]
    delays: list[float] = []
    calls = 0

    async def one_connection() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("bug")
        # Second connection: authenticated long enough to count as stable.
        client._authed_at = time.monotonic() - push.STABLE_CONNECTION_SECONDS  # noqa: SLF001
        if calls == 3:
            await asyncio.sleep(3600)

    async def sleep(delay: float) -> None:
        delays.append(delay)

    with (
        patch.object(client, "_run_one_connection", one_connection),
        patch.object(client, "_async_sleep", sleep),
        patch.object(push, "_next_delay", lambda backoff: backoff),
    ):
        task = asyncio.create_task(client.async_run())
        for _ in range(50):
            await asyncio.sleep(0)
            if calls >= 3:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert "Unexpected push channel error" in caplog.text
    # 1st failure: 1 s; 2nd (stable session) resets to 1 s again.
    assert delays == [push.BACKOFF_INITIAL, push.BACKOFF_INITIAL]
    assert coordinator.listeners == []
    assert client._cancel_pairing_cb is None  # noqa: SLF001
