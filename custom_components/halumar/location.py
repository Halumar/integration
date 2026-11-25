"""Location of the devices (contracts docs/location.md).

Two ways reach the device_tracker of a device:

* ``halumar.report_location``: devices with a Home Assistant key report
  their position, at home and away. Like ``halumar.control`` every
  authenticated user may call it, so read-only device users (control path
  "checked") never hit HA's own permission check (a 401 there counts as a
  failed login). The action resolves the caller to its device and
  rule snapshot, checks the rule ``location`` (limit, background), the age of
  the fix and one report per 5 s, updates the tracker and tells the hub only
  the metadata (``POST /api/integration/location-status``).
* ``client_location`` on the push session: positions of devices on the
  hub path that the hub accepted (devices without a key). The rule of the
  snapshot is checked again.

Positions never go to the hub and never into the log.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SERVICE_REPORT_LOCATION, SIGNAL_LOCATION
from .device_users import async_device_client_id
from .enforcement import DATA_ENFORCEMENT, EnforcementManager
from .snapshot import is_uuid

try:
    import probatio as vol
except ImportError:  # pragma: no cover - HA < 2026.9 validates with voluptuous
    import voluptuous as vol  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

DATA_LOCATION = f"{DOMAIN}_location"
STATUS_ENDPOINT = "/api/integration/location-status"
MSG_CLIENT_LOCATION = "client_location"

MIN_INTERVAL = 5.0
STALE_AFTER = timedelta(minutes=15)
FUTURE_SKEW = timedelta(minutes=5)
RULE_VALUES = ("off", "foreground", "background")
SHARED = ("none", "foreground", "background")
FIX_KEYS = (
    "latitude",
    "longitude",
    "accuracy",
    "altitude",
    "vertical_accuracy",
    "speed",
    "course",
)


def _finite(value: Any) -> float:
    if isinstance(value, bool):
        raise vol.Invalid("not a number")
    number = float(value)
    if not math.isfinite(number):
        raise vol.Invalid("not a finite number")
    return number


def _number(low: float, high: float) -> Any:
    return vol.All(_finite, vol.Range(min=low, max=high))


def _optional(low: float, high: float) -> Any:
    return vol.Any(None, _number(low, high))


def _aware(value: Any) -> datetime:
    parsed = dt_util.parse_datetime(cv.string(value))
    if parsed is None or parsed.tzinfo is None:
        raise vol.Invalid("ts must be an ISO 8601 time with offset")
    return parsed


SCHEMA_REPORT_LOCATION = vol.Schema(
    {
        vol.Required("latitude"): _number(-90, 90),
        vol.Required("longitude"): _number(-180, 180),
        vol.Required("accuracy"): _number(0, 100000),
        vol.Optional("altitude"): _optional(-1000, 100000),
        vol.Optional("vertical_accuracy"): _optional(0, 100000),
        vol.Optional("speed"): _optional(0, 1000),
        vol.Optional("course"): _optional(0, 360),
        vol.Optional("battery_level"): vol.Any(
            None, vol.All(vol.Coerce(int), vol.Range(min=0, max=100))
        ),
        vol.Required("background"): cv.boolean,
        vol.Required("ts"): _aware,
        vol.Optional("location_shared"): vol.Any(None, vol.In(SHARED)),
    }
)


@dataclass
class LocationState:
    """Per device: last accepted report (monotonic) and newest fix time."""

    accepted_at: dict[str, float] = field(default_factory=dict)
    newest: dict[str, datetime] = field(default_factory=dict)


def _state(hass: HomeAssistant) -> LocationState:
    state: LocationState | None = hass.data.get(DATA_LOCATION)
    if state is None:
        state = hass.data[DATA_LOCATION] = LocationState()
    return state


def _denied(reason: str) -> ServiceValidationError:
    placeholders = {}
    if reason == "rate_limited":
        # Shared translation with the other actions.
        placeholders = {
            "service": f"{DOMAIN}.{SERVICE_REPORT_LOCATION}",
            "wait": str(int(MIN_INTERVAL)),
        }
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=reason,
        translation_placeholders=placeholders,
    )


def rule_of(manager: EnforcementManager, client_id: str) -> str:
    """Rule ``location`` of the device's snapshot (off without one)."""
    snap = manager.snapshots.get(client_id) or {}
    value = snap.get("location")
    return value if value in RULE_VALUES else "off"


def check(rule: str, background: bool, ts: datetime, now: datetime) -> str | None:
    """Reason a fix is refused, None = allowed (same checks as the hub)."""
    if ts < now - STALE_AFTER or ts > now + FUTURE_SKEW:
        return "location_stale"
    if rule == "off":
        return "location_not_allowed"
    if background and rule != "background":
        return "location_background_not_allowed"
    return None


def _fix(data: dict[str, Any], ts: datetime) -> dict[str, Any]:
    fix = {key: data.get(key) for key in FIX_KEYS}
    fix["ts"] = dt_util.as_utc(ts).isoformat()
    return fix


async def _async_tell_hub(
    manager: EnforcementManager, client_id: str, data: dict[str, Any], ts: datetime
) -> None:
    """Metadata of an accepted report to the hub; best effort, never coordinates."""
    coordinator = manager.coordinator()
    if coordinator is None:
        return
    body = {
        "client_id": client_id,
        "background": data["background"],
        "ts": dt_util.as_utc(ts).isoformat(),
        "location_shared": data.get("location_shared"),
        "battery_level": data.get("battery_level"),
    }
    try:
        await coordinator.api_post_json(STATUS_ENDPOINT, body)
    except Exception as err:  # noqa: BLE001 - best effort, the hub may be away
        _LOGGER.debug("Location status not delivered to the hub: %s", err)


async def async_report_location(call: ServiceCall) -> ServiceResponse:
    """Handle halumar.report_location (registered in services.py)."""
    hass = call.hass
    manager: EnforcementManager | None = hass.data.get(DATA_ENFORCEMENT)
    if manager is None:  # pragma: no cover - registered after the manager
        raise _denied("rules_missing")
    user_id = call.context.user_id
    user = await hass.auth.async_get_user(user_id) if user_id else None
    if user is None:
        raise _denied("not_a_device")
    client_id = manager.client_of_user(user.id)
    if client_id is None:
        raise _denied(
            "rules_missing"
            if await async_device_client_id(hass, user)
            else "not_a_device"
        )
    data = call.data
    ts: datetime = data["ts"]
    reason = check(
        rule_of(manager, client_id), data["background"], ts, dt_util.utcnow()
    )
    state = _state(hass)
    now = hass.loop.time()
    if reason is None:
        last = state.accepted_at.get(client_id)
        if last is not None and now - last < MIN_INTERVAL:
            reason = "rate_limited"
    if reason is not None:
        _LOGGER.info(
            "Location of %s refused: %s (background=%s)",
            client_id,
            reason,
            data["background"],
        )
        raise _denied(reason)
    state.accepted_at[client_id] = now
    newest = state.newest.get(client_id)
    if newest is not None and ts <= newest:
        return {"applied": False}
    state.newest[client_id] = ts
    async_dispatcher_send(hass, SIGNAL_LOCATION, client_id, _fix(data, ts))
    _LOGGER.debug(
        "Location of %s accepted (background=%s)", client_id, data["background"]
    )
    hass.async_create_background_task(
        _async_tell_hub(manager, client_id, dict(data), ts),
        name=f"{DOMAIN}-location-status",
    )
    return {"applied": True}


def _push_fix(raw: Any) -> tuple[dict[str, Any], datetime] | None:
    """A location_fix of the push session and its time, None when malformed."""
    if not isinstance(raw, dict) or set(raw) - {*FIX_KEYS, "ts"}:
        return None
    limits = {
        "latitude": (-90, 90),
        "longitude": (-180, 180),
        "accuracy": (0, 100000),
        "altitude": (-1000, 100000),
        "vertical_accuracy": (0, 100000),
        "speed": (0, 1000),
        "course": (0, 360),
    }
    fix: dict[str, Any] = {}
    for key, (low, high) in limits.items():
        value = raw.get(key)
        if value is None:
            if key in ("latitude", "longitude", "accuracy"):
                return None
            fix[key] = None
            continue
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        if not math.isfinite(value) or not low <= value <= high:
            return None
        fix[key] = float(value)
    ts = dt_util.parse_datetime(raw["ts"]) if isinstance(raw.get("ts"), str) else None
    if ts is None or ts.tzinfo is None:
        return None
    fix["ts"] = dt_util.as_utc(ts).isoformat()
    return fix, ts


@callback
def async_handle_push(hass: HomeAssistant, message: dict[str, Any]) -> bool:
    """``client_location`` from the hub; False = another message type."""
    if message.get("type") != MSG_CLIENT_LOCATION:
        return False
    manager: EnforcementManager | None = hass.data.get(DATA_ENFORCEMENT)
    data = message.get("data")
    if manager is None or not isinstance(data, dict):
        return True
    client_id = data.get("client_id")
    parsed = _push_fix(data.get("location"))
    if not isinstance(client_id, str) or not is_uuid(client_id) or parsed is None:
        _LOGGER.debug("Ignoring invalid client_location")
        return True
    if rule_of(manager, client_id) == "off":
        _LOGGER.info("Ignoring client_location of %s: location is off", client_id)
        return True
    fix, ts = parsed
    state = _state(hass)
    newest = state.newest.get(client_id)
    if newest is not None and ts <= newest:
        return True
    state.newest[client_id] = ts
    async_dispatcher_send(hass, SIGNAL_LOCATION, client_id, fix)
    return True
