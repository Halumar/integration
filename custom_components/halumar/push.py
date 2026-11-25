"""WebSocket push client for the Halumar integration.

Connects to the hub's /api/ws endpoint (only when the status snapshot
sets ``hub.ws_push``), so state changes reach Home Assistant within ~1s
instead of the poll cadence.

Design (deliberately minimal):

* The connection goes through the shared HA session with the same request
  headers as the REST calls (``Authorization: Bearer <integration token>``
  and the integration version) — no second credential path.
* Every received event is a REFRESH TRIGGER ONLY: the client never applies
  event payloads itself. ``GET /api/integration/status`` stays the single
  source of truth for all entities, which keeps the client forward
  compatible with event types this version does not know. The coordinator's
  request-refresh debouncer (immediate + 10s cooldown) coalesces bursts.
* While the push channel is up, the REST poll relaxes to
  SLOW_UPDATE_INTERVAL as a safety net; on disconnect it drops back to
  UPDATE_INTERVAL. Each switch requests an immediate refresh because a new
  interval only takes effect after the next refresh.
* Reconnects use exponential backoff (1s doubling to a 60s cap, +/-20%
  jitter), reset after a connection that stayed authenticated long enough
  to count as stable.
* A rejected handshake (HTTP 403 / close 4003) NEVER raises
  ConfigEntryAuthFailed from this task: the hub rotates the integration
  token on every restart and Supervisor re-discovery heals the entry;
  reauth stays exclusively on the coordinator's REST poll.
* The pairing window expires server-side without any event (the PIN lives
  in memory), so a one-shot refresh timer is armed just past the advertised
  ``pairing_remaining`` and re-armed on every coordinator update. This
  keeps ``binary_sensor.pairing_active`` honest even on the slow poll.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import aiohttp

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .const import SLOW_UPDATE_INTERVAL, UPDATE_INTERVAL
from .device_users import MSG_UPDATE_IDENTITY, PUSH_MESSAGE_TYPES
from .enforcement_sync import async_handle_push
from .family import async_handle_relay
from .location import async_handle_push as async_handle_location

if TYPE_CHECKING:
    from .coordinator import HubConfigEntry, HubCoordinator

_LOGGER = logging.getLogger(__name__)

# Upper bound of the WS handshake (HTTP upgrade).
HANDSHAKE_TIMEOUT = 15.0

# How long to wait for the server's auth_ok after the WS handshake before
# giving up on the attempt.
AUTH_OK_TIMEOUT = 10.0

# Longest silence of an authenticated session before it counts as dead and
# is reconnected: the hub sends a JSON ping every 30 s (api/ws/broadcast.py
# WS_PING_INTERVAL) and drops a session that misses one by 10 s. Without
# it a socket whose peer vanished (hub host off, NAT reset) would wait for
# TCP forever and the 300 s safety poll would stay on.
PUSH_SILENCE_TIMEOUT = 45.0

# Reconnect backoff: starts at BACKOFF_INITIAL, doubles per failed attempt
# up to BACKOFF_CAP, with +/-BACKOFF_JITTER relative jitter on every delay.
BACKOFF_INITIAL = 1.0
BACKOFF_CAP = 60.0
BACKOFF_JITTER = 0.2

# A connection that stayed authenticated at least this long counts as
# stable and resets the backoff, so a server that accepts and then
# immediately drops the session cannot cause a 1s reconnect storm.
STABLE_CONNECTION_SECONDS = 30.0

# Extra seconds past the advertised pairing_remaining before the one-shot
# pairing-expiry refresh fires (covers clock skew and in-flight polls).
PAIRING_EXPIRY_GRACE = 2.0

# Server messages that are protocol chatter, not push events: they must not
# trigger a refresh. Everything else (known or unknown event type) does.
# family_changed: admin UI broadcast of the family manager, nothing to refresh.
_NON_EVENT_TYPES = frozenset(
    {"auth_ok", "pong", "subscribed", "error", "family_changed"}
)


def _next_delay(backoff: float) -> float:
    """Return the jittered reconnect delay for the current backoff value."""
    return backoff * (1.0 + random.uniform(-BACKOFF_JITTER, BACKOFF_JITTER))


class HubPushClient:
    """Maintains the WS push connection for one config entry.

    ``async_run`` is started as a config-entry background task and loops
    until cancelled (entry unload). It owns nothing except the pairing
    timer and its coordinator listener; the aiohttp session belongs to the
    coordinator.
    """

    def __init__(self, hass: HomeAssistant, coordinator: HubCoordinator) -> None:
        """Store collaborators and derive the WS endpoint URL."""
        self._hass = hass
        self._coordinator = coordinator
        # http://host:port -> ws://host:port, https -> wss
        self._ws_url = coordinator.base_url.replace("http", "ws", 1) + "/api/ws"
        self._cancel_pairing_cb: CALLBACK_TYPE | None = None
        # Monotonic timestamp of the current connection's auth_ok, None while
        # not authenticated. Drives the interval restore + backoff reset.
        self._authed_at: float | None = None

    async def async_run(self) -> None:
        """Run the connect/listen/backoff loop until the task is cancelled.

        Only CancelledError ever escapes, so awaiting the cancelled task on
        unload can never raise anything else.
        """
        # Re-arm the pairing-expiry one-shot on every coordinator update
        # (and once for the snapshot that gated this task on).
        remove_listener = self._coordinator.async_add_listener(
            self._arm_pairing_expiry_timer
        )
        self._arm_pairing_expiry_timer()

        backoff = BACKOFF_INITIAL
        try:
            while True:
                self._authed_at = None
                try:
                    await self._run_one_connection()
                except (aiohttp.ClientError, TimeoutError, OSError) as err:
                    # Includes the 403 handshake rejection. Deliberately NOT
                    # ConfigEntryAuthFailed: reauth stays exclusive to the
                    # REST poll (see module docstring).
                    _LOGGER.debug("Push connection error: %s", err)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _LOGGER.exception("Unexpected push channel error")

                stable = False
                authed_at = self._authenticated_since()
                if authed_at is not None:
                    stable = time.monotonic() - authed_at >= STABLE_CONNECTION_SECONDS
                    # Push channel lost: fall back to the fast poll and
                    # resync immediately — the interval switch only takes
                    # effect after a refresh.
                    self._coordinator.update_interval = timedelta(
                        seconds=UPDATE_INTERVAL
                    )
                    self._authed_at = None
                    await self._coordinator.async_request_refresh()

                if stable:
                    backoff = BACKOFF_INITIAL
                await self._async_sleep(_next_delay(backoff))
                backoff = min(backoff * 2.0, BACKOFF_CAP)
        except asyncio.CancelledError:
            # Entry unload: restore the fast interval synchronously; the
            # catch-up refresh is skipped because the coordinator is shut
            # down right after this task is cancelled.
            if self._authenticated_since() is not None:
                self._coordinator.update_interval = timedelta(seconds=UPDATE_INTERVAL)
            raise
        finally:
            remove_listener()
            if self._cancel_pairing_cb is not None:
                self._cancel_pairing_cb()
                self._cancel_pairing_cb = None

    def _authenticated_since(self) -> float | None:
        """Monotonic time of the current connection's auth_ok (None: not authed)."""
        return self._authed_at

    async def _async_sleep(self, delay: float) -> None:
        """Sleep between reconnect attempts (seam for tests)."""
        await asyncio.sleep(delay)

    async def _run_one_connection(self) -> None:
        """Connect, wait for auth_ok, then pump messages until the socket drops."""
        if self._coordinator.other_hub is not None:
            # Not to a hub the admin has not confirmed (repair other_hub).
            return
        headers = await self._coordinator.async_request_headers()
        # The shared HA session has no handshake timeout of its own; once
        # upgraded, aiohttp drops the read timeout for the WebSocket.
        async with asyncio.timeout(HANDSHAKE_TIMEOUT):
            ws = await self._coordinator.session.ws_connect(
                self._ws_url, headers=headers
            )
        async with ws:
            if not await self._wait_for_auth_ok(ws):
                return
            self._authed_at = time.monotonic()
            _LOGGER.info("Push channel connected (%s)", self._ws_url)
            # Connected: relax the safety poll and catch up on anything that
            # happened while disconnected (the refresh also makes the new
            # interval take effect).
            self._coordinator.update_interval = timedelta(seconds=SLOW_UPDATE_INTERVAL)
            await self._coordinator.async_request_refresh()
            await self._pump(ws)
        _LOGGER.info("Push channel lost, falling back to 30s polling")

    async def _wait_for_auth_ok(self, ws: aiohttp.ClientWebSocketResponse) -> bool:
        """Return True once the server confirmed the session with auth_ok.

        The hub authenticates the Bearer header during the handshake and
        sends auth_ok as its first message. Anything else — a close frame
        (e.g. code 4003 for a rejected token), a timeout, or an unexpected
        payload — fails the attempt without raising.
        """
        try:
            msg = await ws.receive(timeout=AUTH_OK_TIMEOUT)
        except TimeoutError:
            _LOGGER.debug("Push channel: no auth_ok within %ss", AUTH_OK_TIMEOUT)
            return False
        if msg.type != aiohttp.WSMsgType.TEXT:
            _LOGGER.debug("Push channel closed during auth (%s)", msg.type)
            return False
        data = self._decode(msg.data)
        if data is None or data.get("type") != "auth_ok":
            _LOGGER.debug("Push channel: expected auth_ok, got %s", msg.data)
            return False
        return True

    async def _pump(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Answer keepalive pings and turn push events into refresh requests."""
        while True:
            try:
                async with asyncio.timeout(PUSH_SILENCE_TIMEOUT):
                    msg = await ws.receive()
            except TimeoutError:
                _LOGGER.info(
                    "Push channel silent for %ss, reconnecting", PUSH_SILENCE_TIMEOUT
                )
                return
            if msg.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ):
                return
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = self._decode(msg.data)
            if data is None:
                continue
            msg_type = data.get("type")
            if msg_type == "ping":
                # Application-level keepalive: the hub culls sessions
                # that do not answer its JSON ping (aiohttp only answers
                # protocol-level pings automatically).
                await ws.send_json({"type": "pong"})
            elif msg_type in _NON_EVENT_TYPES:
                continue
            elif self._coordinator.other_hub is not None:
                # The status named another hub than the entry's: nothing of
                # it is applied until the admin confirms it (repair issue).
                continue
            elif async_handle_relay(self._hass, data):
                # Family pushes for family managers away from home.
                continue
            elif async_handle_push(self._hass, self._coordinator.device_users, data):
                # Rule snapshots and hub events: no refresh.
                continue
            elif async_handle_location(self._hass, data):
                # Position of a device with parental controls: no refresh.
                continue
            elif (
                msg_type in PUSH_MESSAGE_TYPES
                and self._coordinator.device_users is not None
            ):
                # Per-device HA user requests: handled in the
                # background so the pump keeps answering pings. Without the
                # feature they fall through to a plain refresh below.
                self._coordinator.device_users.async_schedule_push(data)
                if msg_type == MSG_UPDATE_IDENTITY:
                    # A rename on the hub: the device name follows.
                    await self._coordinator.async_request_refresh()
            else:
                # Refresh trigger only — never apply the event payload (see
                # module docstring). Unknown event types refresh too, which
                # keeps this client forward compatible.
                await self._coordinator.async_request_refresh()

    @staticmethod
    def _decode(raw: Any) -> dict[str, Any] | None:
        """Parse a text frame into a dict, or None when it is not one."""
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    @callback
    def _arm_pairing_expiry_timer(self) -> None:
        """(Re)arm a one-shot refresh just past the pairing window's expiry.

        The hub keeps the pairing PIN in memory and emits no event when
        the window times out, so on the slow push-mode poll the pairing
        entities would stay stale for minutes. Re-armed on every coordinator
        update while pairing is active; cancelled otherwise.
        """
        if self._cancel_pairing_cb is not None:
            self._cancel_pairing_cb()
            self._cancel_pairing_cb = None

        data = self._coordinator.data
        if not data or not data.get("pairing_active"):
            return
        try:
            remaining = max(float(data.get("pairing_remaining") or 0), 0.0)
        except (TypeError, ValueError):
            remaining = 0.0

        async def _expiry_refresh(_now: Any) -> None:
            self._cancel_pairing_cb = None
            await self._coordinator.async_request_refresh()

        self._cancel_pairing_cb = async_call_later(
            self._hass, remaining + PAIRING_EXPIRY_GRACE, _expiry_refresh
        )


@callback
def async_sync_push_task(
    hass: HomeAssistant,
    entry: HubConfigEntry,
    coordinator: HubCoordinator,
) -> None:
    """Start or stop the push task to match the latest status snapshot.

    Runs as a coordinator listener (plus once right after setup), so the
    ``hub.ws_push`` capability appearing later — e.g. the hub updated to
    2026.7.5+ while the entry stayed loaded — starts the push channel
    without an entry reload, and a capability that disappears stops it.
    A failed refresh never touches the task: reconnect backoff on a flaky
    hub is the push client's own job.
    """
    hub = coordinator.data.get("hub") if coordinator.data else None
    want = (
        coordinator.last_update_success
        and isinstance(hub, dict)
        and bool(hub.get("ws_push"))
    )
    if want:
        if coordinator.push_task is None or coordinator.push_task.done():
            client = HubPushClient(hass, coordinator)
            coordinator.push_task = entry.async_create_background_task(
                hass, client.async_run(), name="hub-ws-push"
            )
    elif coordinator.last_update_success and coordinator.push_task is not None:
        # Cancellation awaits the task, so it cannot run inline in a
        # listener callback; async_cancel_push_task nulls push_task first
        # thing, so a re-entrant sync cannot double-cancel.
        hass.async_create_task(async_cancel_push_task(coordinator))
        # Defensive: the client only restores the fast interval itself when
        # it was authenticated at cancel time.
        coordinator.update_interval = timedelta(seconds=UPDATE_INTERVAL)


async def async_cancel_push_task(coordinator: HubCoordinator) -> None:
    """Cancel and await the entry's push task if one is running.

    Called from async_unload_entry before coordinator.async_shutdown, so no
    reconnect attempt runs against an entry that is going away.
    """
    task = coordinator.push_task
    coordinator.push_task = None
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
