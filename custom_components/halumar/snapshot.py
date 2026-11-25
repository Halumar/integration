"""Rule snapshots of the devices (contracts common/enforcement-snapshot.json).

The hub resolves every rule of a device (profile, adjustments, boundaries,
areas, "when away", quick access, kids rules) into one snapshot and hands it
to the integration; halumar.control decides with it locally, also while the
hub is unreachable. This module only validates snapshots and answers the
time based kids questions; it never trusts a snapshot it cannot verify.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.util import dt as dt_util

SNAPSHOT_VERSION = 1
MAX_ENTITIES = 10000

CONTROL_PATH_CHECKED = "checked"
CONTROL_PATH_DIRECT = "direct"

LEVELS = ("hidden", "view", "control")
QUICK_LEVELS = ("off", "status", "control")
_REMOTE_TO_QUICK = {"hidden": "off", "view": "status", "control": "control"}

_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_USER_ID = re.compile(r"^[0-9a-f]{32}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")
_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_HHMM = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_TOP_KEYS = frozenset(
    {
        "version",
        "client_id",
        "ha_user_id",
        "issued_at",
        "rules_hash",
        "control_path",
        "remote_access",
        "child_protection",
        "entities",
        "kids",
        "access",
        "home_networks",
        "location",
    }
)
# Rule location: limit of halumar.report_location and client_location.
LOCATION_VALUES = ("off", "foreground", "background")
# Further networks that count as at home: only inside these ranges.
MAX_HOME_NETWORKS = 10
_HOME_RANGES = tuple(
    ipaddress.ip_network(n)
    for n in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "fc00::/7",
    )
)
# Access switch and access hours; early access.
_ACCESS_KEYS = frozenset({"switch", "hours", "windows", "time_zone", "early_until"})


# Kids keys; the releases of the family manager and the device-wide
# pause of everything are optional.
_KIDS_KEYS = frozenset(
    {
        "time_zone",
        "override_until",
        "entities",
        "bedtime_released_until",
        "pause_released_until",
        "rate_limit_all",
    }
)
_RATE_ALL_KEYS = frozenset(
    {"enabled", "max_actions", "max_entities", "window_seconds", "cooldown_seconds"}
)
_KIDS_ENTITY_KEYS = frozenset(
    {"blocked", "quiet_hours", "color_lock", "volume_max", "rate_limit"}
)


def _optional_time(value: Any) -> bool:
    return value is None or (isinstance(value, str) and parse_utc(value) is not None)


def released(value: str | None, now: datetime) -> bool:
    """A release (timestamp) is running at ``now``; a past time is none."""
    until = parse_utc(value)
    return until is not None and until > now


class SnapshotError(ValueError):
    """A snapshot is malformed or its hash does not match."""


def is_uuid(value: Any) -> bool:
    """Whether value is a client id (UUID)."""
    return isinstance(value, str) and bool(_UUID.match(value))


def rules_hash(snapshot: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON without rules_hash and issued_at."""
    body = {k: v for k, v in snapshot.items() if k not in ("rules_hash", "issued_at")}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check(cond: bool, message: str) -> None:
    if not cond:
        raise SnapshotError(message)


def _check_entity_access(entity_id: str, access: Any) -> None:
    _check(bool(_ENTITY_ID.match(entity_id)), f"bad entity id {entity_id!r}")
    _check(isinstance(access, dict), f"{entity_id}: access must be an object")
    _check(
        set(access) <= {"local", "remote", "quick", "run"}
        and {"local", "remote", "quick"} <= set(access),
        f"{entity_id}: unexpected keys",
    )
    _check(access["local"] in LEVELS, f"{entity_id}: bad local level")
    _check(access["remote"] in LEVELS, f"{entity_id}: bad remote level")
    _check(access["quick"] in QUICK_LEVELS, f"{entity_id}: bad quick level")
    _check(isinstance(access.get("run", False), bool), f"{entity_id}: bad run")


def _check_window(window: Any) -> None:
    _check(
        isinstance(window, dict) and set(window) == {"days", "start", "end"},
        "bad quiet hours window",
    )
    days = window["days"]
    _check(
        isinstance(days, list)
        and 1 <= len(days) <= 7
        and len(set(days)) == len(days)
        and all(d in _DAYS for d in days),
        "bad quiet hours days",
    )
    for key in ("start", "end"):
        _check(
            isinstance(window[key], str) and bool(_HHMM.match(window[key])),
            "bad quiet hours time",
        )


def _check_kids(kids: Any, entities: dict[str, Any]) -> None:
    _check(
        isinstance(kids, dict)
        and {"time_zone", "override_until", "entities"} <= set(kids)
        and set(kids) <= _KIDS_KEYS,
        "kids: unexpected keys",
    )
    for key in ("bedtime_released_until", "pause_released_until"):
        _check(_optional_time(kids.get(key)), f"kids: bad {key}")
    rate_all = kids.get("rate_limit_all")
    _check(
        rate_all is None
        or (
            isinstance(rate_all, dict)
            and set(rate_all) == _RATE_ALL_KEYS
            and isinstance(rate_all["enabled"], bool)
            and all(
                isinstance(rate_all[k], int)
                and not isinstance(rate_all[k], bool)
                and rate_all[k] >= 1
                for k in _RATE_ALL_KEYS - {"enabled"}
            )
        ),
        "kids: bad rate_limit_all",
    )
    _check(
        kids["time_zone"] is None
        or (isinstance(kids["time_zone"], str) and len(kids["time_zone"]) <= 64),
        "kids: bad time_zone",
    )
    override = kids["override_until"]
    _check(
        override is None
        or (isinstance(override, str) and parse_utc(override) is not None),
        "kids: bad override_until",
    )
    k_entities = kids["entities"]
    _check(isinstance(k_entities, dict), "kids: entities must be an object")
    for entity_id, rules in k_entities.items():
        _check(entity_id in entities, f"kids: {entity_id} is not in entities")
        _check(
            isinstance(rules, dict)
            and _KIDS_ENTITY_KEYS <= set(rules)
            and set(rules) <= _KIDS_ENTITY_KEYS | {"release_until"},
            f"kids {entity_id}: unexpected keys",
        )
        _check(_optional_time(rules.get("release_until")), "kids: bad release_until")
        _check(isinstance(rules["blocked"], bool), "kids: bad blocked")
        _check(isinstance(rules["color_lock"], bool), "kids: bad color_lock")
        _check(
            isinstance(rules["quiet_hours"], list) and len(rules["quiet_hours"]) <= 14,
            "kids: bad quiet_hours",
        )
        for window in rules["quiet_hours"]:
            _check_window(window)
        vmax = rules["volume_max"]
        _check(
            vmax is None
            or (
                isinstance(vmax, int)
                and not isinstance(vmax, bool)
                and 0 <= vmax <= 100
            ),
            "kids: bad volume_max",
        )
        rate = rules["rate_limit"]
        _check(
            rate is None
            or (
                isinstance(rate, dict)
                and set(rate) == {"max_actions", "window_seconds", "cooldown_seconds"}
                and all(
                    isinstance(v, int) and not isinstance(v, bool) and v >= 0
                    for v in rate.values()
                )
            ),
            "kids: bad rate_limit",
        )


def _check_access(access: Any) -> None:
    """Snapshot field access: null or switch, hours, windows, time zone."""
    if access is None:
        return
    _check(
        isinstance(access, dict) and set(access) == _ACCESS_KEYS,
        "access: unexpected keys",
    )
    _check(isinstance(access["switch"], bool), "access: bad switch")
    _check(isinstance(access["hours"], bool), "access: bad hours")
    windows = access["windows"]
    _check(isinstance(windows, list) and len(windows) <= 14, "access: bad windows")
    _check(access["hours"] or not windows, "access: windows without hours")
    for window in windows:
        _check_window(window)
    tz = access["time_zone"]
    _check(
        tz is None or (isinstance(tz, str) and len(tz) <= 64), "access: bad time_zone"
    )
    early = access["early_until"]
    _check(
        early is None
        or (
            isinstance(early, str) and len(early) <= 40 and parse_utc(early) is not None
        ),
        "access: bad early_until",
    )


def _check_home_networks(value: Any) -> None:
    _check(
        isinstance(value, list) and len(value) <= MAX_HOME_NETWORKS,
        "bad home_networks",
    )
    for cidr in value:
        try:
            net = ipaddress.ip_network(cidr) if isinstance(cidr, str) else None
        except ValueError:
            net = None
        _check(
            net is not None
            and str(net) == cidr
            and any(
                net.version == r.version and net.subnet_of(r)  # type: ignore[arg-type]
                for r in _HOME_RANGES
            ),
            "home_networks: not a private network in canonical form",
        )


def in_home_networks(networks: list[str], address: str) -> bool:
    """Whether an IP address lies inside one of the home networks.

    IPv4-mapped IPv6 addresses count as IPv4; brackets and zone ids are ignored.
    """
    try:
        ip = ipaddress.ip_address(address.strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in ipaddress.ip_network(cidr) for cidr in networks)


def validate_snapshot(raw: Any) -> dict[str, Any]:
    """Return the snapshot if it is well-formed and its hash matches."""
    _check(isinstance(raw, dict), "snapshot must be an object")
    snapshot: dict[str, Any] = raw
    _check(set(snapshot) == _TOP_KEYS, "unexpected or missing keys")
    _check(snapshot["version"] == SNAPSHOT_VERSION, "unknown snapshot version")
    _check(is_uuid(snapshot["client_id"]), "bad client_id")
    _check(
        isinstance(snapshot["ha_user_id"], str)
        and bool(_USER_ID.match(snapshot["ha_user_id"])),
        "bad ha_user_id",
    )
    _check(isinstance(snapshot["issued_at"], str), "bad issued_at")
    _check(
        isinstance(snapshot["rules_hash"], str)
        and bool(_HASH.match(snapshot["rules_hash"])),
        "bad rules_hash",
    )
    _check(
        snapshot["control_path"] in (CONTROL_PATH_CHECKED, CONTROL_PATH_DIRECT),
        "bad control_path",
    )
    for key in ("remote_access", "child_protection"):
        _check(isinstance(snapshot[key], bool), f"bad {key}")
    entities = snapshot["entities"]
    _check(
        isinstance(entities, dict) and len(entities) <= MAX_ENTITIES,
        "bad entities",
    )
    for entity_id, access in entities.items():
        _check_entity_access(entity_id, access)
    if snapshot["child_protection"]:
        _check(snapshot["control_path"] == CONTROL_PATH_CHECKED, "kids: not checked")
        _check_kids(snapshot["kids"], entities)
    else:
        _check(snapshot["kids"] is None, "kids without parental controls")
    _check_access(snapshot["access"])
    _check_home_networks(snapshot["home_networks"])
    _check(snapshot["location"] in LOCATION_VALUES, "bad location")
    _check(rules_hash(snapshot) == snapshot["rules_hash"], "rules_hash mismatch")
    return snapshot


def access_level(access: dict[str, Any], route: str, remote_access: bool) -> str:
    """Level of an entity on a route (local, external, unknown = external)."""
    if route == "local":
        return str(access["local"])
    return str(access["remote"]) if remote_access else "hidden"


def quick_level(access: dict[str, Any], route: str, remote_access: bool) -> str:
    """Quick access level, on the external route capped by the remote level."""
    quick = str(access["quick"])
    if route == "local":
        return quick
    mapped = _REMOTE_TO_QUICK[access_level(access, route, remote_access)]
    return QUICK_LEVELS[min(QUICK_LEVELS.index(quick), QUICK_LEVELS.index(mapped))]


# ── kids time windows (same rules as the hub, services/kids_rules.py) ────


UTC = timezone.utc


def parse_utc(value: str | None) -> datetime | None:
    """Aware datetime of an ISO timestamp; naive = UTC; None if invalid."""
    if not value:
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _zone(name: str | None) -> ZoneInfo | timezone:
    if not name:
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def _hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _spans(
    windows: list[dict[str, Any]], day: datetime, tz: ZoneInfo | timezone
) -> list[tuple[datetime, datetime]]:
    weekday = _DAYS[day.weekday()]
    spans = []
    for window in windows:
        if weekday not in window["days"]:
            continue
        start_t, end_t = _hhmm(window["start"]), _hhmm(window["end"])
        start = datetime.combine(day.date(), start_t, tzinfo=tz)
        end_day = day.date() if end_t > start_t else day.date() + timedelta(days=1)
        end = datetime.combine(end_day, end_t, tzinfo=tz)
        if end.astimezone(UTC) > start.astimezone(UTC):
            spans.append((start.astimezone(UTC), end.astimezone(UTC)))
    return spans


def quiet_until(
    windows: list[dict[str, Any]], now: datetime, tz_name: str | None
) -> datetime | None:
    """End (UTC) of the bedtime ``now`` is in, None outside."""
    if not windows:
        return None
    tz = _zone(tz_name)
    local = now.astimezone(tz)
    spans: list[tuple[datetime, datetime]] = []
    for offset in range(-1, 15):
        spans.extend(_spans(windows, local + timedelta(days=offset), tz))
    spans.sort()
    until = None
    point = now.astimezone(UTC)
    for _ in range(len(spans) + 1):
        covering = [end for start, end in spans if start <= point < end]
        if not covering:
            break
        point = max(covering)
        until = point
    return until


def next_start(
    windows: list[dict[str, Any]], now: datetime, tz_name: str | None
) -> datetime | None:
    """Start (UTC) of the next window within 8 days; None inside one."""
    if not windows or quiet_until(windows, now, tz_name) is not None:
        return None
    tz = _zone(tz_name)
    local = now.astimezone(tz)
    point = now.astimezone(UTC)
    starts = [
        start
        for offset in range(0, 8)
        for start, _end in _spans(windows, local + timedelta(days=offset), tz)
        if start > point
    ]
    return min(starts) if starts else None


def access_closed(
    access: dict[str, Any] | None, now: datetime
) -> tuple[bool, datetime | None]:
    """(closed, opens_at) of a device's snapshot access.

    Closed while the switch is off (opens_at unknown) or, with access hours,
    while ``now`` is outside every window (opens_at = next window start) and
    no early access runs (a Yes to the device's request opens it until
    ``early_until``, with the integration's own clock).
    """
    if access is None:
        return False, None
    if not access["switch"]:
        return True, None
    if not access["hours"]:
        return False, None
    early = parse_utc(access.get("early_until"))
    if early is not None and now < early:
        return False, None
    windows = access["windows"]
    if quiet_until(windows, now, access["time_zone"]) is not None:
        return False, None
    return True, next_start(windows, now, access["time_zone"])
