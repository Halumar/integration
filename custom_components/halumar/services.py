"""Actions (services) of the Halumar integration.

Registered once in async_setup (action-setup), so they exist even while the
config entry is not loaded; each handler then checks that the entry is
loaded and raises a translated ServiceValidationError otherwise.

- halumar.navigate_to_area: Navigate a client to a specific area (or home)
- halumar.show_entity: Show an entity on a client (with optional control_mode)
- halumar.display_notification: Send a notification to one client or all ("all")
- halumar.clear_notifications: Clear all notifications on a client
- halumar.ack_notifications: Confirm delivery of notifications for a client
  (remote fallback used by client apps when their hub WebSocket is down)
- halumar.refresh_client: Force a client to reload all data
- halumar.kick_client: Disconnect a client's WebSocket session
- halumar.start_pairing: Start a regular pairing session (optional profile)
- halumar.start_guest_pairing: Start a guest pairing session with time-limited PIN
- halumar.cancel_pairing: Cancel an open pairing window
- halumar.manage_client_tags: Add, remove, or set tags on a client
- halumar.extend_guest: Extend a guest client's session
- halumar.force_refresh: Force the integration coordinator to refresh
- halumar.control: Switch on behalf of a device (control.py, any user)
- halumar.apply_rules: Apply rule snapshots by hand (admin; the hub pushes them)
- halumar.panel_announce: Announce text with a chime on a wall panel (admin)
- halumar.panel_show_camera: Open a camera on a wall panel or TV (admin)
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from dataclasses import field as dataclass_field

# Aliased import: this package has a "datetime" platform module.
from datetime import datetime as dt_datetime
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import (
    HomeAssistantError,
    ServiceValidationError,
    Unauthorized,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.util.hass_dict import HassKey

from .const import (
    ALL_AREAS_TAG,
    DOMAIN,
    HOME_AREA_ID,
    SERVICE_APPLY_RULES,
    SERVICE_CONTROL,
    SERVICE_REPORT_LOCATION,
)
from .control import SCHEMA_CONTROL, async_control
from .coordinator import (
    HubApiError,
    HubConfigEntry,
    HubCoordinator,
    OtherHubError,
    hub_call,
)
from .panel import async_send_panel_action
from .device_users import DeviceUserManager
from .enforcement import DATA_ENFORCEMENT, EnforcementManager
from .location import SCHEMA_REPORT_LOCATION, async_report_location
from .offline_notifications import (
    DATA_OFFLINE_NOTIFICATIONS,
    NOTIFY_ENDPOINT,
    OfflineNotifications,
    broadcast_notification_id,
    hub_unreachable,
)

try:
    import probatio as vol
except ImportError:  # pragma: no cover - HA < 2026.9 validates with voluptuous
    import voluptuous as vol  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

# UUID format validator for service call parameters (security hardening)
_UUID_PATTERN = re.compile(r"^[a-f0-9\-]{8,36}$", re.IGNORECASE)

# Slug fallback: dot-separated word/dash segments. Dots may only join
# segments, so path-traversal tokens like "." / ".." can never reach the
# URL builders (yarl would normalize "clients/../kick" to a different
# endpoint).
_SLUG_PATTERN = re.compile(r"^[\w\-]+(?:\.[\w\-]+)*$")


def _cv_id(value: Any) -> str:
    """Validate that a value looks like a safe ID (UUID or slug)."""
    text: str = cv.string(value)
    if not _UUID_PATTERN.match(text) and not _SLUG_PATTERN.match(text):
        raise vol.Invalid(f"Expected a valid ID, got: {text!r}")
    return text


def _cv_iso_datetime(value: Any) -> str:
    """Validate that a string is a valid ISO 8601 datetime."""
    text: str = cv.string(value)
    try:
        dt_datetime.fromisoformat(text)
    except ValueError as err:
        raise vol.Invalid(f"Expected ISO 8601 datetime, got: {text!r}") from err
    return text


# ── Service names ────────────────────────────────────────────────────────
SERVICE_NAVIGATE_TO_AREA = "navigate_to_area"
SERVICE_SHOW_ENTITY = "show_entity"
SERVICE_DISPLAY_NOTIFICATION = "display_notification"
SERVICE_CLEAR_NOTIFICATIONS = "clear_notifications"
SERVICE_ACK_NOTIFICATIONS = "ack_notifications"
SERVICE_REFRESH_CLIENT = "refresh_client"
SERVICE_KICK_CLIENT = "kick_client"
SERVICE_FORCE_REFRESH = "force_refresh"
SERVICE_START_PAIRING = "start_pairing"
SERVICE_START_GUEST_PAIRING = "start_guest_pairing"
SERVICE_CANCEL_PAIRING = "cancel_pairing"
SERVICE_MANAGE_CLIENT_TAGS = "manage_client_tags"
SERVICE_EXTEND_GUEST = "extend_guest"
SERVICE_PANEL_ANNOUNCE = "panel_announce"
SERVICE_PANEL_SHOW_CAMERA = "panel_show_camera"

# Notification broadcast target accepted by POST /api/integration/notify
BROADCAST_ALL = "all"

# HTTP 403 on start-pairing: the hub's "allow pairing via integration"
# toggle is off.
_PAIRING_STATUS_KEYS = {403: "pairing_disabled"}

# ── Service schemas ──────────────────────────────────────────────────────
# area_id is optional because home=true navigates to the start screen
# instead; the handler enforces that at least one of the two is given.
SCHEMA_NAVIGATE_TO_AREA = vol.Schema(
    {
        vol.Required("client_id"): _cv_id,
        vol.Optional("area_id"): _cv_id,
        vol.Optional("home", default=False): cv.boolean,
    }
)

SCHEMA_SHOW_ENTITY = vol.Schema(
    {
        vol.Required("client_id"): _cv_id,
        vol.Required("entity_id"): cv.string,
        vol.Optional("control_mode"): vol.In(["basic", "advanced", "full"]),
    }
)

# Panel commands announce and show_camera; the hub checks the device, the
# rule panel_announce and the camera.
SCHEMA_PANEL_ANNOUNCE = vol.Schema(
    {
        vol.Required("client_id"): _cv_id,
        vol.Required("text"): vol.All(cv.string, vol.Length(min=1, max=300)),
        vol.Optional("volume"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        vol.Optional("chime"): cv.boolean,
    }
)

SCHEMA_PANEL_SHOW_CAMERA = vol.Schema(
    {
        vol.Required("client_id"): _cv_id,
        vol.Required("entity_id"): vol.All(
            cv.entity_id, vol.Match(r"^camera\.[a-z0-9_]+$")
        ),
    }
)

# Notification fields mirror the hub's SendNotificationRequest model
# (including its length limits). client_id additionally accepts the literal
# "all" for a broadcast.
SCHEMA_DISPLAY_NOTIFICATION = vol.Schema(
    {
        vol.Required("client_id"): _cv_id,
        vol.Optional("title"): vol.All(cv.string, vol.Length(max=500)),
        vol.Optional("message"): vol.All(cv.string, vol.Length(max=5000)),
        vol.Optional("notification_type", default="default"): vol.In(
            ["default", "sticky"]
        ),
        vol.Optional("icon"): vol.All(cv.string, vol.Length(max=200)),
        vol.Optional("icon_svg"): vol.All(cv.string, vol.Length(max=10000)),
        vol.Optional("popup_mode"): vol.In(["none", "popup", "both", "direct"]),
        vol.Optional("scheduled_at"): _cv_iso_datetime,
        vol.Optional("priority"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        vol.Optional("color"): vol.In(
            ["none", "red", "blue", "green", "yellow", "orange"]
        ),
        vol.Optional("action_type"): vol.In(["none", "area", "entity", "url"]),
        vol.Optional("action_target"): vol.All(cv.string, vol.Length(max=2000)),
        vol.Optional("show_time"): cv.boolean,
    }
)

SCHEMA_CLIENT_ONLY = vol.Schema({vol.Required("client_id"): _cv_id})

# Batch size is bounded (1..50) to match the hub's AckNotificationsRequest
# model; every id must look like a safe ID before it goes on the wire.
SCHEMA_ACK_NOTIFICATIONS = vol.Schema(
    {
        vol.Required("client_id"): _cv_id,
        vol.Required("notification_ids"): vol.All(
            cv.ensure_list, [_cv_id], vol.Length(min=1, max=50)
        ),
    }
)

SCHEMA_EMPTY = vol.Schema({})

# expires_hours is an integer per the hub's start-pairing body (1 hour .. 90
# days per grant; missing = the guest default duration of the hub).
# profile is a profile NAME of the hub (missing = the default profile of
# the device type, guests: the guest default profile; unknown names fail
# server-side).
SCHEMA_START_GUEST_PAIRING = vol.Schema(
    {
        vol.Optional("expires_hours"): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=2160)
        ),
        vol.Optional("tags"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("all_areas", default=False): cv.boolean,
        vol.Optional("profile"): vol.All(cv.string, vol.Length(min=1, max=40)),
    }
)

SCHEMA_START_PAIRING = vol.Schema(
    {vol.Optional("profile"): vol.All(cv.string, vol.Length(min=1, max=40))}
)

SCHEMA_MANAGE_CLIENT_TAGS = vol.Schema(
    {
        vol.Required("client_id"): _cv_id,
        vol.Required("action"): vol.In(["add", "remove", "set"]),
        vol.Required("tags"): vol.All(cv.ensure_list, [cv.string]),
    }
)

SCHEMA_EXTEND_GUEST = vol.Schema(
    {
        vol.Required("client_id"): _cv_id,
        vol.Optional("hours"): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=720)),
        vol.Optional("expires_at"): _cv_iso_datetime,
    }
)

# Rule snapshots by hand (the hub pushes them; this is for checks and
# tests). Validated like a push: hash, structure, own device user only.
SCHEMA_APPLY_RULES = vol.Schema(
    {
        vol.Optional("snapshots", default=list): vol.All(
            cv.ensure_list, [dict], vol.Length(max=500)
        ),
        vol.Optional("removed", default=list): vol.All(
            cv.ensure_list, [cv.string], vol.Length(max=500)
        ),
        vol.Optional("complete", default=False): cv.boolean,
        vol.Optional("control_paths", default=dict): vol.All(
            dict, vol.Length(max=1000)
        ),
    }
)


# ── Rate limiting for privileged services ────────────────────────────────
# Minimum number of seconds that must elapse between two successive
# invocations of the same privileged service. Together with a per-service
# lock (guarding against concurrent calls) this keeps a runaway automation
# or a fast caller from flooding the hub with pairing, tag or extension
# requests.
_PRIVILEGED_COOLDOWN = 3.0


@dataclass
class PrivilegedCalls:
    """Last call and lock per privileged action of this HA instance."""

    last_call: dict[str, float] = dataclass_field(default_factory=dict)
    locks: dict[str, asyncio.Lock] = dataclass_field(default_factory=dict)


DATA_PRIVILEGED_CALLS: HassKey[PrivilegedCalls] = HassKey(f"{DOMAIN}_privileged_calls")


def privileged_calls(hass: HomeAssistant) -> PrivilegedCalls:
    """The privileged-call state of this HA instance (created on first use)."""
    return hass.data.setdefault(DATA_PRIVILEGED_CALLS, PrivilegedCalls())


def _privileged_lock(hass: HomeAssistant, service: str) -> asyncio.Lock:
    """Return (creating if needed) the concurrency lock for a service."""
    return privileged_calls(hass).locks.setdefault(service, asyncio.Lock())


def _enforce_rate_limit(hass: HomeAssistant, service: str) -> None:
    """Reject calls that arrive faster than the cooldown allows."""
    last_call = privileged_calls(hass).last_call
    now = time.monotonic()
    last = last_call.get(service)
    if last is not None and (now - last) < _PRIVILEGED_COOLDOWN:
        wait = _PRIVILEGED_COOLDOWN - (now - last)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="rate_limited",
            translation_placeholders={"service": service, "wait": f"{wait:.1f}"},
        )
    last_call[service] = now


async def _run_privileged(
    hass: HomeAssistant, service: str, work: Callable[[], Awaitable[None]]
) -> None:
    """Run a privileged action: one at a time, at most once per cooldown."""
    lock = _privileged_lock(hass, service)
    if lock.locked():
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="in_progress",
            translation_placeholders={"service": service},
        )
    async with lock:
        _enforce_rate_limit(hass, service)
        await work()


def _guest_tags(data: dict[str, Any]) -> list[str] | None:
    """Area scope of a guest PIN (contracts $defs/client_tags).

    None = no tags and no ``all_areas``: the guest's profile decides
    (the hub's guest profile shows only the device's own area). Tags become a
    device adjustment of the rule areas. ``all_areas`` sends the reserved
    marker "*" (all active areas); it cannot be combined with tags, and "*"
    is not accepted as a plain tag.
    """
    tags = [t for t in data.get("tags") or [] if t.strip()]
    if data.get("all_areas"):
        if tags:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="guest_tags_and_all_areas",
            )
        return [ALL_AREAS_TAG]
    if ALL_AREAS_TAG in tags:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="guest_reserved_tag",
        )
    return tags or None


def _loaded_entry(hass: HomeAssistant) -> HubConfigEntry:
    """Return the loaded config entry or raise a translated validation error.

    Halumar allows exactly one entry per installation, so the actions take
    no config-entry field.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            return entry
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="not_loaded",
    )


def _coordinator(hass: HomeAssistant) -> HubCoordinator:
    """Coordinator of the loaded entry."""
    return _loaded_entry(hass).runtime_data


def _notify_target(
    hass: HomeAssistant,
) -> tuple[HubConfigEntry, HubCoordinator | None]:
    """Entry of the notification actions and its coordinator.

    Besides a loaded entry, an entry that waits for the hub (setup retry,
    e.g. HA restarted while the hub is down) is accepted without a
    coordinator: notifications then go straight to the device's sensor.
    """
    entries = hass.config_entries.async_entries(DOMAIN)
    for entry in entries:
        if entry.state is ConfigEntryState.LOADED:
            return entry, entry.runtime_data
    for entry in entries:
        if entry.state is ConfigEntryState.SETUP_RETRY:
            return entry, None
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="not_loaded",
    )


def _offline(hass: HomeAssistant) -> OfflineNotifications:
    return hass.data[DATA_OFFLINE_NOTIFICATIONS]


async def _hub_first(queue: OfflineNotifications, coordinator: HubCoordinator) -> bool:
    """Whether a notification change may go to the hub now.

    Changes queued while the hub was unreachable are handed over first; a
    new change never overtakes them.
    """
    if queue.has_ops:
        await queue.async_flush(coordinator)
    return not queue.has_ops


async def _queued(
    queue: OfflineNotifications, coordinator: HubCoordinator | None, action: str
) -> None:
    """Report truthfully why a change went into the queue.

    No answer: queued as designed for an unreachable hub. Another hub held:
    queued, the caller gets the error of the repair. Token rejected (401):
    queued (nothing is lost), the renewal or reauthentication starts, the
    caller gets the error. Any other answer (403, 429, 5xx): queued, logged
    once with its status.
    """
    err = queue.pause_error if coordinator is not None else None
    if isinstance(err, OtherHubError):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="other_hub_held",
            translation_placeholders={"url": err.detail or ""},
        ) from err
    if (
        coordinator is not None
        and isinstance(err, HubApiError)
        and err.status in _TOKEN_STATUSES
    ):
        await coordinator.async_token_rejected()
        if await _hub_first(queue, coordinator):
            return  # a renewed token: handed over
        err = queue.pause_error
        if isinstance(err, HubApiError) and err.status in _TOKEN_STATUSES:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="queued_token_rejected",
                translation_placeholders={"status": str(err.status)},
            ) from err
    if isinstance(err, HubApiError) and err.status is not None:
        if queue.reported_status != err.status:
            queue.reported_status = err.status
            _LOGGER.warning(
                "%s: the hub answered HTTP %s to the queued changes; this one "
                "is queued as well and handed over in order later",
                action,
                err.status,
            )
        return
    _LOGGER.info("%s: the hub is not reachable; queued for the hub", action)


def _require_known(queue: OfflineNotifications, client_id: str) -> None:
    if not queue.known(client_id):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_client",
            translation_placeholders={"client_id": client_id},
        )


# The answer that means the integration token is not accepted (a 403 is the
# hub refusing the network path, e.g. a non-host address: no reauth).
_TOKEN_STATUSES = frozenset({401})


async def _fallback_or_raise(
    err: HomeAssistantError,
    action: str,
    client_id: str,
    coordinator: HubCoordinator,
) -> None:
    """Re-raise unless the hub simply did not answer.

    A rejected token also starts the token renewal or reauthentication.
    """
    cause = err.__cause__
    if not hub_unreachable(cause):
        if isinstance(cause, HubApiError) and cause.status in _TOKEN_STATUSES:
            await coordinator.async_token_rejected()
        raise err
    _LOGGER.warning(
        "%s for %s: the hub is not reachable (%s); writing to the device's "
        "notification sensor, the hub takes it over when it is back",
        action,
        client_id,
        err.__cause__,
    )


def _require_data(coordinator: HubCoordinator) -> dict[str, Any]:
    """Return coordinator.data or raise when there is no snapshot yet."""
    data = coordinator.data
    if not data:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="no_data",
        )
    return data


# ── Client command handlers (each calls its own hub endpoint) ──────────


async def _async_navigate_to_area(call: ServiceCall) -> None:
    """Navigate a client to a specific area (or to the start screen)."""
    coordinator = _coordinator(call.hass)
    client_id: str = call.data["client_id"]
    area_id: str | None
    if call.data["home"]:
        # Home wins over area_id: the hub maps the sentinel to the
        # client's start screen.
        area_id = HOME_AREA_ID
    else:
        area_id = call.data.get("area_id")
        if not area_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="navigate_needs_area",
            )
    await hub_call(
        "navigate_to_area",
        coordinator.api_post_json(
            f"/api/integration/clients/{client_id}/navigate_to_area",
            {"area_id": area_id},
        ),
        {"client_id": client_id, "area_id": area_id},
    )
    _LOGGER.info("Navigated client %s to area %s", client_id, area_id)


async def _async_show_entity(call: ServiceCall) -> None:
    """Show an entity on a client with optional control_mode."""
    coordinator = _coordinator(call.hass)
    client_id: str = call.data["client_id"]
    entity_id: str = call.data["entity_id"]
    payload: dict[str, Any] = {"entity_id": entity_id}
    if "control_mode" in call.data:
        payload["control_mode"] = call.data["control_mode"]
    await hub_call(
        "show_entity",
        coordinator.api_post_json(
            f"/api/integration/clients/{client_id}/show_entity", payload
        ),
        {"client_id": client_id, "entity_id": entity_id},
    )
    _LOGGER.info(
        "Sent show_entity '%s' to client %s (control_mode=%s)",
        entity_id,
        client_id,
        payload.get("control_mode", "default"),
    )


async def _async_panel_announce(call: ServiceCall) -> None:
    """Announce text on a wall panel (the text is never logged)."""
    coordinator = _coordinator(call.hass)
    args = {k: call.data[k] for k in ("text", "volume", "chime") if k in call.data}
    await async_send_panel_action(
        call.hass, coordinator, call.context, call.data["client_id"], "announce", args
    )
    _LOGGER.info("Sent announce to client %s", call.data["client_id"])


async def _async_panel_show_camera(call: ServiceCall) -> None:
    """Open a camera on a wall panel or TV."""
    coordinator = _coordinator(call.hass)
    await async_send_panel_action(
        call.hass,
        coordinator,
        call.context,
        call.data["client_id"],
        "show_camera",
        {"entity_id": call.data["entity_id"]},
    )
    _LOGGER.info(
        "Sent show_camera %s to client %s",
        call.data["entity_id"],
        call.data["client_id"],
    )


async def _async_display_notification(call: ServiceCall) -> None:
    """Send a notification to one client or broadcast to all ("all").

    A notification carries a UUID chosen here (for "all" a broadcast id from
    which every device's id is derived), so a retry or the later handover can
    never double it. When the hub does not answer,
    the notification goes straight to the device's sensor and waits in the
    queue (offline_notifications.py). Watches as on the hub: "all" leaves
    them out, a notification for a watch goes to the phone that set it up.
    """
    hass = call.hass
    _entry, coordinator = _notify_target(hass)
    queue = _offline(hass)
    client_id: str = call.data["client_id"]
    title = call.data.get("title", "")
    message = call.data.get("message", "")
    if not title and not message:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="notification_needs_text",
        )
    # Forward exactly the fields the caller provided; the hub's
    # notification model supplies the defaults for everything else.
    fields: dict[str, Any] = {"title": title, "message": message}
    for field in (
        "notification_type",
        "icon",
        "icon_svg",
        "popup_mode",
        "scheduled_at",
        "priority",
        "color",
        "action_type",
        "action_target",
        "show_time",
    ):
        if field in call.data:
            fields[field] = call.data[field]
    payload: dict[str, Any] = {"client_id": client_id, **fields}
    # A broadcast carries a broadcast id; each device's id is derived from it
    # (hub and fallback alike), so a timed-out broadcast is never doubled.
    payload["notification_id"] = str(uuid.uuid4())
    ids: dict[str, str] = {}
    if client_id != BROADCAST_ALL:
        ids[client_id] = payload["notification_id"]

    if coordinator is not None and await _hub_first(queue, coordinator):
        try:
            result = await hub_call(
                "display_notification",
                coordinator.api_post_json(NOTIFY_ENDPOINT, payload),
                {"client_id": client_id},
            )
        except HomeAssistantError as err:
            await _fallback_or_raise(
                err, "display_notification", client_id, coordinator
            )
        else:
            if client_id == BROADCAST_ALL:
                _LOGGER.info(
                    "Broadcast notification to %s client(s) (%s duplicate(s))",
                    result.get("sent", 0),
                    result.get("duplicates", 0),
                )
            else:
                _LOGGER.info(
                    "Sent notification to client %s (ws_sent=%s)",
                    client_id,
                    result.get("ws_sent"),
                )
            return

    if fields.get("popup_mode") == "direct" and (
        fields.get("action_type", "none") == "none" or not fields.get("action_target")
    ):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="display_notification_failed",
            translation_placeholders={
                "client_id": client_id,
                "detail": "direct requires an area/entity/url link",
            },
        )
    if client_id == BROADCAST_ALL:
        targets = queue.active_client_ids()
        ids = {
            target: broadcast_notification_id(payload["notification_id"], target)
            for target in targets
        }
    else:
        _require_known(queue, client_id)
        recipient = queue.recipient(client_id)
        if recipient is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="watch_without_phone",
                translation_placeholders={"client_id": client_id},
            )
        if recipient != client_id:
            # Same id as the hub stores under the phone: a call the hub took
            # before its answer timed out is handed over as a duplicate.
            ids = {recipient: payload["notification_id"]}
            _LOGGER.info(
                "Queued notification for watch %s goes to its phone %s",
                client_id,
                recipient,
            )
        targets = [recipient]
    await queue.async_notify(targets, fields, ids)
    _LOGGER.info(
        "Notification written to the sensor of %d device(s) and queued for the hub",
        len(targets),
    )
    await _queued(queue, coordinator, "display_notification")


async def _async_clear_notifications(call: ServiceCall) -> None:
    """Clear all notifications on a client (queued while the hub is down)."""
    hass = call.hass
    _entry, coordinator = _notify_target(hass)
    queue = _offline(hass)
    client_id: str = call.data["client_id"]
    if coordinator is not None and await _hub_first(queue, coordinator):
        try:
            await hub_call(
                "clear_notifications",
                coordinator.api_post_json(
                    f"/api/integration/clients/{client_id}/clear_notifications", {}
                ),
                {"client_id": client_id},
            )
        except HomeAssistantError as err:
            await _fallback_or_raise(err, "clear_notifications", client_id, coordinator)
        else:
            _LOGGER.info("Cleared notifications on client %s", client_id)
            await queue.async_cleared_on_hub(client_id)
            return
    _require_known(queue, client_id)
    await queue.async_clear(client_id)
    _LOGGER.info("Notifications of client %s cleared, queued for the hub", client_id)
    await _queued(queue, coordinator, "clear_notifications")


async def _async_ack_notifications(call: ServiceCall) -> None:
    """Confirm delivery of specific notifications on a client's behalf.

    Remote fallback: a client app that cannot reach the hub WebSocket
    (away from home) received the notifications via the HA sensor and
    acknowledges them through HA instead. The hub enforces ownership
    per id and silently skips anything not pending for this client. While
    the hub is unreachable the integration confirms them in the sensor and
    hands the confirmation over later.
    """
    hass = call.hass
    entry, coordinator = _notify_target(hass)
    queue = _offline(hass)
    client_id: str = call.data["client_id"]
    notification_ids: list[str] = call.data["notification_ids"]

    # Only the device's own HA user, an admin or a system call (no user,
    # e.g. an automation) may acknowledge for a device.
    user_id = call.context.user_id
    if user_id is not None:
        device_users = coordinator.device_users if coordinator else None
        if device_users is None:
            device_users = DeviceUserManager(hass, entry, None)
            await device_users.async_setup()
        own = device_users.user_id_of(client_id)
        if user_id != own:
            user = await hass.auth.async_get_user(user_id)
            if user is None or not user.is_admin:
                raise Unauthorized(context=call.context, permission="ack_notifications")

    # Never log notification content (there is none in this payload by
    # design: ids only).
    _LOGGER.debug(
        "ack_notifications: client %s, %d id(s), requested by user %s",
        client_id,
        len(notification_ids),
        user_id,
    )
    if coordinator is not None and await _hub_first(queue, coordinator):
        try:
            result = await hub_call(
                "ack_notifications",
                coordinator.api_post_json(
                    "/api/integration/notifications/ack",
                    {"client_id": client_id, "notification_ids": notification_ids},
                ),
                {"client_id": client_id},
            )
        except HomeAssistantError as err:
            await _fallback_or_raise(err, "ack_notifications", client_id, coordinator)
        else:
            _LOGGER.info(
                "Acknowledged %d notification(s) for client %s (%d skipped)",
                len(result.get("acked", [])),
                client_id,
                len(result.get("skipped", [])),
            )
            await queue.async_acked_on_hub(client_id, notification_ids)
            return
    acked, skipped = await queue.async_ack(client_id, notification_ids)
    _LOGGER.info(
        "Acknowledged %d notification(s) for client %s (%d skipped), queued for "
        "the hub",
        len(acked),
        client_id,
        len(skipped),
    )
    await _queued(queue, coordinator, "ack_notifications")


async def _async_refresh_client(call: ServiceCall) -> None:
    """Force a client to reload all data."""
    coordinator = _coordinator(call.hass)
    client_id: str = call.data["client_id"]
    await hub_call(
        "refresh_client",
        coordinator.api_post_json(f"/api/integration/clients/{client_id}/refresh", {}),
        {"client_id": client_id},
    )
    _LOGGER.info("Sent refresh to client %s", client_id)


async def _async_kick_client(call: ServiceCall) -> None:
    """Disconnect a client's WebSocket session (client stays paired)."""
    coordinator = _coordinator(call.hass)
    client_id: str = call.data["client_id"]
    result = await hub_call(
        "kick_client",
        coordinator.api_post(f"/api/integration/clients/{client_id}/kick"),
        {"client_id": client_id},
    )
    _LOGGER.info(
        "Kicked client %s (%s connection(s) closed)", client_id, result.get("closed", 0)
    )
    await coordinator.async_request_refresh()


# ── Non-command handlers ─────────────────────────────────────────────────


async def _async_force_refresh(call: ServiceCall) -> None:
    """Force immediate data refresh of the integration coordinator."""
    await _coordinator(call.hass).async_request_refresh()


async def _async_start_guest_pairing(call: ServiceCall) -> None:
    """Start a guest pairing session via the Halumar Hub."""
    coordinator = _coordinator(call.hass)

    async def _work() -> None:
        data: dict[str, Any] = {"is_guest": True}
        if "expires_hours" in call.data:
            data["guest_expires_hours"] = call.data["expires_hours"]
        tags = _guest_tags(dict(call.data))
        if tags is not None:
            data["guest_tags"] = tags
        if "profile" in call.data:
            data["profile"] = call.data["profile"]
        # The integration token is scoped to /api/integration/*, so guest
        # pairing goes through start-pairing with guest params.
        await hub_call(
            "start_guest_pairing",
            coordinator.api_post_json("/api/integration/start-pairing", data),
            status_keys=_PAIRING_STATUS_KEYS,
        )
        # The pairing PIN is a secret and must never be logged.
        _LOGGER.debug("Guest pairing started")
        await coordinator.async_request_refresh()

    await _run_privileged(call.hass, SERVICE_START_GUEST_PAIRING, _work)


async def _async_start_pairing(call: ServiceCall) -> None:
    """Start a regular (non-guest) pairing session via the hub."""
    coordinator = _coordinator(call.hass)

    async def _work() -> None:
        data: dict[str, Any] = {"is_guest": False}
        if "profile" in call.data:
            data["profile"] = call.data["profile"]
        await hub_call(
            "start_pairing",
            coordinator.api_post_json("/api/integration/start-pairing", data),
            status_keys=_PAIRING_STATUS_KEYS,
        )
        # The pairing PIN is a secret and must never be logged.
        _LOGGER.debug("Regular pairing started")
        await coordinator.async_request_refresh()

    await _run_privileged(call.hass, SERVICE_START_PAIRING, _work)


async def _async_cancel_pairing(call: ServiceCall) -> None:
    """Cancel an open pairing window (idempotent)."""
    coordinator = _coordinator(call.hass)
    result = await hub_call(
        "cancel_pairing",
        coordinator.api_post("/api/integration/pairing/cancel"),
    )
    _LOGGER.info(
        "Pairing cancel requested (window was open: %s)",
        result.get("cancelled", False),
    )
    await coordinator.async_request_refresh()


async def _async_manage_client_tags(call: ServiceCall) -> None:
    """Add, remove, or set tags on a client."""
    coordinator = _coordinator(call.hass)
    client_id: str = call.data["client_id"]
    action: str = call.data["action"]
    tags: list[str] = call.data.get("tags", [])

    async def _work() -> None:
        if action == "set":
            new_tags = tags
        else:
            # add/remove require the current tag list; fail cleanly if the
            # coordinator has no data yet or does not know the client
            # rather than silently treating it as having no tags.
            data = _require_data(coordinator)
            client = next(
                (c for c in data.get("clients", []) if c.get("id") == client_id),
                None,
            )
            if client is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="unknown_client",
                    translation_placeholders={"client_id": client_id},
                )
            current_tags = list(client.get("tags", []))
            if action == "add":
                new_tags = list(dict.fromkeys(current_tags + tags))
            else:
                remove_set = set(tags)
                new_tags = [t for t in current_tags if t not in remove_set]

        await hub_call(
            "manage_client_tags",
            coordinator.api_post_json(
                f"/api/integration/clients/{client_id}/tags", {"tags": new_tags}
            ),
            {"client_id": client_id},
        )
        _LOGGER.info("Updated tags for client %s (%s): %s", client_id, action, new_tags)
        await coordinator.async_request_refresh()

    await _run_privileged(call.hass, SERVICE_MANAGE_CLIENT_TAGS, _work)


async def _async_extend_guest(call: ServiceCall) -> None:
    """Extend a guest client's session expiry."""
    coordinator = _coordinator(call.hass)
    client_id: str = call.data["client_id"]
    payload: dict[str, Any] = {}
    if "hours" in call.data:
        payload["hours"] = call.data["hours"]
    elif "expires_at" in call.data:
        payload["expires_at"] = call.data["expires_at"]
    else:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="extend_guest_needs_time",
        )

    async def _work() -> None:
        result = await hub_call(
            "extend_guest",
            coordinator.api_post_json(
                f"/api/integration/clients/{client_id}/extend", payload
            ),
            {"client_id": client_id},
        )
        _LOGGER.info(
            "Extended guest %s to %s", client_id, result.get("expires_at", "unknown")
        )
        await coordinator.async_request_refresh()

    await _run_privileged(call.hass, SERVICE_EXTEND_GUEST, _work)


# Custom (non-entity) services are callable by ANY authenticated user in HA,
# including non-admins. The privileged services mint pairing PINs, widen
# client access, or push navigation/content to clients, so they are
# registered admin-only (async_register_admin_service raises Unauthorized
# for non-admins).
type _Handler = Callable[[ServiceCall], Coroutine[Any, Any, None]]

_ADMIN_SERVICES: tuple[tuple[str, _Handler, Any], ...] = (
    (SERVICE_NAVIGATE_TO_AREA, _async_navigate_to_area, SCHEMA_NAVIGATE_TO_AREA),
    (SERVICE_SHOW_ENTITY, _async_show_entity, SCHEMA_SHOW_ENTITY),
    (
        SERVICE_DISPLAY_NOTIFICATION,
        _async_display_notification,
        SCHEMA_DISPLAY_NOTIFICATION,
    ),
    (SERVICE_CLEAR_NOTIFICATIONS, _async_clear_notifications, SCHEMA_CLIENT_ONLY),
    (SERVICE_REFRESH_CLIENT, _async_refresh_client, SCHEMA_CLIENT_ONLY),
    (SERVICE_KICK_CLIENT, _async_kick_client, SCHEMA_CLIENT_ONLY),
    (SERVICE_START_PAIRING, _async_start_pairing, SCHEMA_START_PAIRING),
    (
        SERVICE_START_GUEST_PAIRING,
        _async_start_guest_pairing,
        SCHEMA_START_GUEST_PAIRING,
    ),
    (SERVICE_CANCEL_PAIRING, _async_cancel_pairing, SCHEMA_EMPTY),
    (SERVICE_MANAGE_CLIENT_TAGS, _async_manage_client_tags, SCHEMA_MANAGE_CLIENT_TAGS),
    (SERVICE_EXTEND_GUEST, _async_extend_guest, SCHEMA_EXTEND_GUEST),
    (SERVICE_PANEL_ANNOUNCE, _async_panel_announce, SCHEMA_PANEL_ANNOUNCE),
    (SERVICE_PANEL_SHOW_CAMERA, _async_panel_show_camera, SCHEMA_PANEL_SHOW_CAMERA),
)


async def _async_apply_rules(call: ServiceCall) -> ServiceResponse:
    """Apply rule snapshots (admin): same checks as the hub push."""
    coordinator = _coordinator(call.hass)
    manager: EnforcementManager = call.hass.data[DATA_ENFORCEMENT]
    if coordinator.device_users is None:  # pragma: no cover - set in setup
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_loaded"
        )
    applied = await manager.async_apply(
        call.data["snapshots"],
        call.data["removed"],
        call.data["complete"],
        coordinator.device_users,
        call.data["control_paths"],
    )
    if applied != len(call.data["snapshots"]):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="rules_rejected",
            translation_placeholders={
                "rejected": str(len(call.data["snapshots"]) - applied)
            },
        )
    hashes: dict[str, Any] = dict(manager.hashes())
    return {"applied": applied, "rules_hashes": hashes}


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions of the Halumar integration."""
    # control and report_location are called by the device users of the
    # apps (not admins); each handler checks the caller against the rules.
    hass.services.async_register(
        DOMAIN,
        SERVICE_CONTROL,
        async_control,
        schema=SCHEMA_CONTROL,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REPORT_LOCATION,
        async_report_location,
        schema=SCHEMA_REPORT_LOCATION,
        supports_response=SupportsResponse.OPTIONAL,
    )
    for name, handler, schema in _ADMIN_SERVICES:
        async_register_admin_service(hass, DOMAIN, name, handler, schema=schema)
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_APPLY_RULES,
        _async_apply_rules,
        schema=SCHEMA_APPLY_RULES,
        supports_response=SupportsResponse.OPTIONAL,
    )
    # ack_notifications is deliberately NOT admin-only: its expected caller is
    # a client's own non-admin device user, on whose HA connection remote
    # client apps confirm delivery. The handler accepts only that user, an
    # admin or a system call.
    hass.services.async_register(
        DOMAIN,
        SERVICE_ACK_NOTIFICATIONS,
        _async_ack_notifications,
        schema=SCHEMA_ACK_NOTIFICATIONS,
    )
    # force_refresh only re-polls the hub locally and leaks nothing, so it
    # stays available to any authenticated user.
    hass.services.async_register(
        DOMAIN,
        SERVICE_FORCE_REFRESH,
        _async_force_refresh,
        schema=SCHEMA_EMPTY,
    )
