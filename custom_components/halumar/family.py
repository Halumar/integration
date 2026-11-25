"""Away route of the family manager and of child devices.

The hub is only reachable at home. Away from home the app of a family manager
device speaks the same family messages over its Home Assistant WebSocket, and
a child device that holds a Home Assistant key its requests (``kids_request``,
``kids_request_cancel``; answers ``kids_request_state``), and a recipient of
early access requests its answers (``access_requests_get``,
``access_request_answer``; pushes ``access_request_update``):

* ``<ns>/family`` ``{message}`` → the integration takes the Halumar device
  from the calling HA user (the user the integration created for the
  device; never from the message or from token names),
  relays ``POST /api/integration/family {client_id, message}`` and answers
  ``result = {messages: [...]}`` (contract ``integration/ha-ws-family.result.json``);
* ``<ns>/family/subscribe`` → every ``family_relay`` push of the hub for that
  device is sent as an HA event message whose ``event`` is the hub push itself
  (``integration/ha-ws-family.event.json``).

The hub decides everything (which device may send what); the integration
only relays and rate-limits per HA user.
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from typing import Any

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.hass_dict import HassKey

from .brand import BRAND_NAME
from .const import DOMAIN
from .device_users import async_device_client_id

try:
    import probatio as vol
except ImportError:  # pragma: no cover - HA < 2026.9 validates with voluptuous
    import voluptuous as vol  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

WS_FAMILY = f"{DOMAIN}/family"
WS_FAMILY_SUBSCRIBE = f"{DOMAIN}/family/subscribe"
FAMILY_ENDPOINT = "/api/integration/family"
DATA_FAMILY = f"{DOMAIN}_family_subscriptions"

_FAMILY_TYPE = re.compile(r"^family_[a-z_]{1,40}$")
# Child devices that hold a key: their requests and the answer.
_CHILD_TYPES = frozenset({"kids_request", "kids_request_cancel"})
_CHILD_PUSH = "kids_request_state"
# Early access: recipients read and answer away; a request itself gets
# home_only from the hub (a closed device holds no valid HA key).
_ACCESS_TYPES = frozenset(
    {
        "access_request",
        "access_request_cancel",
        "access_requests_get",
        "access_request_answer",
    }
)
_ACCESS_PUSH = frozenset(
    {
        "access_request_state",
        "access_request_update",
        "access_requests",
        "access_request_error",
    }
)


def _app_message(msg_type: Any) -> bool:
    return isinstance(msg_type, str) and (
        bool(_FAMILY_TYPE.match(msg_type))
        or msg_type in _CHILD_TYPES
        or msg_type in _ACCESS_TYPES
    )


def _hub_push(msg_type: Any) -> bool:
    return isinstance(msg_type, str) and (
        bool(_FAMILY_TYPE.match(msg_type))
        or msg_type == _CHILD_PUSH
        or msg_type in _ACCESS_PUSH
    )


# Per HA user (the hub has its own limits per device).
RATE_LIMIT = 60
RATE_WINDOW = 60.0
# HA user id -> times of its calls in the current window, per HA instance.
DATA_CALLS: HassKey[dict[str, deque[float]]] = HassKey(f"{DOMAIN}_family_calls")


def _hub_unreachable() -> dict[str, Any]:
    return {
        "messages": [
            {
                "type": "family_error",
                "reason": "hub_unreachable",
                "message": "The hub is not reachable.",
            }
        ]
    }


def family_calls(hass: HomeAssistant) -> dict[str, deque[float]]:
    """The per-user call times of this HA instance."""
    return hass.data.setdefault(DATA_CALLS, {})


def _rate_ok(hass: HomeAssistant, user_id: str) -> bool:
    now = time.monotonic()
    q = family_calls(hass).setdefault(user_id, deque())
    while q and now - q[0] >= RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        return False
    q.append(now)
    return True


def _subscriptions(hass: HomeAssistant) -> dict[str, dict[tuple[int, int], Any]]:
    """client_id -> {(connection id, msg id): connection}."""
    subs: dict[str, dict[tuple[int, int], Any]] = hass.data.setdefault(DATA_FAMILY, {})
    return subs


async def _client_of(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection
) -> str | None:
    user = connection.user
    if user is None or user.is_admin or user.system_generated:
        return None
    return await async_device_client_id(hass, user)


def _coordinator(hass: HomeAssistant) -> Any:
    from .enforcement import DATA_ENFORCEMENT  # noqa: PLC0415 - import cycle

    manager = hass.data.get(DATA_ENFORCEMENT)
    return manager.coordinator() if manager is not None else None


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_FAMILY,
        vol.Required("message"): dict,
    }
)
@websocket_api.async_response
async def ws_family(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Relay one family message of the calling device to the hub."""
    client_id = await _client_of(hass, connection)
    if client_id is None:
        connection.send_error(
            msg["id"], websocket_api.ERR_UNAUTHORIZED, f"not a {BRAND_NAME} device user"
        )
        return
    message = msg["message"]
    if not _app_message(message.get("type")):
        connection.send_error(
            msg["id"],
            websocket_api.ERR_INVALID_FORMAT,
            "family_*, kids_request* or access_request* message expected",
        )
        return
    if not _rate_ok(hass, connection.user.id):
        connection.send_result(
            msg["id"],
            {
                "messages": [
                    {
                        "type": "family_error",
                        "reason": "rate_limited",
                        "message": "Too many family actions, try again later.",
                        "retry_after": int(RATE_WINDOW),
                    }
                ]
            },
        )
        return
    coordinator = _coordinator(hass)
    if coordinator is None:
        connection.send_result(msg["id"], _hub_unreachable())
        return
    try:
        answer = await coordinator.api_post_json(
            FAMILY_ENDPOINT, {"client_id": client_id, "message": message}
        )
    except Exception as err:  # noqa: BLE001 - any failure = hub unreachable
        status = getattr(err, "status", None)
        if status == 429:
            answer = {
                "messages": [
                    {
                        "type": "family_error",
                        "reason": "rate_limited",
                        "message": "Too many family actions, try again later.",
                    }
                ]
            }
        else:
            _LOGGER.info("Family message of %s not relayed: %s", client_id, err)
            answer = _hub_unreachable()
    messages = answer.get("messages") if isinstance(answer, dict) else None
    if not isinstance(messages, list) or not messages:
        answer = _hub_unreachable()
    connection.send_result(msg["id"], {"messages": answer["messages"][:10]})


@websocket_api.websocket_command({vol.Required("type"): WS_FAMILY_SUBSCRIBE})
@websocket_api.async_response
async def ws_family_subscribe(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Forward the hub's family pushes for the calling device."""
    client_id = await _client_of(hass, connection)
    if client_id is None:
        connection.send_error(
            msg["id"], websocket_api.ERR_UNAUTHORIZED, f"not a {BRAND_NAME} device user"
        )
        return
    key = (id(connection), msg["id"])
    subs = _subscriptions(hass)
    subs.setdefault(client_id, {})[key] = connection

    @callback
    def _unsubscribe() -> None:
        entries = subs.get(client_id)
        if entries is not None:
            entries.pop(key, None)
            if not entries:
                subs.pop(client_id, None)

    connection.subscriptions[msg["id"]] = _unsubscribe
    connection.send_result(msg["id"])


@callback
def async_handle_relay(hass: HomeAssistant, data: dict[str, Any]) -> bool:
    """Hub push ``family_relay``: hand the message to the subscriptions."""
    if data.get("type") != "family_relay":
        return False
    message = data.get("message")
    client_ids = data.get("client_ids")
    if not isinstance(message, dict) or not isinstance(client_ids, list):
        return True
    if not _hub_push(message.get("type")):
        return True
    subs = _subscriptions(hass)
    for client_id in client_ids:
        for (_conn_id, msg_id), connection in list(subs.get(client_id, {}).items()):
            connection.send_message(websocket_api.event_message(msg_id, message))
    return True


@callback
def async_register_family(hass: HomeAssistant) -> None:
    """Register the HA WebSocket commands (once per HA instance)."""
    websocket_api.async_register_command(hass, ws_family)
    websocket_api.async_register_command(hass, ws_family_subscribe)
