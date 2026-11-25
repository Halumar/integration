"""Enforcement in Home Assistant.

halumar.control (allowlist, rule snapshot, routes, groups, batches, kids
rules, execution as the system), rule snapshots (validation, own device
users only, push, poll backstop), read-only device users with the
user_updated watcher and the start check, outage and direct-attempt
detection, and the events for the hub log and the HA event bus. Many
negative cases: every way around the rules must fail closed.
"""

from __future__ import annotations

import copy
import logging
from collections.abc import Generator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from aioresponses import CallbackResult, aioresponses
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_READ_ONLY, GROUP_ID_USER
from homeassistant.auth.models import User
from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from yarl import URL

from custom_components.halumar.allowlist import ALLOWLIST_FILE, Allowlist
from custom_components.halumar.const import DOMAIN
from custom_components.halumar.device_users import (
    DeviceUserSafetyError,
    DeviceIdentity,
    assert_safe_device_user,
)
from custom_components.halumar.enforcement import (
    DATA_ENFORCEMENT,
    DIRECT_THRESHOLD,
    ISSUE_OUTAGE,
    ISSUE_RESTORED,
    ISSUE_SERVICE_CALLED,
    AWAITING_GRACE,
    ISSUE_UNREADABLE,
    STORAGE_KEY,
    WS_LOGGER,
    EnforcementManager,
)
from custom_components.halumar.enforcement_sync import async_handle_push
from custom_components.halumar.events import MAX_QUEUE
from custom_components.halumar.snapshot import (
    SnapshotError,
    quiet_until,
    rules_hash,
    validate_snapshot,
)

from ._ha import MockConfigEntry, MockUser, async_capture_events
from .common import BASE_URL, CLIENT_ID, GUEST_ID
from .test_device_users import (
    FakeHub,
    _poll,
    _provisioned,
    _request,
    _settle,
    _user_of,
)

EVENTS_URL = f"{BASE_URL}/api/integration/events"
KIDS_URL = f"{BASE_URL}/api/integration/kids-check"
RULES_URL = f"{BASE_URL}/api/integration/enforcement-rules"

CONTROL = "custom_components.halumar.control.connection_route"


@pytest.fixture
def existing_owner(hass: HomeAssistant) -> Generator[MockUser, None, None]:
    """A human owner exists, as in every real installation."""
    yield MockUser(is_owner=True, name="Primary Owner").add_to_hass(hass)


def access(
    local: str = "control",
    remote: str = "hidden",
    quick: str = "off",
    run: bool | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {"local": local, "remote": remote, "quick": quick}
    if run is not None:
        item["run"] = run
    return item


def _paths(snapshots: Any) -> dict[str, Any]:
    """control_paths as the hub sends it next to these snapshots."""
    return {
        s["client_id"]: s["control_path"]
        for s in snapshots or []
        if isinstance(s, dict) and "client_id" in s and "control_path" in s
    }


def make_snapshot(
    client_id: str,
    user_id: str,
    entities: dict[str, Any],
    *,
    control_path: str = "checked",
    remote_access: bool = False,
    kids: dict[str, Any] | None = None,
    access: dict[str, Any] | None = None,
    home_networks: list[str] | None = None,
    location: str = "off",
) -> dict[str, Any]:
    snap = {
        "version": 1,
        "client_id": client_id,
        "ha_user_id": user_id,
        "issued_at": "2026-11-20T18:00:00+00:00",
        "rules_hash": "",
        "control_path": control_path,
        "remote_access": remote_access,
        "child_protection": kids is not None,
        "entities": entities,
        "kids": kids,
        "access": access,
        "home_networks": home_networks or [],
        "location": location,
    }
    snap["rules_hash"] = rules_hash(snap)
    return snap


def kids_rules(
    *,
    blocked: bool = False,
    quiet: list[dict[str, Any]] | None = None,
    color_lock: bool = False,
    volume_max: int | None = None,
    rate: bool = False,
) -> dict[str, Any]:
    return {
        "blocked": blocked,
        "quiet_hours": quiet or [],
        "color_lock": color_lock,
        "volume_max": volume_max,
        "rate_limit": {"max_actions": 6, "window_seconds": 60, "cooldown_seconds": 300}
        if rate
        else None,
    }


ALL_DAY = [
    {
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "start": "00:00",
        "end": "00:00",
    }
]

ENTITIES = {
    "light.bed": access(remote="control", quick="control"),
    "light.ceiling": access(),
    "light.group_ok": access(),
    "light.group_bad": access(),
    "lock.front": access(remote="view", quick="status"),
    "cover.garage": access(local="view"),
    "scene.movie": access(run=True),
    "scene.hidden_effects": access(),
    "media_player.tv": access(),
    "input_boolean.a": access(remote="control"),
}


class Env:
    """A loaded entry with one provisioned device user and recorded services."""

    def __init__(
        self, hass: HomeAssistant, entry: MockConfigEntry, hub: FakeHub, user: User
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.hub = hub
        self.user = user
        self.calls: list[ServiceCall] = []

    @property
    def manager(self) -> EnforcementManager:
        manager: EnforcementManager = self.hass.data[DATA_ENFORCEMENT]
        return manager

    async def apply(
        self, snap: dict[str, Any] | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        snap = snap or make_snapshot(CLIENT_ID, self.user.id, ENTITIES, **kwargs)
        result: dict[str, Any] = await self.hass.services.async_call(
            DOMAIN,
            "apply_rules",
            {"snapshots": [snap], "control_paths": _paths([snap])},
            blocking=True,
            return_response=True,
        )
        await _settle(self.hass)
        return result

    async def control(
        self, route: str = "local", user_id: str | None = "device", **data: Any
    ) -> Any:
        uid = self.user.id if user_id == "device" else user_id
        with patch(CONTROL, return_value=route):
            return await self.hass.services.async_call(
                DOMAIN,
                "control",
                data,
                blocking=True,
                context=Context(user_id=uid),
                return_response=data.pop("_response", True),
            )

    async def denied(
        self, reason: str, route: str = "local", **data: Any
    ) -> ServiceValidationError:
        with pytest.raises(ServiceValidationError) as err:
            await self.control(route=route, _response=False, **data)
        assert err.value.translation_key == reason, err.value.translation_key
        return err.value


@pytest.fixture
async def env(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> Env:
    entry, hub = await _provisioned(hass, mock_api, CLIENT_ID)
    user = await _user_of(hass, entry, CLIENT_ID)
    result = Env(hass, entry, hub, user)

    def record(call: ServiceCall) -> None:
        result.calls.append(call)

    for domain, actions in {
        "light": ("turn_on", "turn_off", "toggle"),
        "lock": ("lock", "unlock", "open"),
        "cover": ("open_cover", "close_cover"),
        "scene": ("turn_on",),
        "media_player": ("volume_set", "volume_up", "volume_mute", "media_play"),
        "camera": ("snapshot",),
    }.items():
        for action in actions:
            hass.services.async_register(domain, action, record)
    for entity_id in ENTITIES:
        if not entity_id.startswith("input_boolean"):
            hass.states.async_set(entity_id, "off")
    hass.states.async_set(
        "light.group_ok", "off", {"entity_id": ["light.bed", "light.ceiling"]}
    )
    hass.states.async_set(
        "light.group_bad", "off", {"entity_id": ["light.bed", "light.not_listed"]}
    )
    hass.states.async_set("light.not_listed", "off")
    hass.states.async_set("media_player.tv", "playing", {"volume_level": 0.35})
    hass.states.async_set("camera.door", "idle")
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"a": {}, "b": {}}}
    )
    mock_api.post(EVENTS_URL, payload={"accepted": 1, "duplicates": 0}, repeat=True)
    return result


# ── allowlist ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("domain", "action", "data", "reason"),
    [
        ("light", "turn_on", {"brightness_pct": 40, "color_temp_kelvin": 2700}, None),
        ("light", "turn_on", {"rgb_color": [255, 0, 0], "transition": 1.5}, None),
        ("light", "turn_on", {"entity_id": "light.other"}, "not_allowed_field"),
        ("light", "turn_on", {"area_id": "kitchen"}, "not_allowed_field"),
        ("light", "turn_on", {"target": {"entity_id": "light.x"}}, "not_allowed_field"),
        ("light", "turn_on", {"profile": "relax"}, "not_allowed_field"),
        ("light", "turn_on", {"brightness": 256}, "not_allowed_value"),
        ("light", "turn_on", {"brightness": True}, "not_allowed_value"),
        ("light", "turn_on", {"brightness": "50"}, "not_allowed_value"),
        ("light", "turn_on", {"brightness": 1.5}, "not_allowed_value"),
        ("light", "turn_on", {"transition": float("nan")}, "not_allowed_value"),
        ("light", "turn_on", {"rgb_color": [255, 0]}, "not_allowed_value"),
        ("light", "turn_on", {"rgb_color": [255, 0, 300]}, "not_allowed_value"),
        ("light", "turn_on", {"flash": "forever"}, "not_allowed_value"),
        ("light", "set_level", {}, "not_allowed_action"),
        (
            "media_player",
            "join",
            {"group_members": ["media_player.x"]},
            "not_allowed_action",
        ),
        ("media_player", "play_media", {"media_content_id": "x"}, "not_allowed_action"),
        ("media_player", "volume_set", {"volume_level": 0.5}, None),
        ("media_player", "volume_set", {"volume_level": 2}, "not_allowed_value"),
        ("scene", "apply", {"entities": {}}, "not_allowed_action"),
        ("scene", "create", {}, "not_allowed_action"),
        (
            "script",
            "turn_on",
            {"variables": {"target": "lock.front"}},
            "not_allowed_field",
        ),
        ("lock", "unlock", {"code": "1234"}, None),
        ("lock", "unlock", {"code": 1234}, "not_allowed_value"),
        ("camera", "snapshot", {"filename": "/config/x.jpg"}, "not_allowed_action"),
        ("homeassistant", "turn_on", {}, "not_allowed_action"),
        ("shell_command", "x", {}, "not_allowed_action"),
        ("input_datetime", "set_datetime", {"date": "2026-11-20"}, None),
        ("input_datetime", "set_datetime", {"date": "20.11.2026"}, "not_allowed_value"),
        ("time", "set_value", {"time": "07:30"}, None),
        ("remote", "send_command", {"command": ["power"], "num_repeats": 2}, None),
        ("remote", "send_command", {"command": "power"}, "not_allowed_value"),
        ("climate", "set_hvac_mode", {"hvac_mode": "heat"}, None),
        ("climate", "set_hvac_mode", {"hvac_mode": "boost"}, "not_allowed_value"),
    ],
)
def test_allowlist_check(
    domain: str, action: str, data: dict[str, Any], reason: str | None
) -> None:
    assert Allowlist.load().check(domain, action, data) == reason


def test_allowlist_limits_and_bundled_file() -> None:
    allowlist = Allowlist.load()
    assert ALLOWLIST_FILE.name == "control_allowlist.json"
    assert (
        allowlist.check("light", "turn_on", {f"x{i}": 1 for i in range(30)})
        == "not_allowed_field"
    )
    assert allowlist.release_required("scene") and allowlist.release_required("remote")
    assert not allowlist.release_required("light")
    assert (
        allowlist.rule("lock", "lock").safe
        and not allowlist.rule("lock", "unlock").safe
    )
    for dom in allowlist.domains.values():
        for rule in dom.actions.values():
            assert not set(rule.fields) & allowlist.forbidden


# ── snapshots ────────────────────────────────────────────────────────────


def test_snapshot_validation() -> None:
    good = make_snapshot(CLIENT_ID, "a" * 32, {"light.x": access()})
    assert validate_snapshot(copy.deepcopy(good)) == good
    cases = []
    bad = copy.deepcopy(good)
    bad["entities"]["light.y"] = access()  # hash no longer matches
    cases.append(bad)
    cases.append({**good, "version": 2})
    cases.append({**good, "extra": 1})
    cases.append({**good, "ha_user_id": "not-a-user"})
    cases.append(
        make_snapshot(
            CLIENT_ID,
            "a" * 32,
            {"light.x": {"local": "admin", "remote": "hidden", "quick": "off"}},
        )
    )
    cases.append(
        make_snapshot(CLIENT_ID, "a" * 32, {"light.x": {**access(), "extra": 1}})
    )
    cases.append(make_snapshot(CLIENT_ID, "a" * 32, {"Light X": access()}))
    kids = {
        "time_zone": None,
        "override_until": None,
        "entities": {"light.x": kids_rules()},
    }
    child = make_snapshot(CLIENT_ID, "a" * 32, {"light.x": access()}, kids=kids)
    assert validate_snapshot(child)
    cases.append(
        make_snapshot(
            CLIENT_ID, "a" * 32, {"light.x": access()}, kids=kids, control_path="direct"
        )
    )
    # A child device may have remote access (the way away).
    assert validate_snapshot(
        make_snapshot(
            CLIENT_ID,
            "a" * 32,
            {"light.x": access(remote="control")},
            kids=kids,
            remote_access=True,
        )
    )
    cases.append(
        make_snapshot(
            CLIENT_ID,
            "a" * 32,
            {"light.x": access()},
            kids={**kids, "entities": {"light.y": kids_rules()}},
        )
    )
    no_kids = make_snapshot(CLIENT_ID, "a" * 32, {"light.x": access()})
    no_kids["kids"] = kids
    no_kids["rules_hash"] = rules_hash(no_kids)
    cases.append(no_kids)
    for case in cases:
        with pytest.raises(SnapshotError):
            validate_snapshot(case)


async def test_apply_only_to_own_device_user(env: Env, hass: HomeAssistant) -> None:
    """A snapshot naming any other HA user is rejected: the hub cannot assign one."""
    other = MockUser(name="Family member").add_to_hass(hass)
    with pytest.raises(ServiceValidationError) as err:
        await env.apply(make_snapshot(CLIENT_ID, other.id, ENTITIES))
    assert err.value.translation_key == "rules_rejected"
    with pytest.raises(ServiceValidationError):
        await env.apply(make_snapshot(GUEST_ID, env.user.id, ENTITIES))
    assert env.manager.snapshots == {}
    result = await env.apply()
    assert result["applied"] == 1
    assert env.manager.hashes() == {
        CLIENT_ID: env.manager.snapshots[CLIENT_ID]["rules_hash"]
    }


async def test_apply_rules_needs_admin(env: Env, hass: HomeAssistant) -> None:
    from homeassistant.exceptions import Unauthorized

    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            "apply_rules",
            {"snapshots": []},
            blocking=True,
            context=Context(user_id=env.user.id),
        )


# ── read-only device users ────────────────────────────────────────


async def test_read_only_user_and_direct_calls(
    env: Env, hass: HomeAssistant, hass_ws_client: Any, mock_api: aioresponses
) -> None:
    """Checked: read-only at once, direct denied, control works; direct: system-users."""
    mock_api.passthrough_unmatched = True  # the WebSocket test server
    assert [g.id for g in env.user.groups] == [GROUP_ID_USER]
    await env.apply()
    await _settle(hass)
    assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]

    record = env.entry.runtime_data.device_users.records[CLIENT_ID]
    token = hass.auth.async_create_access_token(
        env.user.refresh_tokens[record["refresh_token_id"]]
    )
    ws = await hass_ws_client(hass, token)
    for domain, service, target in (
        ("input_boolean", "turn_on", {"entity_id": "input_boolean.a"}),
        ("homeassistant", "turn_on", {"entity_id": "input_boolean.a"}),
    ):
        await ws.send_json_auto_id(
            {
                "type": "call_service",
                "domain": domain,
                "service": service,
                "target": target,
            }
        )
        msg = await ws.receive_json()
        assert not msg["success"] and msg["error"]["message"] == "Unauthorized"
    assert hass.states.get("input_boolean.a").state == "off"

    await ws.send_json_auto_id(
        {
            "type": "call_service",
            "domain": DOMAIN,
            "service": "control",
            "service_data": {"entity_id": "input_boolean.a", "action": "turn_on"},
            "return_response": True,
        }
    )
    msg = await ws.receive_json()
    assert msg["success"], msg
    assert msg["result"]["response"]["route"] == "local"
    assert msg["result"]["response"]["results"][0]["ok"] is True
    assert hass.states.get("input_boolean.a").state == "on"

    await ws.send_json_auto_id(
        {
            "type": "call_service",
            "domain": DOMAIN,
            "service": "control",
            "service_data": {"entity_id": "input_boolean.b", "action": "turn_on"},
        }
    )
    msg = await ws.receive_json()
    assert not msg["success"]
    assert msg["error"]["translation_key"] == "blocked_area"
    assert hass.states.get("input_boolean.b").state == "off"

    await env.apply(control_path="direct")
    await _settle(hass)
    assert [g.id for g in env.user.groups] == [GROUP_ID_USER]


async def test_direct_path_stays_users(env: Env, hass: HomeAssistant) -> None:
    await env.apply(control_path="direct")
    assert [g.id for g in env.user.groups] == [GROUP_ID_USER]
    await env.apply(control_path="checked")
    assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]
    await env.apply(control_path="direct")
    assert [g.id for g in env.user.groups] == [GROUP_ID_USER]


async def test_watcher_restores_group_and_alarms(env: Env, hass: HomeAssistant) -> None:
    """Saving the device user in HA (system-users or admin) is undone at once."""
    alarms = async_capture_events(hass, f"{DOMAIN}_alarm")
    events = async_capture_events(hass, f"{DOMAIN}_event")
    await env.apply()
    for group in (GROUP_ID_USER, GROUP_ID_ADMIN):
        await hass.auth.async_update_user(env.user, group_ids=[group])
        await _settle(hass)
        assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]
        assert not env.user.is_admin
    assert [a.data["kind"] for a in alarms] == ["access_group_restored"] * 2
    assert alarms[0].data["client_id"] == CLIENT_ID
    assert "access_group_restored" in [e.data["kind"] for e in events]
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_RESTORED}_{CLIENT_ID}")
    queued = [e["type"] for e in env.manager.events._queue]
    assert queued.count("access_group_restored") == 2


async def test_rule_change_is_not_an_outside_group_change(
    env: Env, hass: HomeAssistant
) -> None:
    alarms = async_capture_events(hass, f"{DOMAIN}_alarm")
    await env.apply()
    await env.apply(control_path="direct")
    await _settle(hass)
    assert alarms == []


async def test_start_check_restores_what_changed_while_down(
    env: Env, hass: HomeAssistant
) -> None:
    """Emergency exit: changed while the integration was not watching, restored at start."""
    await env.apply()
    env.manager.async_shutdown()  # integration "not running"
    await hass.auth.async_update_user(env.user, group_ids=[GROUP_ID_USER])
    await _settle(hass)
    assert [g.id for g in env.user.groups] == [GROUP_ID_USER]
    await env.manager.async_sync_groups()  # what the start check does
    assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_RESTORED}_{CLIENT_ID}")


async def test_reconcile_restores_with_repair(env: Env, hass: HomeAssistant) -> None:
    """Emergency exit undone by the device user reconcile (after a restart) is reported too."""
    await env.apply()
    env.manager.async_shutdown()  # integration "not running": no watcher
    await hass.auth.async_update_user(env.user, group_ids=[GROUP_ID_USER])
    await _settle(hass)
    await env.entry.runtime_data.device_users.async_provision(
        _request(env.hub.client(CLIENT_ID), token_action="rotate")
    )
    await _settle(hass)
    user = await _user_of(hass, env.entry, CLIENT_ID)
    assert [g.id for g in user.groups] == [GROUP_ID_READ_ONLY]
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_RESTORED}_{CLIENT_ID}")


async def test_owner_or_credentials_never_touched(
    env: Env, hass: HomeAssistant
) -> None:
    owner = MockUser(is_owner=True, name="x").add_to_hass(hass)
    snap = make_snapshot(CLIENT_ID, owner.id, ENTITIES)
    env.manager.snapshots[CLIENT_ID] = snap
    await env.manager.async_sync_groups()
    assert [g.id for g in owner.groups] != [GROUP_ID_READ_ONLY]


def test_assert_safe_device_user_with_read_only() -> None:
    identity = DeviceIdentity.from_request(
        {"client_id": CLIENT_ID, "display_name": "iPhone", "remote_allowed": True}
    )
    user = MockUser(name="x")
    user.groups = [type("G", (), {"id": GROUP_ID_READ_ONLY})()]  # type: ignore[list-item]
    assert_safe_device_user(user, identity, GROUP_ID_READ_ONLY)  # type: ignore[arg-type]
    with pytest.raises(DeviceUserSafetyError):
        assert_safe_device_user(user, identity, GROUP_ID_USER)  # type: ignore[arg-type]
    with pytest.raises(DeviceUserSafetyError):
        assert_safe_device_user(user, identity, GROUP_ID_ADMIN)  # type: ignore[arg-type]


async def test_new_device_user_is_created_read_only(
    env: Env, hass: HomeAssistant
) -> None:
    """A device whose snapshot says checked gets a read-only user right away."""
    await env.apply()
    # Re-provisioning (rotation) keeps the group and passes the safety check.
    await env.entry.runtime_data.device_users.async_provision(
        _request(env.hub.client(CLIENT_ID), token_action="rotate")
    )
    await _settle(hass)
    user = await _user_of(hass, env.entry, CLIENT_ID)
    assert [g.id for g in user.groups] == [GROUP_ID_READ_ONLY]
    assert len(user.refresh_tokens) == 1


async def test_deprovision_forgets_snapshot(env: Env, hass: HomeAssistant) -> None:
    await env.apply()
    await env.entry.runtime_data.device_users.async_deprovision(CLIENT_ID)
    assert CLIENT_ID not in env.manager.snapshots


async def test_rules_pushed_during_token_delivery_are_taken(
    env: Env, hass: HomeAssistant
) -> None:
    """The hub pushes a new device's rules before it answers the
    token delivery (and before the app hears of the token). The device user is
    known by then, so the rules are taken and the first control call right
    after pairing is checked instead of meeting rules_missing."""
    coordinator = env.entry.runtime_data
    device_users = coordinator.device_users
    original = coordinator.post_client_token
    taken: list[int] = []

    async def deliver(client_id: str, ha_user_id: str, *args: Any) -> Any:
        snap = make_snapshot(client_id, ha_user_id, ENTITIES)
        taken.append(
            await env.manager.async_apply(
                [snap], [], False, device_users, _paths([snap])
            )
        )
        return await original(client_id, ha_user_id, *args)

    with patch.object(coordinator, "post_client_token", deliver):
        assert await device_users.async_provision(_request(env.hub.client(GUEST_ID)))
    await _settle(hass)
    assert taken == [1]
    guest = await _user_of(hass, env.entry, GUEST_ID)
    assert [g.id for g in guest.groups] == [GROUP_ID_READ_ONLY]
    response = await env.control(
        user_id=guest.id, entity_id="light.bed", action="turn_on"
    )
    assert response["results"][0]["executed"] is True


# ── halumar.control: callers ─────────────────────────────────────────────


async def test_callers(env: Env, hass: HomeAssistant) -> None:
    family = MockUser(name="Family member").add_to_hass(hass)
    await env.denied(
        "not_a_device", user_id=family.id, entity_id="light.bed", action="turn_on"
    )
    await env.denied("rules_missing", entity_id="light.bed", action="turn_on")
    await env.denied(
        "not_a_device", user_id="unknown", entity_id="light.bed", action="turn_on"
    )
    # Admins and system calls: allowlist only, own context.
    admin = MockUser(name="Admin").add_to_hass(hass)
    await hass.auth.async_update_user(admin, group_ids=[GROUP_ID_ADMIN])
    response = await env.control(
        user_id=admin.id, entity_id="light.not_listed", action="turn_on"
    )
    assert response["results"][0]["executed"] and response["rules_hash"] is None
    assert env.calls[-1].context.user_id == admin.id
    await env.control(user_id=None, entity_id="light.not_listed", action="turn_off")
    with pytest.raises(ServiceValidationError) as err:
        await env.control(
            user_id=None,
            _response=False,
            entity_id="light.bed",
            action="turn_on",
            data={"entity_id": "lock.front"},
        )
    assert err.value.translation_key == "not_allowed_field"


async def test_execution_as_system_with_parent_context(env: Env) -> None:
    await env.apply()
    ctx_calls = env.calls
    with patch(CONTROL, return_value="local"):
        call_context = Context(user_id=env.user.id)
        await env.hass.services.async_call(
            DOMAIN,
            "control",
            {
                "entity_id": "light.bed",
                "action": "turn_on",
                "data": {"brightness_pct": 30},
            },
            blocking=True,
            context=call_context,
        )
    executed = ctx_calls[-1]
    assert (
        executed.data["entity_id"] == ["light.bed"]
        or executed.data["entity_id"] == "light.bed"
    )
    assert executed.data["brightness_pct"] == 30
    assert executed.context.user_id is None
    assert executed.context.parent_id == call_context.id


# ── halumar.control: rules ───────────────────────────────────────────────


async def test_areas_routes_and_levels(env: Env) -> None:
    await env.apply()
    await env.denied("blocked_area", entity_id="light.not_listed", action="turn_on")
    await env.denied("unknown_entity", entity_id="light.nope", action="turn_on")
    await env.control(entity_id="light.ceiling", action="turn_on")
    # Without remote access nothing works from away (also unknown route).
    for route in ("external", "unknown"):
        await env.denied(
            "blocked_remote", route=route, entity_id="light.bed", action="turn_on"
        )
    # Local "view": only the safe direction.
    await env.denied("blocked_view_only", entity_id="cover.garage", action="open_cover")
    await env.denied("not_allowed_action", entity_id="cover.garage", action="unlock")


async def test_remote_levels_and_safe_direction(env: Env) -> None:
    await env.apply(remote_access=True)
    await env.control(route="external", entity_id="light.bed", action="turn_on")
    await env.control(route="external", entity_id="lock.front", action="lock")
    await env.denied(
        "blocked_remote",
        route="external",
        entity_id="lock.front",
        action="unlock",
        data={"code": "1234"},
    )
    await env.denied(
        "blocked_remote", route="external", entity_id="light.ceiling", action="turn_on"
    )
    await env.control(route="local", entity_id="lock.front", action="unlock")


async def test_quick_access(env: Env) -> None:
    await env.apply(remote_access=True)
    await env.control(entity_id="light.bed", action="toggle", origin="quick_access")
    await env.denied(
        "blocked_quick_access",
        entity_id="lock.front",
        action="lock",
        origin="quick_access",
    )
    await env.denied(
        "blocked_quick_access",
        entity_id="light.ceiling",
        action="toggle",
        origin="quick_access",
    )


async def test_release_and_groups(env: Env) -> None:
    await env.apply()
    await env.control(entity_id="scene.movie", action="turn_on")
    await env.denied(
        "scene_not_released", entity_id="scene.hidden_effects", action="turn_on"
    )
    await env.control(entity_id="light.group_ok", action="turn_on")
    await env.denied(
        "blocked_group_member", entity_id="light.group_bad", action="turn_on"
    )


async def test_batch_results_and_partial_denial(env: Env) -> None:
    await env.apply()
    before = len(env.calls)
    response = await env.control(
        entity_ids=["light.bed", "light.not_listed", "light.ceiling"], action="turn_off"
    )
    assert [
        (r["entity_id"], r["ok"], r["executed"], r["reason"])
        for r in response["results"]
    ] == [
        ("light.bed", True, True, None),
        ("light.not_listed", False, False, "blocked_area"),
        ("light.ceiling", True, True, None),
    ]
    assert len(env.calls) == before + 2
    with pytest.raises(ServiceValidationError) as err:
        await env.control(
            _response=False, entity_ids=["light.bed", "lock.front"], action="turn_off"
        )
    assert err.value.translation_key in ("control_partly_denied",)
    with pytest.raises(Exception):  # noqa: B017 - schema error
        await env.control(
            entity_id="light.bed", entity_ids=["light.bed"], action="turn_on"
        )
    with pytest.raises(Exception):  # noqa: B017 - schema error
        await env.control(
            entity_ids=[f"light.l{i}" for i in range(51)], action="turn_on"
        )


async def test_check_only_executes_nothing(env: Env) -> None:
    await env.apply()
    before = len(env.calls)
    response = await env.control(
        entity_ids=["light.bed", "light.not_listed"], action="toggle", check_only=True
    )
    assert [r["ok"] for r in response["results"]] == [True, False]
    assert not any(r["executed"] for r in response["results"])
    assert len(env.calls) == before


async def test_rate_limit(env: Env) -> None:
    await env.apply()
    with patch.object(Allowlist, "max_calls_per_minute", 2, create=True):
        env.manager.allowlist.max_calls_per_minute = 2
        await env.control(entity_id="light.bed", action="turn_on")
        await env.control(entity_id="light.bed", action="turn_on")
        await env.denied("rate_limited", entity_id="light.bed", action="turn_on")


async def test_action_failed(env: Env, hass: HomeAssistant) -> None:
    await env.apply()

    def boom(call: ServiceCall) -> None:
        from homeassistant.exceptions import HomeAssistantError

        raise HomeAssistantError("device offline")

    hass.services.async_register("light", "toggle", boom)
    await env.denied("action_failed", entity_id="light.bed", action="toggle")


# ── kids ─────────────────────────────────────────────────────────────────


PAUSE_ALL = {
    "enabled": True,
    "max_actions": 12,
    "max_entities": 4,
    "window_seconds": 60,
    "cooldown_seconds": 300,
}


def _kids_env_snapshot(env: Env, **rules: Any) -> dict[str, Any]:
    entities = {
        "light.bed": access(),
        "media_player.tv": access(),
        "light.ceiling": access(),
    }
    extra = {}
    if "pause_all" in rules:
        extra["rate_limit_all"] = rules.pop("pause_all")
    if "pause_released_until" in rules:
        extra["pause_released_until"] = rules.pop("pause_released_until")
    kids = {
        "time_zone": "Europe/Zurich",
        "override_until": rules.pop("override_until", None),
        "entities": {
            "light.bed": kids_rules(**rules),
            "media_player.tv": kids_rules(volume_max=40),
        },
        **extra,
    }
    return make_snapshot(CLIENT_ID, env.user.id, entities, kids=kids)


async def test_kids_local_rules(env: Env, hass: HomeAssistant) -> None:
    events = async_capture_events(hass, f"{DOMAIN}_event")
    await env.apply(_kids_env_snapshot(env, color_lock=True))
    await env.denied(
        "blocked_kids", entity_id="light.ceiling", action="turn_on"
    )  # no kids entry
    await env.denied(
        "blocked_kids_color",
        entity_id="light.bed",
        action="turn_on",
        data={"rgb_color": [255, 0, 0]},
    )
    await env.control(
        entity_id="light.bed", action="turn_on", data={"brightness_pct": 20}
    )
    response = await env.control(
        entity_id="media_player.tv", action="volume_set", data={"volume_level": 0.9}
    )
    assert response["results"][0]["adjusted"] == {"volume_level": 0.4}
    assert env.calls[-1].data["volume_level"] == 0.4
    response = await env.control(entity_id="media_player.tv", action="volume_up")
    assert (
        env.calls[-1].service == "volume_set"
        and env.calls[-1].data["volume_level"] == 0.4
    )
    hass.states.async_set("media_player.tv", "playing", {"volume_level": 0.4})
    await env.denied(
        "blocked_kids_volume", entity_id="media_player.tv", action="volume_up"
    )
    assert "kids_control" in [e.data["kind"] for e in events]
    await env.apply(_kids_env_snapshot(env, blocked=True))
    await env.denied("blocked_kids", entity_id="light.bed", action="turn_on")


async def test_kids_bedtime(env: Env, hass: HomeAssistant) -> None:
    events = async_capture_events(hass, f"{DOMAIN}_event")
    alarms = async_capture_events(hass, f"{DOMAIN}_alarm")
    await env.apply(_kids_env_snapshot(env, quiet=ALL_DAY))
    err = await env.denied(
        "blocked_kids_bedtime", entity_id="light.bed", action="turn_on"
    )
    assert err.translation_placeholders["until"]
    await env.control(entity_id="light.bed", action="turn_off")  # reducing
    hass.states.async_set("light.bed", "on")
    await env.control(entity_id="light.bed", action="toggle")  # toggle off = reducing
    assert "kids_bedtime_attempt" in [e.data["kind"] for e in events]
    assert alarms == []  # bedtime attempts are not alarms
    future = (dt_util.utcnow() + timedelta(hours=1)).isoformat()
    await env.apply(_kids_env_snapshot(env, quiet=ALL_DAY, override_until=future))
    await env.control(entity_id="light.bed", action="turn_on")


async def test_kids_away_without_grant(
    env: Env, hass: HomeAssistant, mock_api: aioresponses
) -> None:
    """A child device with remote access calls halumar.control
    itself from away (no hub grant): the remote levels and every kids rule
    apply, pauses are counted by the hub."""
    entities = {
        "light.bed": access(remote="control"),
        "media_player.tv": access(remote="control"),
        "lock.front": access(remote="view"),
        "light.ceiling": access(remote="hidden"),
    }
    kids = {
        "time_zone": "Europe/Zurich",
        "override_until": None,
        "entities": {
            "light.bed": kids_rules(rate=True),
            "media_player.tv": kids_rules(volume_max=40),
            "lock.front": kids_rules(blocked=True),
            "light.ceiling": kids_rules(),
        },
    }
    await env.apply(
        make_snapshot(CLIENT_ID, env.user.id, entities, kids=kids, remote_access=True)
    )
    bodies: list[dict[str, Any]] = []
    answers = [
        {"allowed": True, "reason": None, "until": None},
        {
            "allowed": False,
            "reason": "paused_all",
            "until": "2026-11-20T18:05:00+00:00",
        },
    ]

    def kids_check(url: URL, **kwargs: Any) -> CallbackResult:
        bodies.append(kwargs["json"])
        return CallbackResult(status=200, payload=answers[len(bodies) - 1])

    mock_api.post(KIDS_URL, callback=kids_check, repeat=True)
    response = await env.control(
        route="external", entity_id="light.bed", action="turn_on"
    )
    assert response["route"] == "external"
    assert response["results"][0]["executed"] is True
    # no grant from away: the hub counts this action
    assert bodies == [
        {"client_id": CLIENT_ID, "entity_id": "light.bed", "action": "turn_on"}
    ]
    await env.denied(
        "blocked_kids_paused_all",
        route="external",
        entity_id="light.bed",
        action="turn_on",
    )
    await env.denied(
        "blocked_remote", route="external", entity_id="light.ceiling", action="turn_on"
    )
    await env.denied(
        "blocked_kids", route="external", entity_id="lock.front", action="lock"
    )
    response = await env.control(
        route="external",
        entity_id="media_player.tv",
        action="volume_set",
        data={"volume_level": 0.9},
    )
    assert response["results"][0]["adjusted"] == {"volume_level": 0.4}
    entities_bed = {**entities, "light.ceiling": access(remote="control")}
    await env.apply(
        make_snapshot(
            CLIENT_ID,
            env.user.id,
            entities_bed,
            kids={
                **kids,
                "entities": {
                    **kids["entities"],
                    "light.ceiling": kids_rules(quiet=ALL_DAY),
                },
            },
            remote_access=True,
        )
    )
    await env.denied(
        "blocked_kids_bedtime",
        route="external",
        entity_id="light.ceiling",
        action="turn_on",
    )


async def test_childs_watch_is_checked_like_its_phone(
    env: Env, hass: HomeAssistant, mock_api: aioresponses
) -> None:
    """A child's Apple Watch has no hub socket and
    always calls halumar.control itself with its read-only user. Its snapshot
    carries its phone's kids rules and releases: bedtime blocks it, the
    phone's release frees it, and every action asks kids-check with the
    watch's own client_id (the hub counts it on the phone, one counter)."""
    entities = {
        "light.bed": access(remote="control"),
        "light.ceiling": access(remote="control"),
    }
    kids = {
        "time_zone": "Europe/Zurich",
        "override_until": None,
        "entities": {
            "light.bed": kids_rules(rate=True),
            "light.ceiling": kids_rules(quiet=ALL_DAY),
        },
    }
    await env.apply(
        make_snapshot(CLIENT_ID, env.user.id, entities, kids=kids, remote_access=True)
    )
    bodies: list[dict[str, Any]] = []

    def kids_check(url: URL, **kwargs: Any) -> CallbackResult:
        bodies.append(kwargs["json"])
        if len(bodies) == 1:
            return CallbackResult(
                status=200, payload={"allowed": True, "reason": None, "until": None}
            )
        # The phone used up the shared limit in between.
        return CallbackResult(
            status=200,
            payload={
                "allowed": False,
                "reason": "rate_limited",
                "until": "2026-11-20T18:05:00+00:00",
            },
        )

    mock_api.post(KIDS_URL, callback=kids_check, repeat=True)
    for route in ("local", "external"):
        await env.denied(
            "blocked_kids_bedtime",
            route=route,
            entity_id="light.ceiling",
            action="turn_on",
        )
    await env.control(entity_id="light.bed", action="turn_on")
    await env.denied("blocked_kids_cooldown", entity_id="light.bed", action="turn_on")
    assert [b["client_id"] for b in bodies] == [CLIENT_ID, CLIENT_ID]
    # The phone's bedtime release is in the watch's snapshot too.
    future = (dt_util.utcnow() + timedelta(hours=1)).isoformat()
    await env.apply(
        make_snapshot(
            CLIENT_ID,
            env.user.id,
            entities,
            kids={**kids, "bedtime_released_until": future},
            remote_access=True,
        )
    )
    await env.control(entity_id="light.ceiling", action="turn_on")


def test_quiet_until_crosses_midnight() -> None:
    windows = [{"days": ["fri"], "start": "21:00", "end": "07:00"}]
    friday_late = dt_util.parse_datetime("2026-11-20T22:00:00+01:00")
    assert quiet_until(windows, friday_late, "Europe/Zurich") == dt_util.parse_datetime(
        "2026-11-21T06:00:00+00:00"
    )
    assert (
        quiet_until(
            windows,
            dt_util.parse_datetime("2026-11-20T12:00:00+01:00"),
            "Europe/Zurich",
        )
        is None
    )
    assert quiet_until([], friday_late, None) is None


async def test_kids_hub_check(
    env: Env, hass: HomeAssistant, mock_api: aioresponses
) -> None:
    await env.apply(_kids_env_snapshot(env, rate=True))
    bodies: list[dict[str, Any]] = []
    answers = [
        {"allowed": True, "reason": None, "until": None},
        {"allowed": False, "reason": "cooldown", "until": "2026-11-20T18:05:00+00:00"},
    ]

    def kids(url: URL, **kwargs: Any) -> CallbackResult:
        bodies.append(kwargs["json"])
        return CallbackResult(status=200, payload=answers[len(bodies) - 1])

    mock_api.post(KIDS_URL, callback=kids, repeat=True)
    await env.control(entity_id="light.bed", action="turn_on", grant="g" * 20)
    assert bodies[0] == {
        "client_id": CLIENT_ID,
        "entity_id": "light.bed",
        "action": "turn_on",
        "grant": "g" * 20,
    }
    err = await env.denied(
        "blocked_kids_cooldown", entity_id="light.bed", action="turn_on"
    )
    assert err.translation_placeholders["until"]
    # check_only never asks (it would count).
    await env.control(entity_id="light.bed", action="turn_on", check_only=True)
    assert len(bodies) == 2


async def test_kids_pause_everything_asks_the_hub_for_every_entity(
    env: Env, hass: HomeAssistant, mock_api: aioresponses
) -> None:
    """With rate_limit_all on, every action asks the hub (also entities
    without rate_limit); paused_all becomes blocked_kids_paused_all."""
    events = async_capture_events(hass, f"{DOMAIN}_event")
    await env.apply(_kids_env_snapshot(env, pause_all=PAUSE_ALL))
    bodies: list[dict[str, Any]] = []
    answers = [
        {"allowed": True, "reason": None, "until": None},
        {
            "allowed": False,
            "reason": "paused_all",
            "until": "2026-11-20T18:05:00+00:00",
        },
    ]

    def kids(url: URL, **kwargs: Any) -> CallbackResult:
        bodies.append(kwargs["json"])
        return CallbackResult(status=200, payload=answers[min(len(bodies), 2) - 1])

    mock_api.post(KIDS_URL, callback=kids, repeat=True)
    await env.control(entity_id="light.bed", action="turn_on")
    assert bodies[0]["entity_id"] == "light.bed"
    err = await env.denied(
        "blocked_kids_paused_all", entity_id="media_player.tv", action="media_play"
    )
    assert err.translation_placeholders["until"]
    assert bodies[1]["entity_id"] == "media_player.tv"
    await _settle(hass)
    assert events[-1].data["kind"] == "kids_pause"
    assert events[-1].data["reason"] == "blocked_kids_paused_all"


async def test_kids_pause_everything_off_or_released_does_not_ask(
    env: Env, hass: HomeAssistant, mock_api: aioresponses
) -> None:
    mock_api.post(KIDS_URL, status=500, repeat=True)  # asking would reject
    await env.apply(_kids_env_snapshot(env, pause_all={**PAUSE_ALL, "enabled": False}))
    await env.control(entity_id="light.bed", action="turn_on")
    await env.apply(
        _kids_env_snapshot(
            env, pause_all=PAUSE_ALL, pause_released_until="2999-01-01T00:00:00+00:00"
        )
    )
    await env.control(entity_id="light.bed", action="turn_on")


def test_snapshot_rate_limit_all_shape() -> None:
    kids = {
        "time_zone": None,
        "override_until": None,
        "entities": {"light.x": kids_rules()},
        "rate_limit_all": PAUSE_ALL,
    }
    good = make_snapshot(CLIENT_ID, "a" * 32, {"light.x": access()}, kids=kids)
    assert validate_snapshot(copy.deepcopy(good)) == good
    for bad_value in (
        {**PAUSE_ALL, "extra": 1},
        {k: v for k, v in PAUSE_ALL.items() if k != "max_entities"},
        {**PAUSE_ALL, "max_actions": 0},
        {**PAUSE_ALL, "enabled": 1},
        {**PAUSE_ALL, "max_entities": True},
    ):
        bad = make_snapshot(
            CLIENT_ID,
            "a" * 32,
            {"light.x": access()},
            kids={**kids, "rate_limit_all": bad_value},
        )
        with pytest.raises(SnapshotError):
            validate_snapshot(bad)


async def test_kids_hub_unreachable(
    env: Env, hass: HomeAssistant, mock_api: aioresponses
) -> None:
    await env.apply(_kids_env_snapshot(env, rate=True))
    mock_api.post(KIDS_URL, status=500, repeat=True)
    before = len(env.calls)
    await env.denied("kids_hub_unreachable", entity_id="light.bed", action="turn_on")
    await hass.config_entries.async_unload(env.entry.entry_id)
    await env.denied("kids_hub_unreachable", entity_id="light.bed", action="turn_on")
    assert len(env.calls) == before


# ── events ────────────────────────────────────────────────────────


async def test_events_fired_and_sent(
    env: Env, hass: HomeAssistant, mock_api: aioresponses
) -> None:
    events = async_capture_events(hass, f"{DOMAIN}_event")
    alarms = async_capture_events(hass, f"{DOMAIN}_alarm")
    await env.apply()
    await env.control(entity_id="light.bed", action="turn_on", data={"brightness": 10})
    await env.denied("blocked_area", entity_id="light.not_listed", action="turn_on")
    kinds = [e.data["kind"] for e in events]
    assert (
        "control" in kinds and "denied" in kinds and "integration_started" not in kinds
    )  # started before capture
    control = next(e.data for e in events if e.data["kind"] == "control")
    assert control["client_id"] == CLIENT_ID and control["entity_id"] == "light.bed"
    assert (
        control["route"] == "local"
        and "data" not in control
        and "brightness" not in str(control)
    )
    assert [a.data["kind"] for a in alarms] == ["denied"]
    assert alarms[0].data["reason"] == "blocked_area"
    await env.manager.events.async_flush()
    sent = [
        kw["json"]
        for (m, u), calls in mock_api.requests.items()
        if str(u) == EVENTS_URL
        for _, kw in [(c.args, c.kwargs) for c in calls]
    ]
    types = [e["type"] for body in sent for e in body["events"]]
    assert {"integration_started", "rules_applied", "control", "denied"} <= set(types)
    for body in sent:
        for event in body["events"]:
            assert set(event) <= {
                "id",
                "type",
                "at",
                "client_id",
                "ha_user_id",
                "entity_id",
                "action",
                "route",
                "origin",
                "reason",
                "count",
                "since",
                "ha_group",
                "rules_hash",
                "reliability",
                "version",
            }
    assert env.manager.events.queued == 0


async def test_event_queue_bounded_when_hub_down(
    env: Env, hass: HomeAssistant, mock_api: aioresponses
) -> None:
    reporter = env.manager.events
    await reporter.async_flush()
    mock_api.clear()
    mock_api.post(EVENTS_URL, status=503, repeat=True)
    for _ in range(MAX_QUEUE + 25):
        reporter.record(
            "control",
            client_id=CLIENT_ID,
            entity_id="light.bed",
            action="turn_on",
            route="local",
        )
    await reporter.async_flush()
    assert reporter.queued == MAX_QUEUE
    assert reporter.dropped == 25


async def test_hub_events_are_fired(env: Env, hass: HomeAssistant) -> None:
    events = async_capture_events(hass, f"{DOMAIN}_event")
    good = {
        "kind": "guest_paired",
        "category": "guests",
        "at": "2026-11-20T18:00:00+00:00",
        "client_id": GUEST_ID,
        "device_name": "Gast 1",
        "pin": "1234",
    }
    assert async_handle_push(hass, None, {"type": "ha_event", "data": good})
    assert async_handle_push(
        hass, None, {"type": "ha_event", "data": {**good, "category": "kids"}}
    )
    assert async_handle_push(
        hass,
        None,
        {"type": "ha_event", "data": {**good, "kind": "denied", "category": "alarms"}},
    )
    assert not async_handle_push(hass, None, {"type": "client_status"})
    assert len(events) == 1
    assert events[0].data["kind"] == "guest_paired" and "pin" not in events[0].data
    assert "test" not in events[0].data


async def test_hub_test_event_of_any_kind_is_fired(
    env: Env, hass: HomeAssistant
) -> None:
    """'Send test' of a notification rule: any kind, fired with test true."""
    events = async_capture_events(hass, f"{DOMAIN}_event")
    alarms = async_capture_events(hass, f"{DOMAIN}_alarm")
    test = {
        "kind": "denied",
        "category": "alarms",
        "at": "2026-11-20T18:05:00+00:00",
        "client_id": None,
        "count": 1,
        "test": True,
    }
    assert async_handle_push(hass, None, {"type": "ha_event", "data": test})
    # without test: true a kind of the integration stays the integration's
    assert async_handle_push(
        hass, None, {"type": "ha_event", "data": {**test, "test": "yes"}}
    )
    assert async_handle_push(
        hass, None, {"type": "ha_event", "data": {**test, "category": "kids"}}
    )
    await hass.async_block_till_done()
    assert len(events) == 1 and len(alarms) == 1
    assert events[0].data == {
        "kind": "denied",
        "category": "alarms",
        "at": "2026-11-20T18:05:00+00:00",
        "count": 1,
        "test": True,
    }
    assert alarms[0].data["test"] is True and alarms[0].data["kind"] == "denied"


async def test_enforcement_rules_push_and_complete(
    env: Env, hass: HomeAssistant
) -> None:
    snap = make_snapshot(CLIENT_ID, env.user.id, ENTITIES)
    device_users = env.entry.runtime_data.device_users
    assert async_handle_push(
        hass,
        device_users,
        {
            "type": "enforcement_rules",
            "data": {
                "snapshots": [snap],
                "control_paths": _paths([snap]),
                "removed": [],
                "complete": False,
            },
        },
    )
    await _settle(hass)
    assert CLIENT_ID in env.manager.snapshots
    # the status no longer lists the device: a complete set without it forgets
    env.entry.runtime_data.client_by_id.pop(CLIENT_ID)
    assert async_handle_push(
        hass,
        device_users,
        {
            "type": "enforcement_rules",
            "data": {
                "snapshots": [],
                "control_paths": {},
                "removed": [],
                "complete": True,
            },
        },
    )
    await _settle(hass)
    assert env.manager.snapshots == {}


async def test_poll_backstop_fetches_rules(
    env: Env, hass: HomeAssistant, mock_api: aioresponses
) -> None:
    snap = make_snapshot(CLIENT_ID, env.user.id, ENTITIES)
    fetched: list[int] = []

    def rules(url: URL, **kwargs: Any) -> CallbackResult:
        fetched.append(1)
        return CallbackResult(
            status=200, payload={"snapshots": [snap], "control_paths": _paths([snap])}
        )

    mock_api.get(RULES_URL, callback=rules, repeat=True)
    env.hub.snapshot["enforcement"] = {"rules_hashes": {CLIENT_ID: snap["rules_hash"]}}
    await env.entry.runtime_data.async_refresh()
    await _settle(hass)
    assert env.manager.hashes() == {CLIENT_ID: snap["rules_hash"]}
    await env.entry.runtime_data.async_refresh()
    await _settle(hass)
    assert len(fetched) == 1  # in line: no second fetch


# ── watchers ────────────────────────────────────────────────


async def test_direct_attempts_best_effort(env: Env, hass: HomeAssistant) -> None:
    from homeassistant.exceptions import Unauthorized

    await env.apply()
    alarms = async_capture_events(hass, f"{DOMAIN}_alarm")
    logger = logging.getLogger(WS_LOGGER)
    other = MockUser(name="Somebody").add_to_hass(hass)
    for _ in range(DIRECT_THRESHOLD):
        err = Unauthorized(
            context=Context(user_id=env.user.id), entity_id="input_boolean.a"
        )
        logger.error(
            "[1] Error during service call to %s.%s: %s",
            "input_boolean",
            "turn_on",
            err,
        )
        logger.error(
            "[1] Error during service call to %s.%s: %s",
            "light",
            "turn_on",
            Unauthorized(user_id=other.id),
        )
        logger.error(
            "[1] Error during service call to %s.%s: %s", "light", "turn_on", "plain"
        )
    await _settle(hass)
    assert [a.data["kind"] for a in alarms] == ["direct_access_attempt"]
    assert alarms[0].data["count"] == DIRECT_THRESHOLD
    assert alarms[0].data["entity_id"] == "input_boolean.a"
    queued = [
        e for e in env.manager.events._queue if e["type"] == "direct_access_attempt"
    ]
    assert queued[0]["reliability"] == "best_effort"


async def test_real_direct_call_is_seen(
    env: Env, hass: HomeAssistant, hass_ws_client: Any, mock_api: aioresponses
) -> None:
    """End to end with HA's own WebSocket API: three direct calls raise one alarm."""
    mock_api.passthrough_unmatched = True
    await env.apply()
    alarms = async_capture_events(hass, f"{DOMAIN}_alarm")
    record = env.entry.runtime_data.device_users.records[CLIENT_ID]
    ws = await hass_ws_client(
        hass,
        hass.auth.async_create_access_token(
            env.user.refresh_tokens[record["refresh_token_id"]]
        ),
    )
    for _ in range(DIRECT_THRESHOLD):
        await ws.send_json_auto_id(
            {
                "type": "call_service",
                "domain": "input_boolean",
                "service": "toggle",
                "target": {"entity_id": "input_boolean.a"},
            }
        )
        assert not (await ws.receive_json())["success"]
    await _settle(hass)
    assert [a.data["kind"] for a in alarms] == ["direct_access_attempt"]


async def test_one_alarm_per_burst_of_direct_and_untargeted_calls(
    env: Env, hass: HomeAssistant, hass_ws_client: Any, mock_api: aioresponses
) -> None:
    """The live sequence over Home Assistant's WebSocket API: an action
    without a target (scene.apply) and three refused entity calls with a
    separate ``target`` give one alarm; the targeted calls are never
    reported as actions without an entity."""
    mock_api.passthrough_unmatched = True
    await env.apply()
    assert await async_setup_component(hass, "scene", {})
    await hass.async_block_till_done()
    alarms = async_capture_events(hass, f"{DOMAIN}_alarm")
    record = env.entry.runtime_data.device_users.records[CLIENT_ID]
    ws = await hass_ws_client(
        hass,
        hass.auth.async_create_access_token(
            env.user.refresh_tokens[record["refresh_token_id"]]
        ),
    )
    issue_id = f"{ISSUE_SERVICE_CALLED}_{CLIENT_ID}"

    async def call(msg: dict[str, Any]) -> dict[str, Any]:
        await ws.send_json_auto_id({"type": "call_service", **msg})
        return await ws.receive_json()

    # Refused entity calls with a separate target: never reported as such.
    for _ in range(DIRECT_THRESHOLD):
        answer = await call(
            {
                "domain": "input_boolean",
                "service": "toggle",
                "target": {"entity_id": "input_boolean.a"},
            }
        )
        assert not answer["success"]
    await _settle(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert [a.data["kind"] for a in alarms] == ["direct_access_attempt"]

    # An action without a target in the same window: repair, no second alarm.
    await call(
        {
            "domain": "scene",
            "service": "apply",
            "service_data": {"entities": {"input_boolean.b": "on"}},
        }
    )
    for _ in range(DIRECT_THRESHOLD):
        await call(
            {
                "domain": "input_boolean",
                "service": "toggle",
                "target": {"entity_id": "input_boolean.a"},
            }
        )
    await _settle(hass)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_placeholders["service"] == "scene.apply"
    assert [a.data["kind"] for a in alarms] == ["direct_access_attempt"]


async def test_untargeted_call_first_throttles_the_direct_attempts(
    env: Env, hass: HomeAssistant, hass_ws_client: Any, mock_api: aioresponses
) -> None:
    """The action without a target comes first, then the direct attempts."""
    mock_api.passthrough_unmatched = True
    await env.apply()
    assert await async_setup_component(hass, "scene", {})
    alarms = async_capture_events(hass, f"{DOMAIN}_alarm")
    record = env.entry.runtime_data.device_users.records[CLIENT_ID]
    ws = await hass_ws_client(
        hass,
        hass.auth.async_create_access_token(
            env.user.refresh_tokens[record["refresh_token_id"]]
        ),
    )
    msgs: list[dict[str, Any]] = [
        {
            "domain": "scene",
            "service": "apply",
            "service_data": {"entities": {"input_boolean.b": "on"}},
        }
    ] + [
        {
            "domain": "input_boolean",
            "service": "toggle",
            "target": {"entity_id": "input_boolean.a"},
        }
    ] * DIRECT_THRESHOLD
    for msg in msgs:
        await ws.send_json_auto_id({"type": "call_service", **msg})
        await ws.receive_json()
    await _settle(hass)
    assert [a.data["kind"] for a in alarms] == ["direct_access_attempt"]


async def test_outage_detected_at_start(
    hass: HomeAssistant, hass_storage: dict[str, Any], existing_owner: MockUser
) -> None:
    last = dt_util.utcnow() - timedelta(hours=5)
    snap = make_snapshot(CLIENT_ID, "a" * 32, {"light.x": access()})
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "snapshots": {CLIENT_ID: snap},
            "alive_at": last.isoformat(),
            "stopped_at": None,
        },
    }
    with patch.object(
        EnforcementManager,
        "_ha_last_seen",
        return_value=dt_util.utcnow() - timedelta(minutes=5),
    ):
        assert await async_setup_component(hass, DOMAIN, {})
    manager: EnforcementManager = hass.data[DATA_ENFORCEMENT]
    assert manager.last_outage is not None
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_OUTAGE)
    types = [e["type"] for e in manager.events._queue]
    assert types[:2] == ["integration_outage", "integration_started"]


async def test_no_outage_without_gap(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "snapshots": {},
            "alive_at": (dt_util.utcnow() - timedelta(minutes=12)).isoformat(),
        },
    }
    with patch.object(
        EnforcementManager,
        "_ha_last_seen",
        return_value=dt_util.utcnow() - timedelta(minutes=2),
    ):
        assert await async_setup_component(hass, DOMAIN, {})
    assert hass.data[DATA_ENFORCEMENT].last_outage is None


async def test_snapshots_survive_restart_and_bad_ones_are_dropped(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    good = make_snapshot(CLIENT_ID, "a" * 32, {"light.x": access()})
    bad = {
        **make_snapshot(GUEST_ID, "b" * 32, {"light.x": access()}),
        "rules_hash": "0" * 64,
    }
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "snapshots": {CLIENT_ID: good, GUEST_ID: bad},
        },
    }
    assert await async_setup_component(hass, DOMAIN, {})
    manager: EnforcementManager = hass.data[DATA_ENFORCEMENT]
    assert list(manager.snapshots) == [CLIENT_ID]
    assert manager.desired_group_id(CLIENT_ID) == GROUP_ID_READ_ONLY


async def test_control_works_without_loaded_entry(
    env: Env, hass: HomeAssistant
) -> None:
    """The hub is gone and the entry unloaded: decisions come from the stored snapshot."""
    await env.apply()
    await hass.config_entries.async_unload(env.entry.entry_id)
    await env.control(entity_id="light.bed", action="turn_on")
    await env.denied("blocked_area", entity_id="light.not_listed", action="turn_on")


async def test_diagnostics_contain_enforcement(env: Env, hass: HomeAssistant) -> None:
    from custom_components.halumar.diagnostics import async_get_config_entry_diagnostics

    await env.apply()
    diag = await async_get_config_entry_diagnostics(hass, env.entry)
    enforcement = diag["enforcement"]
    assert enforcement["devices"][CLIENT_ID]["entities"] == len(ENTITIES)
    assert "light.bed" not in str(enforcement)


# ── edges ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("domain", "action", "data", "reason"),
    [
        ("automation", "trigger", {"skip_condition": "yes"}, "not_allowed_value"),
        ("datetime", "set_value", {"datetime": "2026-11-20T18:00:00+01:00"}, None),
        ("datetime", "set_value", {"datetime": "tomorrow"}, "not_allowed_value"),
        ("time", "set_value", {"time": "x" * 80}, "not_allowed_value"),
    ],
)
def test_allowlist_edges(
    domain: str, action: str, data: dict[str, Any], reason: str | None
) -> None:
    assert Allowlist.load().check(domain, action, data) == reason


def test_allowlist_unknown_field_type() -> None:
    from custom_components.halumar.allowlist import _value_ok

    assert _value_ok({"type": "blob"}, "x") is False


def test_connection_route() -> None:
    from custom_components.halumar.control import connection_route

    hass = SimpleNamespace()
    request = SimpleNamespace(remote="192.168.1.20")
    with patch(
        "custom_components.halumar.control.is_cloud_connection", return_value=False
    ):
        with patch("custom_components.halumar.control.current_request") as cur:
            cur.get.return_value = request
            assert connection_route(hass) == "local"  # type: ignore[arg-type]
            request.remote = "8.8.8.8"
            assert connection_route(hass) == "external"  # type: ignore[arg-type]
            request.remote = "not-an-ip"
            assert connection_route(hass) == "external"  # type: ignore[arg-type]
            request.remote = None
            assert connection_route(hass) == "unknown"  # type: ignore[arg-type]
            cur.get.return_value = None
            assert connection_route(hass) == "unknown"  # type: ignore[arg-type]
    with patch(
        "custom_components.halumar.control.is_cloud_connection", return_value=True
    ):
        assert connection_route(hass) == "external"  # type: ignore[arg-type]


def _contract_vectors() -> list[dict[str, Any]]:
    import json
    import os
    from pathlib import Path

    base = os.environ.get("CONTRACTS_DIR")
    folder = Path(base) / "fixtures/network/home-networks" if base else None
    if folder is None or not folder.is_dir():
        pytest.skip("CONTRACTS_DIR with fixtures/network/home-networks not set")
    return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))]


def test_home_networks_contract_vectors() -> None:
    """The shared vectors decide alike (contains, IPv4-mapped, brackets)."""
    from custom_components.halumar.snapshot import in_home_networks

    checked = 0
    for vector in _contract_vectors():
        nets = [n["cidr"] for n in vector["expect"].get("networks", [])]
        for case in vector.get("hosts", []):
            assert in_home_networks(nets, case["address"]) is case["home"], (
                vector["name"],
                case,
            )
            checked += 1
    assert checked >= 10


def test_connection_route_home_networks() -> None:
    """A caller from a further home network is local, Cloud stays external."""
    from custom_components.halumar.control import connection_route

    hass = SimpleNamespace()
    nets = ["100.64.0.0/10", "fd7a:115c:a1e0::/48"]
    request = SimpleNamespace(remote="100.101.102.103")
    with patch(
        "custom_components.halumar.control.is_cloud_connection", return_value=False
    ):
        with patch("custom_components.halumar.control.current_request") as cur:
            cur.get.return_value = request
            assert connection_route(hass) == "external"  # type: ignore[arg-type]
            assert connection_route(hass, []) == "external"  # type: ignore[arg-type]
            assert connection_route(hass, nets) == "local"  # type: ignore[arg-type]
            request.remote = "fd7a:115c:a1e0:1::2"
            assert connection_route(hass, nets) == "local"  # type: ignore[arg-type]
            request.remote = "100.128.0.1"
            assert connection_route(hass, nets) == "external"  # type: ignore[arg-type]
            request.remote = "8.8.8.8"
            assert connection_route(hass, nets) == "external"  # type: ignore[arg-type]
    with patch(
        "custom_components.halumar.control.is_cloud_connection", return_value=True
    ):
        assert connection_route(hass, nets) == "external"  # type: ignore[arg-type]


async def test_home_networks_make_a_vpn_caller_local(env: Env) -> None:
    """Further home networks end to end: the snapshot's home networks decide the route."""

    async def call(remote: str) -> Any:
        request = SimpleNamespace(remote=remote)
        with (
            patch(
                "custom_components.halumar.control.is_cloud_connection",
                return_value=False,
            ),
            patch("custom_components.halumar.control.current_request") as cur,
        ):
            cur.get.return_value = request
            return await env.hass.services.async_call(
                DOMAIN,
                "control",
                {"entity_id": "light.ceiling", "action": "turn_on"},
                blocking=True,
                context=Context(user_id=env.user.id),
                return_response=True,
            )

    # Without the list a Tailscale address is away: no remote access, refused.
    await env.apply()
    with pytest.raises(ServiceValidationError) as err:
        await call("100.101.102.103")
    assert err.value.translation_key == "blocked_remote"
    # With 100.64.0.0/10 the same caller is at home and may switch.
    await env.apply(home_networks=["100.64.0.0/10"])
    answer = await call("100.101.102.103")
    assert answer["route"] == "local"
    assert answer["results"][0]["executed"] is True
    # A public address stays away.
    with pytest.raises(ServiceValidationError) as err:
        await call("203.0.113.9")
    assert err.value.translation_key == "blocked_remote"


@pytest.mark.parametrize(
    "networks",
    [
        ["8.8.8.0/24"],
        ["100.101.0.0/10"],
        ["100.64.0.0/10", "nope"],
        "100.64.0.0/10",
        [f"10.{i}.0.0/16" for i in range(11)],
        [None],
    ],
)
def test_snapshot_home_networks_fail_closed(networks: Any) -> None:
    """A list that is not canonical and private invalidates the snapshot."""
    snap = make_snapshot(CLIENT_ID, "a" * 32, {}, home_networks=["100.64.0.0/10"])
    assert validate_snapshot(copy.deepcopy(snap))["home_networks"] == ["100.64.0.0/10"]
    snap["home_networks"] = networks
    snap["rules_hash"] = rules_hash(snap)
    with pytest.raises(SnapshotError):
        validate_snapshot(snap)
    missing = make_snapshot(CLIENT_ID, "a" * 32, {})
    del missing["home_networks"]
    missing["rules_hash"] = rules_hash(missing)
    with pytest.raises(SnapshotError):
        validate_snapshot(missing)


async def test_control_schema_edges(env: Env) -> None:
    await env.apply()
    with pytest.raises(Exception):  # noqa: B017 - schema error
        await env.control(entity_id="light.bed", action="Turn-On")
    await env.denied("not_allowed_field", action="turn_on")
    env.manager.allowlist.max_entities = 1
    await env.denied(
        "not_allowed_field", entity_ids=["light.bed", "light.ceiling"], action="turn_on"
    )


async def test_group_with_foreign_domain_member(env: Env, hass: HomeAssistant) -> None:
    await env.apply()
    hass.states.async_set(
        "light.group_mixed",
        "off",
        {"entity_id": ["light.bed", "switch.x", 5, "light.group_mixed"]},
    )
    snap = make_snapshot(
        CLIENT_ID, env.user.id, {**ENTITIES, "light.group_mixed": access()}
    )
    await env.apply(snap)
    await env.denied(
        "blocked_group_member", entity_id="light.group_mixed", action="turn_on"
    )


async def test_nested_group_checks_every_member(env: Env, hass: HomeAssistant) -> None:
    """A group among the members switches its members as well; they
    are checked like the first level (unlisted member, own reason kept)."""
    hass.states.async_set(
        "light.outer", "off", {"entity_id": ["light.ceiling", "light.inner"]}
    )
    hass.states.async_set(
        "light.inner", "off", {"entity_id": ["light.bed", "light.not_listed"]}
    )
    entities = {**ENTITIES, "light.outer": access(), "light.inner": access()}
    await env.apply(make_snapshot(CLIENT_ID, env.user.id, entities))
    before = len(env.calls)
    await env.denied("blocked_group_member", entity_id="light.outer", action="turn_on")
    assert len(env.calls) == before

    hass.states.async_set(
        "light.inner", "off", {"entity_id": ["light.bed", "light.outer"]}
    )
    await env.control(entity_id="light.outer", action="turn_on")
    assert env.calls[-1].data["entity_id"] == "light.outer"

    # Remote: the member hidden away from home keeps its own reason.
    entities["light.outer"] = access(remote="control")
    entities["light.inner"] = access(remote="control")
    await env.apply(make_snapshot(CLIENT_ID, env.user.id, entities))
    await env.denied(
        "blocked_remote", route="external", entity_id="light.outer", action="turn_on"
    )


async def test_group_depth_is_bounded(env: Env, hass: HomeAssistant) -> None:
    depth = env.manager.allowlist.target_max_depth
    names = [f"light.g{i}" for i in range(depth + 1)]
    for outer, inner in zip(names, names[1:], strict=False):
        hass.states.async_set(outer, "off", {"entity_id": [inner]})
    hass.states.async_set(names[-1], "off", {"entity_id": ["light.bed"]})
    entities = {**ENTITIES, **{n: access() for n in names}}
    await env.apply(make_snapshot(CLIENT_ID, env.user.id, entities))
    await env.denied("blocked_group_member", entity_id=names[0], action="turn_on")
    await env.control(entity_id=names[1], action="turn_on")


async def test_group_members_follow_their_kids_rules(
    env: Env, hass: HomeAssistant
) -> None:
    """Bedtime, colour lock and volume cap of a member (also nested)
    hold for the group; a reducing action stays allowed at bedtime."""
    hass.states.async_set(
        "light.kids_group", "off", {"entity_id": ["light.ceiling", "light.inner"]}
    )
    hass.states.async_set("light.inner", "off", {"entity_id": ["light.bed"]})
    hass.states.async_set(
        "media_player.speakers", "playing", {"entity_id": ["media_player.tv"]}
    )
    entities = {
        "light.kids_group": access(),
        "light.inner": access(),
        "light.ceiling": access(),
        "light.bed": access(),
        "media_player.speakers": access(),
        "media_player.tv": access(),
    }

    def snapshot(**bed: Any) -> dict[str, Any]:
        kids = {
            "time_zone": "Europe/Zurich",
            "override_until": None,
            "entities": {
                "light.kids_group": kids_rules(),
                "light.inner": kids_rules(),
                "light.ceiling": kids_rules(),
                "light.bed": kids_rules(**bed),
                "media_player.speakers": kids_rules(),
                "media_player.tv": kids_rules(volume_max=40),
            },
        }
        return make_snapshot(CLIENT_ID, env.user.id, entities, kids=kids)

    await env.apply(snapshot(quiet=ALL_DAY))
    err = await env.denied(
        "blocked_kids_bedtime", entity_id="light.kids_group", action="turn_on"
    )
    assert err.translation_placeholders["until"]
    await env.control(entity_id="light.kids_group", action="turn_off")
    hass.states.async_set(
        "light.kids_group", "on", {"entity_id": ["light.ceiling", "light.inner"]}
    )
    await env.control(entity_id="light.kids_group", action="toggle")

    await env.apply(snapshot(color_lock=True))
    await env.denied(
        "blocked_kids_color",
        entity_id="light.kids_group",
        action="turn_on",
        data={"rgb_color": [255, 0, 0]},
    )
    await env.control(
        entity_id="light.kids_group", action="turn_on", data={"brightness_pct": 30}
    )

    response = await env.control(
        entity_id="media_player.speakers",
        action="volume_set",
        data={"volume_level": 0.9},
    )
    assert response["results"][0]["adjusted"] == {"volume_level": 0.4}
    assert env.calls[-1].data == {
        "volume_level": 0.4,
        "entity_id": "media_player.speakers",
    }


def _kids_of(entities: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "time_zone": "Europe/Zurich",
        "override_until": None,
        "entities": entities,
        **extra,
    }


def _kids_hub(
    mock_api: aioresponses, refuse: list[str] | None = None
) -> list[dict[str, Any]]:
    """kids-check answering allowed, or paused_all for the entities in ``refuse``."""
    bodies: list[dict[str, Any]] = []

    def kids_check(url: URL, **kwargs: Any) -> CallbackResult:
        body = kwargs["json"]
        bodies.append(body)
        if body["entity_id"] in (refuse or []):
            return CallbackResult(
                payload={"allowed": False, "reason": "paused_all", "until": None}
            )
        return CallbackResult(payload={"allowed": True, "reason": None, "until": None})

    mock_api.post(KIDS_URL, callback=kids_check, repeat=True)
    return bodies


async def test_group_counts_its_members_rate_limit(
    env: Env, hass: HomeAssistant, mock_api: aioresponses
) -> None:
    """A member's counter also counts through a group (also nested): the
    hub is asked for that member like a direct call."""
    hass.states.async_set("light.g", "off", {"entity_id": ["light.lamp", "light.in"]})
    hass.states.async_set("light.in", "off", {"entity_id": ["light.deep"]})
    hass.states.async_set("light.lamp", "off")
    hass.states.async_set("light.deep", "off")
    names = ("light.g", "light.in", "light.lamp", "light.deep")
    entities = {n: access() for n in names}
    kids = _kids_of(
        {
            "light.g": kids_rules(),
            "light.in": kids_rules(),
            "light.lamp": kids_rules(rate=True),
            "light.deep": kids_rules(rate=True),
        }
    )
    await env.apply(make_snapshot(CLIENT_ID, env.user.id, entities, kids=kids))
    # No hub answer: refused, like the direct call on the member.
    await env.denied("kids_hub_unreachable", entity_id="light.lamp", action="toggle")
    await env.denied("kids_hub_unreachable", entity_id="light.g", action="toggle")
    refuse: list[str] = []
    bodies = _kids_hub(mock_api, refuse)
    await env.control(entity_id="light.g", action="toggle")
    assert [b["entity_id"] for b in bodies] == ["light.deep", "light.lamp"]
    assert env.calls[-1].data["entity_id"] == "light.g"
    before = len(env.calls)
    refuse.append("light.lamp")
    await env.denied("blocked_kids_paused_all", entity_id="light.g", action="toggle")
    assert len(env.calls) == before


async def test_group_under_pause_everything_counts_once(
    env: Env, hass: HomeAssistant, mock_api: aioresponses
) -> None:
    """ "Pause everything" counts a group as one action of the group (the hub
    counts each allowed check as an entity of its own: five members asked
    one by one would start the pause after one tap that switched nothing).
    A member with its own counter is asked as well."""
    names = [f"light.m{i}" for i in range(5)]
    hass.states.async_set("light.g", "off", {"entity_id": names})
    for name in names:
        hass.states.async_set(name, "off")
    entities = {n: access() for n in ("light.g", *names)}
    rules = {n: kids_rules() for n in entities}
    rules["light.m3"] = kids_rules(rate=True)
    kids = _kids_of(rules, rate_limit_all=dict(PAUSE_ALL))
    await env.apply(make_snapshot(CLIENT_ID, env.user.id, entities, kids=kids))
    bodies = _kids_hub(mock_api)
    await env.control(entity_id="light.g", action="turn_on")
    assert [b["entity_id"] for b in bodies] == ["light.g", "light.m3"]
    assert env.calls[-1].data["entity_id"] == "light.g"


async def test_group_without_counters_asks_nobody(
    env: Env, hass: HomeAssistant, mock_api: aioresponses
) -> None:
    hass.states.async_set("light.g", "off", {"entity_id": ["light.a"]})
    hass.states.async_set("light.a", "off")
    entities = {n: access() for n in ("light.g", "light.a")}
    kids = _kids_of({n: kids_rules() for n in entities})
    await env.apply(make_snapshot(CLIENT_ID, env.user.id, entities, kids=kids))
    bodies = _kids_hub(mock_api)
    await env.control(entity_id="light.g", action="turn_on")
    assert bodies == []


def _speakers(hass: HomeAssistant, volumes: dict[str, float | None]) -> None:
    hass.states.async_set("media_player.grp", "playing", {"entity_id": sorted(volumes)})
    for name, volume in volumes.items():
        attrs = {} if volume is None else {"volume_level": volume}
        hass.states.async_set(name, "playing", attrs)


async def _speaker_rules(env: Env, caps: dict[str, int | None]) -> None:
    entities = {n: access() for n in ("media_player.grp", *caps)}
    kids = _kids_of(
        {
            "media_player.grp": kids_rules(),
            **{n: kids_rules(volume_max=c) for n, c in caps.items()},
        }
    )
    await env.apply(make_snapshot(CLIENT_ID, env.user.id, entities, kids=kids))


@pytest.mark.parametrize(
    ("action", "volumes", "expected"),
    [
        # One step above the loudest member, at most the lowest cap.
        ("volume_up", {"media_player.kid": 0.1, "media_player.loud": 0.2}, 0.3),
        ("volume_up", {"media_player.kid": 0.1, "media_player.loud": 0.9}, None),
        # Down from the quietest: nobody gets louder.
        ("volume_down", {"media_player.kid": 0.1, "media_player.loud": 0.9}, 0.0),
        ("volume_down", {"media_player.kid": 0.25, "media_player.loud": 0.9}, 0.15),
        ("volume_set", {"media_player.kid": 0.1, "media_player.loud": 0.9}, 0.3),
    ],
)
async def test_group_volume_respects_every_members_cap(
    env: Env,
    hass: HomeAssistant,
    action: str,
    volumes: dict[str, float],
    expected: float | None,
) -> None:
    """Home Assistant steps a media player group member by member (the last
    wins): a capped group gets one volume_set at the lowest cap instead."""
    hass.services.async_register("media_player", "volume_down", lambda call: None)
    _speakers(hass, volumes)
    await _speaker_rules(env, {"media_player.kid": 30, "media_player.loud": None})
    data = {"volume_level": 0.9} if action == "volume_set" else {}
    if expected is None:
        await env.denied(
            "blocked_kids_volume", entity_id="media_player.grp", action=action
        )
        return
    response = await env.control(entity_id="media_player.grp", action=action, data=data)
    assert response["results"][0]["adjusted"] == {"volume_level": expected}
    assert env.calls[-1].service == "volume_set"
    assert env.calls[-1].data == {
        "volume_level": expected,
        "entity_id": "media_player.grp",
    }


async def test_group_volume_takes_the_lowest_cap_in_any_order(
    env: Env, hass: HomeAssistant
) -> None:
    _speakers(hass, {"media_player.a_low": 0.1, "media_player.b_high": 0.48})
    await _speaker_rules(env, {"media_player.a_low": 30, "media_player.b_high": 50})
    await env.denied(
        "blocked_kids_volume", entity_id="media_player.grp", action="volume_up"
    )
    _speakers(hass, {"media_player.a_low": 0.1, "media_player.b_high": 0.15})
    response = await env.control(entity_id="media_player.grp", action="volume_up")
    assert response["results"][0]["adjusted"] == {"volume_level": 0.25}


@pytest.mark.parametrize(
    ("action", "volumes", "expected"),
    [
        # A speaker that is off has no volume: left out, the cap still holds.
        ("volume_up", {"media_player.kid": 0.1, "media_player.off": None}, 0.2),
        ("volume_up", {"media_player.kid": 0.3, "media_player.off": None}, None),
        ("volume_up", {"media_player.kid": None, "media_player.off": None}, None),
        ("volume_down", {"media_player.kid": 0.25, "media_player.off": None}, 0.15),
        ("volume_down", {"media_player.kid": None, "media_player.off": None}, 0.0),
    ],
)
async def test_group_volume_with_a_member_that_is_off(
    env: Env,
    hass: HomeAssistant,
    action: str,
    volumes: dict[str, float | None],
    expected: float | None,
) -> None:
    hass.services.async_register("media_player", "volume_down", lambda call: None)
    _speakers(hass, volumes)
    # The cap of the speaker that is off counts as well.
    await _speaker_rules(env, {"media_player.kid": None, "media_player.off": 30})
    if expected is None:
        await env.denied(
            "blocked_kids_volume", entity_id="media_player.grp", action=action
        )
        return
    response = await env.control(entity_id="media_player.grp", action=action)
    assert response["results"][0]["adjusted"] == {"volume_level": expected}


async def test_device_user_calling_an_action_without_entity_is_reported(
    env: Env, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Home Assistant runs it (no veto): detected, reported once per window
    as a direct attempt with a repair naming the device and the action."""
    await env.apply()
    ran: list[ServiceCall] = []
    hass.services.async_register("shell_command", "wipe", ran.append)
    hass.services.async_register("rest_command", "open_door2", ran.append)
    events = async_capture_events(hass, f"{DOMAIN}_event")
    queued = len(env.manager.events._queue)  # noqa: SLF001
    context = Context(user_id=env.user.id)
    await hass.services.async_call(
        "shell_command", "wipe", {}, blocking=True, context=context
    )
    await hass.services.async_call(
        "rest_command", "open_door2", {}, blocking=True, context=context
    )
    await hass.async_block_till_done()
    assert len(ran) == 2  # not blocked
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_SERVICE_CALLED}_{CLIENT_ID}"
    )
    assert issue is not None
    # The repair names the latest action, the alarm stays one per window.
    assert issue.translation_placeholders["service"] == "rest_command.open_door2"
    assert issue.translation_placeholders["device"]
    sent = list(env.manager.events._queue)[queued:]  # noqa: SLF001
    assert [(e["type"], e.get("action"), e["client_id"]) for e in sent] == [
        ("direct_access_attempt", "wipe", CLIENT_ID)
    ]
    assert [e.data["kind"] for e in events].count("direct_access_attempt") == 1
    assert caplog.text.count("called shell_command.wipe") == 1

    # The next window reports again, with the count and an action name
    # that does not fit the contract's pattern left out.
    env.manager._direct_quiet_until.clear()  # noqa: SLF001
    await hass.services.async_call(
        "rest_command", "open_door2", {}, blocking=True, context=context
    )
    sent = list(env.manager.events._queue)[queued:]  # noqa: SLF001
    assert sent[-1].get("action") is None and sent[-1]["count"] == 3
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_SERVICE_CALLED}_{CLIENT_ID}"
    )
    assert issue is not None
    assert issue.translation_placeholders["service"] == "rest_command.open_door2"

    # Forgetting the device removes the repair.
    await env.manager.async_forget(CLIENT_ID)
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"{ISSUE_SERVICE_CALLED}_{CLIENT_ID}"
        )
        is None
    )


async def test_steps_of_a_script_the_device_started_are_not_reported(
    env: Env, hass: HomeAssistant, tmp_path: Any
) -> None:
    """A script runs its steps in the caller's context: a step without a
    target is part of the device's expected call. A call of the device with
    a fresh context is still reported."""
    await env.apply(control_path="direct")
    ran: list[ServiceCall] = []
    hass.services.async_register("notify", "family", ran.append)
    # The script integration installs its blueprints into the config dir.
    hass.config.config_dir = str(tmp_path)
    assert await async_setup_component(
        hass,
        "script",
        {"script": {"tell": {"sequence": [{"action": "notify.family", "data": {}}]}}},
    )
    device = Context(user_id=env.user.id)
    await hass.services.async_call(
        "script", "turn_on", {"entity_id": "script.tell"}, blocking=True, context=device
    )
    await hass.async_block_till_done()
    assert len(ran) == 1
    issue_id = f"{ISSUE_SERVICE_CALLED}_{CLIENT_ID}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    # A child context of an expected call is expected too.
    child = Context(user_id=env.user.id, parent_id=device.id)
    await hass.services.async_call("notify", "family", {}, blocking=True, context=child)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    await hass.services.async_call(
        "notify", "family", {}, blocking=True, context=Context(user_id=env.user.id)
    )
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_placeholders["service"] == "notify.family"


async def test_a_target_key_on_a_non_entity_domain_does_not_excuse_the_call(
    env: Env, hass: HomeAssistant
) -> None:
    """shell_command and rest_command take any field: an entity_id there is
    data, not a target Home Assistant checks."""
    await env.apply(control_path="direct")
    hass.services.async_register("shell_command", "open_gate", lambda call: None)
    await hass.services.async_call(
        "shell_command",
        "open_gate",
        {"entity_id": "light.bed"},
        blocking=True,
        context=Context(user_id=env.user.id),
    )
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_SERVICE_CALLED}_{CLIENT_ID}"
    )
    assert issue is not None
    assert issue.translation_placeholders["service"] == "shell_command.open_gate"


async def test_expected_calls_are_not_reported(
    env: Env, hass: HomeAssistant, existing_owner: MockUser
) -> None:
    """Own device actions, allowlisted actions, actions with a target,
    other users and system calls are left alone."""
    await env.apply(control_path="direct")
    hass.services.async_register("shell_command", "wipe", lambda call: None)
    hass.services.async_register("weather", "get_forecasts", lambda call: None)
    device = Context(user_id=env.user.id)
    await env.control(entity_id="light.bed", action="turn_on")
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": "light.bed"}, blocking=True, context=device
    )
    await hass.services.async_call(
        "weather",
        "get_forecasts",
        {"entity_id": "weather.home"},
        blocking=True,
        context=device,
    )
    await hass.services.async_call(
        "shell_command",
        "wipe",
        {},
        blocking=True,
        context=Context(user_id=existing_owner.id),
    )
    await hass.services.async_call("shell_command", "wipe", {}, blocking=True)
    await hass.async_block_till_done()
    issues = [
        i
        for (d, i) in ir.async_get(hass).issues
        if d == DOMAIN and i.startswith(ISSUE_SERVICE_CALLED)
    ]
    assert issues == []


async def test_apply_invalid_snapshot_is_rejected(env: Env) -> None:
    bad = make_snapshot(CLIENT_ID, env.user.id, ENTITIES)
    bad["rules_hash"] = "0" * 64
    with pytest.raises(ServiceValidationError):
        await env.apply(bad)


async def test_rejected_snapshot_in_complete_set_never_loosens(
    env: Env, hass: HomeAssistant
) -> None:
    """A hub one contract version ahead: the earlier rules stay."""
    await env.apply()  # control path checked
    old_hash = env.manager.snapshots[CLIENT_ID]["rules_hash"]
    device_users = env.entry.runtime_data.device_users
    issues = ir.async_get(hass)
    cases = []
    newer = make_snapshot(CLIENT_ID, env.user.id, ENTITIES, control_path="direct")
    newer["future_field"] = True
    cases.append(newer)
    bad_hash = make_snapshot(CLIENT_ID, env.user.id, ENTITIES, control_path="direct")
    bad_hash["rules_hash"] = "0" * 64
    cases.append(bad_hash)
    other = MockUser(name="Family member").add_to_hass(hass)
    cases.append(make_snapshot(CLIENT_ID, other.id, ENTITIES, control_path="direct"))
    for case in cases:
        assert (
            await env.manager.async_apply(
                [case], [], True, device_users, _paths([case])
            )
            == 0
        )
        await _settle(hass)
        assert env.manager.snapshots[CLIENT_ID]["rules_hash"] == old_hash
        assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]
        issue = issues.async_get_issue(DOMAIN, ISSUE_UNREADABLE)
        assert issue is not None and issue.translation_key == ISSUE_UNREADABLE
        assert issue.translation_placeholders == {
            "devices": env.hub.client(CLIENT_ID)["name"]
        }
    # an entry that names no device: nobody is absent for sure, nothing goes
    assert (
        await env.manager.async_apply(
            [{"x": 1}], [], True, device_users, _paths([{"x": 1}])
        )
        == 0
    )
    assert CLIENT_ID in env.manager.snapshots
    # readable again: applied, the notice goes
    await env.manager.async_apply(
        [make_snapshot(CLIENT_ID, env.user.id, ENTITIES, control_path="direct")],
        [],
        True,
        device_users,
        {CLIENT_ID: "direct"},
    )
    await _settle(hass)
    assert [g.id for g in env.user.groups] == [GROUP_ID_USER]
    assert issues.async_get_issue(DOMAIN, ISSUE_UNREADABLE) is None
    # a device neither the hub's list nor its status names is forgotten
    await env.apply()
    env.entry.runtime_data.client_by_id.pop(CLIENT_ID)
    await env.manager.async_apply([], [], True, device_users, _paths([]))
    await _settle(hass)
    assert env.manager.snapshots == {}
    assert [g.id for g in env.user.groups] == [GROUP_ID_USER]


async def test_rejected_snapshot_without_earlier_rules_holds_read_only(
    env: Env, hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """No earlier rules to keep: the device user is read-only until rules
    can be read, also against the user settings and over a restart."""
    device_users = env.entry.runtime_data.device_users
    assert [g.id for g in env.user.groups] == [GROUP_ID_USER]
    newer = make_snapshot(CLIENT_ID, env.user.id, ENTITIES, control_path="direct")
    newer["future_field"] = True
    for complete in (False, True):
        await env.manager.async_apply(
            [newer], [], complete, device_users, _paths([newer])
        )
        await _settle(hass)
        assert env.manager.snapshots == {}
        assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]
    assert device_users.group_id_of(CLIENT_ID) == GROUP_ID_READ_ONLY
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNREADABLE) is not None
    await env.denied("rules_missing", entity_id="light.bed", action="turn_on")
    # saved in the HA user settings: set back at once
    await hass.auth.async_update_user(env.user, group_ids=[GROUP_ID_USER])
    await _settle(hass)
    assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]
    # a restart keeps it
    await env.manager._store.async_save(env.manager._data())  # noqa: SLF001
    again = EnforcementManager(hass, env.manager.allowlist, env.manager.events)
    await again.async_setup()
    again.async_shutdown()
    assert again.desired_group_id(CLIENT_ID) == GROUP_ID_READ_ONLY
    # still listed by the status: a set without it keeps the hold
    await env.manager.async_apply([], [], True, device_users, _paths([]))
    await _settle(hass)
    assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]
    # the hub no longer lists the device: free again
    env.entry.runtime_data.client_by_id.pop(CLIENT_ID)
    await env.manager.async_apply([], [], True, device_users, _paths([]))
    await _settle(hass)
    assert [g.id for g in env.user.groups] == [GROUP_ID_USER]
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNREADABLE) is None


@pytest.mark.parametrize("path", ["checked", "hub_only", None])
async def test_rejected_tightening_holds_an_earlier_direct_device(
    env: Env, hass: HomeAssistant, path: str | None
) -> None:
    """The hub moves a direct device to checked in a snapshot this version
    cannot read: control_paths next to it holds the user read-only, the
    earlier snapshot stays for the decisions, and the hold survives a restart."""
    await env.apply(control_path="direct")
    assert [g.id for g in env.user.groups] == [GROUP_ID_USER]
    old_hash = env.manager.snapshots[CLIENT_ID]["rules_hash"]
    device_users = env.entry.runtime_data.device_users
    newer = make_snapshot(CLIENT_ID, env.user.id, ENTITIES, control_path="checked")
    newer["version"] = 2  # a snapshot format of a later contract
    for complete in (False, True):
        assert (
            await env.manager.async_apply(
                [newer], [], complete, device_users, {CLIENT_ID: path}
            )
            == 0
        )
        await _settle(hass)
        assert env.manager.snapshots[CLIENT_ID]["rules_hash"] == old_hash
        assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNREADABLE) is not None
    # saved in the HA user settings: set back at once
    await hass.auth.async_update_user(env.user, group_ids=[GROUP_ID_USER])
    await _settle(hass)
    assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]
    # a restart keeps the hold next to the earlier snapshot
    await env.manager._store.async_save(env.manager._data())  # noqa: SLF001
    again = EnforcementManager(hass, env.manager.allowlist, env.manager.events)
    await again.async_setup()
    again.async_shutdown()
    assert CLIENT_ID in again.snapshots
    assert again.desired_group_id(CLIENT_ID) == GROUP_ID_READ_ONLY
    # readable rules again decide
    await env.apply(control_path="direct")
    assert [g.id for g in env.user.groups] == [GROUP_ID_USER]


async def test_rejected_snapshot_of_a_direct_device_stays_direct(
    env: Env, hass: HomeAssistant
) -> None:
    """control_paths "direct" with a rejected snapshot changes nothing."""
    await env.apply(control_path="direct")
    device_users = env.entry.runtime_data.device_users
    newer = make_snapshot(CLIENT_ID, env.user.id, ENTITIES, control_path="direct")
    newer["version"] = 2
    await env.manager.async_apply(
        [newer], [], True, device_users, {CLIENT_ID: "direct"}
    )
    await _settle(hass)
    assert [g.id for g in env.user.groups] == [GROUP_ID_USER]


async def test_control_paths_never_loosen(env: Env, hass: HomeAssistant) -> None:
    """direct next to a rejected snapshot does not lift a checked one."""
    await env.apply()  # checked
    device_users = env.entry.runtime_data.device_users
    newer = make_snapshot(CLIENT_ID, env.user.id, ENTITIES, control_path="direct")
    newer["version"] = 2
    await env.manager.async_apply(
        [newer], [], True, device_users, {CLIENT_ID: "direct"}
    )
    await _settle(hass)
    assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]


async def test_control_paths_hold_a_device_whose_snapshot_names_nothing(
    env: Env, hass: HomeAssistant
) -> None:
    """A snapshot body this version cannot even attribute: the map still names
    the device, so a direct device goes read-only."""
    await env.apply(control_path="direct")
    device_users = env.entry.runtime_data.device_users
    await env.manager.async_apply(
        [{"device": CLIENT_ID}], [], False, device_users, {CLIENT_ID: "checked"}
    )
    await _settle(hass)
    assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]


@pytest.mark.parametrize(
    ("path", "group"), [("checked", GROUP_ID_READ_ONLY), ("direct", GROUP_ID_USER)]
)
async def test_complete_set_without_a_snapshot_follows_control_paths(
    env: Env, hass: HomeAssistant, path: str, group: str
) -> None:
    """The hub could not build a device's snapshot (direct before, checked now):
    a complete set without it but with its control path holds it read-only;
    "direct" keeps the earlier snapshot as a device without rules."""
    await env.apply(control_path="direct")
    old_hash = env.manager.snapshots[CLIENT_ID]["rules_hash"]
    device_users = env.entry.runtime_data.device_users
    await env.manager.async_apply([], [], True, device_users, {CLIENT_ID: path})
    await _settle(hass)
    assert env.manager.snapshots[CLIENT_ID]["rules_hash"] == old_hash
    assert [g.id for g in env.user.groups] == [group]
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNREADABLE) is not None


def _still_provisioning(env: Env) -> None:
    """The status lists the device's request in provisioning[] (also in the
    next polls: the hub has not stored the device's user yet)."""
    env.hub.clear_provisioning_on_ack = False
    env.hub.snapshot["provisioning"] = [_request(env.hub.client(CLIENT_ID))]
    env.entry.runtime_data.data["provisioning"] = [{"client_id": CLIENT_ID}]


async def test_device_named_without_snapshot_and_not_provisioning_gets_the_repair(
    env: Env, hass: HomeAssistant
) -> None:
    """Not in provisioning[]: the hub could not build its snapshot."""
    env.entry.runtime_data.data["provisioning"] = []
    device_users = env.entry.runtime_data.device_users
    await env.manager.async_apply([], [], False, device_users, {CLIENT_ID: "checked"})
    await _settle(hass)
    assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNREADABLE) is not None


async def test_device_waiting_too_long_for_its_first_snapshot_gets_the_repair(
    env: Env, hass: HomeAssistant, freezer: Any
) -> None:
    """Still in provisioning[] but past the grace period: the repair after the
    next successful poll; the hold stays."""
    _still_provisioning(env)
    device_users = env.entry.runtime_data.device_users
    await env.manager.async_apply([], [], False, device_users, {CLIENT_ID: "checked"})
    await _settle(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNREADABLE) is None
    await _poll(hass, env.entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNREADABLE) is None
    freezer.tick(AWAITING_GRACE + timedelta(seconds=1))
    await _poll(hass, env.entry)
    assert env.entry.runtime_data.data["provisioning"]
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNREADABLE) is not None
    assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]


@pytest.mark.parametrize("complete", [False, True])
async def test_device_named_before_its_first_snapshot_is_held_without_repair(
    env: Env, hass: HomeAssistant, complete: bool
) -> None:
    """A device still being set up: the map names it before the hub sent its
    first snapshot, and the status still lists it in provisioning[]. Its user
    is read-only (fail closed), but there is no "rules unreadable" repair,
    also not after a restart; the first snapshot decides. A snapshot that
    cannot be read does raise the repair."""
    assert CLIENT_ID not in env.manager.snapshots
    _still_provisioning(env)
    device_users = env.entry.runtime_data.device_users
    for _ in range(2):
        await env.manager.async_apply(
            [], [], complete, device_users, {CLIENT_ID: "checked"}
        )
        await _settle(hass)
        assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]
        assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNREADABLE) is None

    await env.manager._store.async_save(env.manager._data())  # noqa: SLF001
    again = EnforcementManager(hass, env.manager.allowlist, env.manager.events)
    await again.async_setup()
    again.async_shutdown()
    assert again.desired_group_id(CLIENT_ID) == GROUP_ID_READ_ONLY
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNREADABLE) is None

    # A first snapshot that cannot be read is a problem to repair.
    newer = make_snapshot(CLIENT_ID, env.user.id, ENTITIES)
    newer["version"] = 2
    await env.manager.async_apply(
        [newer], [], complete, device_users, {CLIENT_ID: "checked"}
    )
    await _settle(hass)
    assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNREADABLE) is not None
    # ... and stays one when the map alone names the device next time.
    await env.manager.async_apply([], [], False, device_users, {CLIENT_ID: "checked"})
    await _settle(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNREADABLE) is not None

    # Readable rules decide.
    await env.apply(control_path="direct")
    assert [g.id for g in env.user.groups] == [GROUP_ID_USER]
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNREADABLE) is None


async def test_device_named_before_its_first_snapshot_gets_it(
    env: Env, hass: HomeAssistant
) -> None:
    _still_provisioning(env)
    device_users = env.entry.runtime_data.device_users
    await env.manager.async_apply([], [], False, device_users, {CLIENT_ID: "direct"})
    await env.manager.async_apply([], [], False, device_users, {CLIENT_ID: "checked"})
    await _settle(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNREADABLE) is None
    await env.apply()  # checked
    assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]
    assert env.manager._awaiting == {}  # noqa: SLF001
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNREADABLE) is None


async def test_unreadable_stored_snapshot_holds_read_only(
    env: Env, hass: HomeAssistant
) -> None:
    """A stored snapshot this version cannot read does not free its user."""
    await env.apply()
    data = copy.deepcopy(env.manager._data())  # noqa: SLF001
    data["snapshots"][CLIENT_ID]["future_field"] = True
    await env.manager._store.async_save(data)  # noqa: SLF001
    again = EnforcementManager(hass, env.manager.allowlist, env.manager.events)
    await again.async_setup()
    again.async_shutdown()
    assert again.snapshots == {}
    assert again.desired_group_id(CLIENT_ID) == GROUP_ID_READ_ONLY
    await again.async_sync_groups()
    assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]


@pytest.mark.parametrize("payload", [{}, {"snapshots": None}, {"snapshots": []}])
async def test_rules_answer_without_the_device_never_loosens(
    env: Env, hass: HomeAssistant, mock_api: aioresponses, payload: dict[str, Any]
) -> None:
    """No list of snapshots is a failed fetch; a list without a device the
    status still lists keeps that device's rules."""
    await env.apply()
    old_hash = env.manager.snapshots[CLIENT_ID]["rules_hash"]
    mock_api.get(RULES_URL, status=200, payload=payload, repeat=True)
    env.hub.snapshot["enforcement"] = {"rules_hashes": {CLIENT_ID: "f" * 64}}
    await _poll(hass, env.entry)
    assert env.manager.hashes() == {CLIENT_ID: old_hash}
    assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNREADABLE)
    assert (issue is not None) is (payload == {"snapshots": []})


async def test_complete_push_without_snapshots_changes_nothing(
    env: Env, hass: HomeAssistant
) -> None:
    await env.apply()
    device_users = env.entry.runtime_data.device_users
    for data in (
        {"complete": True},
        {"complete": True, "snapshots": None, "removed": [CLIENT_ID]},
        {"removed": [CLIENT_ID]},
    ):
        assert async_handle_push(
            hass, device_users, {"type": "enforcement_rules", "data": data}
        )
        await _settle(hass)
        assert CLIENT_ID in env.manager.snapshots
        assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]
    assert (
        await env.manager.async_apply(
            None, [CLIENT_ID], True, device_users, _paths(None)
        )
        == 0
    )
    assert CLIENT_ID in env.manager.snapshots


@pytest.mark.parametrize("complete", [True, False])
async def test_device_without_rules_but_listed_by_the_status_is_kept(
    env: Env, hass: HomeAssistant, complete: bool
) -> None:
    """The hub left the device out (complete set) or names it as removed,
    but its status still lists it as active: its rules stay until the
    status drops it."""
    await env.apply()
    device_users = env.entry.runtime_data.device_users
    issues = ir.async_get(hass)
    removed = [] if complete else [CLIENT_ID]
    await env.manager.async_apply([], removed, complete, device_users, _paths([]))
    await _settle(hass)
    assert CLIENT_ID in env.manager.snapshots
    assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]
    assert issues.async_get_issue(DOMAIN, ISSUE_UNREADABLE) is not None
    # a poll that still lists the device changes nothing
    await _poll(hass, env.entry)
    assert CLIENT_ID in env.manager.snapshots
    # a valid snapshot ends it
    await env.apply()
    assert issues.async_get_issue(DOMAIN, ISSUE_UNREADABLE) is None
    # again without rules; then the status lists the device as inactive
    # while the hub still holds its user: forgotten with that poll
    await env.manager.async_apply([], removed, complete, device_users, _paths([]))
    env.hub.client(CLIENT_ID)["is_active"] = False
    env.hub.snapshot["ha_user_holds"] = [CLIENT_ID]
    await _poll(hass, env.entry)
    assert env.manager.snapshots == {}
    assert [g.id for g in env.user.groups] == [GROUP_ID_USER]
    assert issues.async_get_issue(DOMAIN, ISSUE_UNREADABLE) is None


async def test_device_without_rules_is_kept_while_no_status_is_known(
    env: Env, hass: HomeAssistant
) -> None:
    """Without a loaded entry there is no status: nothing is freed on a guess."""
    await env.apply()
    device_users = env.entry.runtime_data.device_users
    assert await hass.config_entries.async_unload(env.entry.entry_id)
    await env.manager.async_apply([], [CLIENT_ID], True, device_users, _paths([]))
    await _settle(hass)
    assert CLIENT_ID in env.manager.snapshots
    assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]


async def test_rejected_snapshot_of_an_unknown_device_is_not_reported(
    env: Env, hass: HomeAssistant
) -> None:
    """No device user of this integration: nothing is kept, nothing to name."""
    other = "77777777-7777-4777-8777-777777777777"
    snap = make_snapshot(other, env.user.id, ENTITIES)
    device_users = env.entry.runtime_data.device_users
    assert (
        await env.manager.async_apply([snap], [], False, device_users, _paths([snap]))
        == 0
    )
    broken = make_snapshot(other, env.user.id, ENTITIES)
    broken["future_field"] = True
    assert (
        await env.manager.async_apply(
            [broken], [], True, device_users, _paths([broken])
        )
        == 0
    )
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_UNREADABLE) is None
    assert env.manager.diagnostics()["devices"] == {}


async def test_forget_sets_read_only_user_back(env: Env, hass: HomeAssistant) -> None:
    await env.apply()
    assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]
    await env.manager.async_forget(CLIENT_ID)
    assert [g.id for g in env.user.groups] == [GROUP_ID_USER]


async def test_rate_window_slides(env: Env) -> None:
    from collections import deque

    env.manager._calls[env.user.id] = deque([0.0] * 500)
    assert env.manager.rate_ok(env.user.id)


async def test_direct_attempt_window_and_quiet(env: Env, hass: HomeAssistant) -> None:
    await env.apply()
    alarms = async_capture_events(hass, f"{DOMAIN}_alarm")
    manager = env.manager
    from collections import deque

    manager._direct[CLIENT_ID] = deque([dt_util.utcnow() - timedelta(hours=1)])
    for _ in range(DIRECT_THRESHOLD * 2):
        manager.async_direct_attempt(env.user.id, None)
    manager.async_direct_attempt("unknown-user", None)
    await _settle(hass)
    assert len(alarms) == 1  # second burst is within the quiet time


def test_direct_handler_survives_closed_loop() -> None:
    from homeassistant.exceptions import Unauthorized

    from custom_components.halumar.enforcement import _DirectAttemptHandler

    loop = SimpleNamespace(
        call_soon_threadsafe=lambda *a: (_ for _ in ()).throw(RuntimeError("closed"))
    )
    manager = SimpleNamespace(
        hass=SimpleNamespace(loop=loop), async_direct_attempt=None
    )
    handler = _DirectAttemptHandler(manager)  # type: ignore[arg-type]
    record = logging.LogRecord(
        "x", logging.ERROR, "", 0, "%s", (Unauthorized(user_id="u"),), None
    )
    handler.emit(record)


async def test_heartbeat_final_write_and_restore_state(
    env: Env, hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    from homeassistant.const import EVENT_HOMEASSISTANT_FINAL_WRITE
    from homeassistant.core import State

    from ._ha import async_fire_time_changed, mock_restore_cache

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=11))
    await _settle(hass)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await _settle(hass)
    assert hass_storage[STORAGE_KEY]["data"]["stopped_at"]
    mock_restore_cache(hass, [State("light.bed", "on")])
    assert env.manager._ha_last_seen() is not None


async def test_applied_groups_loaded(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    snap = make_snapshot(CLIENT_ID, "a" * 32, {"light.x": access()})
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "snapshots": {CLIENT_ID: snap},
            "applied_groups": {
                CLIENT_ID: GROUP_ID_READ_ONLY,
                GUEST_ID: GROUP_ID_USER,
                "x": "bad",
            },
            "names": {CLIENT_ID: "x"},
        },
    }
    assert await async_setup_component(hass, DOMAIN, {})
    assert hass.data[DATA_ENFORCEMENT]._applied_groups == {
        CLIENT_ID: GROUP_ID_READ_ONLY
    }


async def test_rule_sync_fetch_failure_and_bad_push(
    env: Env, hass: HomeAssistant, mock_api: aioresponses
) -> None:
    mock_api.get(RULES_URL, status=500, repeat=True)
    env.hub.snapshot["enforcement"] = {"rules_hashes": {CLIENT_ID: "f" * 64}}
    await env.entry.runtime_data.async_refresh()
    await _settle(hass)
    assert env.manager.snapshots == {}
    assert async_handle_push(
        hass,
        env.entry.runtime_data.device_users,
        {"type": "enforcement_rules", "data": "x"},
    )


async def test_event_reporter_edges(env: Env, hass: HomeAssistant) -> None:
    from custom_components.halumar.events import device_name_of_user

    reporter = env.manager.events
    assert device_name_of_user(None) is None
    assert not reporter.async_fire_hub_event("x")
    assert not reporter.async_fire_hub_event(
        {"kind": "guest_paired", "category": "guests", "at": "never"}
    )
    alarms = async_capture_events(hass, f"{DOMAIN}_alarm")
    reporter.kinds = {
        **reporter.kinds,
        "hub_alarm": {
            "kind": "hub_alarm",
            "category": "alarms",
            "source": "hub",
            "alarm": True,
        },
    }
    assert reporter.async_fire_hub_event(
        {"kind": "hub_alarm", "category": "alarms", "at": "2026-11-20T18:00:00+00:00"}
    )
    reporter.record("unknown_kind")
    await _settle(hass)
    assert len(alarms) == 1
    # The hub's "everything paused" is fired as <ns>_event, no alarm.
    fired = async_capture_events(hass, f"{DOMAIN}_event")
    assert reporter.async_fire_hub_event(
        {
            "kind": "kids_paused_all",
            "category": "kids",
            "at": "2026-11-20T18:00:00+00:00",
            "client_id": CLIENT_ID,
            "reason": "blocked_kids_paused_all",
            "until": "2026-11-20T18:05:00+00:00",
        }
    )
    await _settle(hass)
    assert fired[-1].data["kind"] == "kids_paused_all" and len(alarms) == 1
    reporter.coordinator_getter = lambda: None
    await reporter.async_flush()


# ── The integration's own entities are never operated by a device ──


async def test_own_entities_are_refused_for_devices(
    env: Env, hass: HomeAssistant
) -> None:
    """P0 gap of the wall panel: a checked (or kids) device with all areas
    cannot press, set or switch an entity of the integration itself, even if
    an admin put it into an area; admins and automations keep the direct way."""
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    own = [
        e.entity_id
        for e in registry.entities.values()
        if e.platform == DOMAIN and e.domain in ("button", "switch")
    ]
    assert own, "the integration registers buttons and switches"
    for entity_id in own:
        if hass.states.get(entity_id) is None:
            hass.states.async_set(entity_id, "off")
    # A panel entity of another device (number) in the snapshot as well.
    panel = registry.async_get_or_create(
        "number", DOMAIN, f"{env.entry.entry_id}_client_x_panel_brightness_control"
    )
    hass.states.async_set(panel.entity_id, "50")
    entities = {**ENTITIES, **{eid: access() for eid in [*own, panel.entity_id]}}
    await env.apply(make_snapshot(CLIENT_ID, env.user.id, entities))
    for entity_id in own:
        action = "press" if entity_id.startswith("button.") else "turn_on"
        await env.denied("own_entity", entity_id=entity_id, action=action)
    await env.denied(
        "own_entity", entity_id=panel.entity_id, action="set_value", data={"value": 10}
    )
    # Other integrations' entities of the same domains still work.
    await env.control(entity_id="light.ceiling", action="turn_on")
    # check_only answers per entity too, nothing executed.
    response = await env.control(
        entity_ids=[panel.entity_id],
        action="set_value",
        data={"value": 10},
        check_only=True,
    )
    assert response["results"][0]["reason"] == "own_entity"
    assert not [c for c in env.calls if c.domain in ("button", "switch", "number")]
