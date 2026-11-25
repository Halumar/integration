"""Devices on the hub path (Home Assistant access "Hub only").

A device on Hub only holds no Home Assistant key unless its profile allows
remote access. The hub reads and switches for it: it calls halumar.control
with the device's own token from the app network, without a grant (only
children get one). The integration has no special case for this:

* the hub sends the snapshot with the control path ``checked`` and no kids
  rules, so the device user is read-only, and local only without remote
  access;
* the hub's call is judged as a local call and goes through every check of
  the device's snapshot;
* a grant only matters to the hub's kids check, which is asked only for
  snapshots with kids rules: it never loosens anything;
* when the hub withdraws the key it rotates the token: the token the device
  held stops working, the user stays.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from aioresponses import CallbackResult, aioresponses
from homeassistant.auth.const import GROUP_ID_READ_ONLY
from homeassistant.auth.models import User
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component
from yarl import URL

from custom_components.halumar.const import DOMAIN
from custom_components.halumar.snapshot import (
    SnapshotError,
    rules_hash,
    validate_snapshot,
)

from .common import GUEST_ID
from .test_device_users import _request, _settle, _user_of
from .test_enforce import (  # noqa: F401 - fixtures
    ALL_DAY,
    KIDS_URL,
    Env,
    _paths,
    access,
    env,
    existing_owner,
    kids_rules,
    make_snapshot,
)
from .test_scene_targets import _entities, scenes  # noqa: F401 - fixture

# An address of the Supervisor network the hub app calls from.
APP_NETWORK = "172.30.32.2"


def _guest_entities() -> dict[str, Any]:
    return _entities(
        **{
            "cover.garage": access(local="view"),
            "scene.hidden_effects": access(),
            "light.group_ok": access(),
            "light.group_bad": access(),
        }
    )


class Guest:
    """A guest device on Hub only next to the enforcement env."""

    def __init__(self, device_env: Env, user: User) -> None:
        self.env = device_env
        self.hass = device_env.hass
        self.user = user

    def snapshot(self, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("control_path", "checked")
        return make_snapshot(GUEST_ID, self.user.id, _guest_entities(), **kwargs)

    async def apply(self, snap: dict[str, Any] | None = None, **kwargs: Any) -> None:
        snap = snap or self.snapshot(**kwargs)
        await self.hass.services.async_call(
            DOMAIN,
            "apply_rules",
            {"snapshots": [snap], "control_paths": _paths([snap])},
            blocking=True,
            return_response=True,
        )
        await _settle(self.hass)

    async def hub_call(self, remote: str = APP_NETWORK, **data: Any) -> Any:
        """halumar.control as the hub calls it: the device's user, from ``remote``."""
        with (
            patch(
                "custom_components.halumar.control.is_cloud_connection",
                return_value=False,
            ),
            patch("custom_components.halumar.control.current_request") as cur,
        ):
            cur.get.return_value = SimpleNamespace(remote=remote)
            return await self.hass.services.async_call(
                DOMAIN,
                "control",
                data,
                blocking=True,
                context=Context(user_id=self.user.id),
                return_response=True,
            )

    async def refused(self, reason: str, **data: Any) -> None:
        with pytest.raises(ServiceValidationError) as err:
            await self.hub_call(**data)
        assert err.value.translation_key == reason, (data, err.value.translation_key)


async def _provision_guest(device_env: Env, **overrides: Any) -> User:
    device_users = device_env.entry.runtime_data.device_users
    assert await device_users.async_provision(
        _request(device_env.hub.client(GUEST_ID), **overrides)
    )
    await _settle(device_env.hass)
    return await _user_of(device_env.hass, device_env.entry, GUEST_ID)


@pytest.fixture
async def guest(scenes: Env) -> Guest:  # noqa: F811 - fixture
    """A paired guest without remote access, with real scenes and scripts."""
    return Guest(scenes, await _provision_guest(scenes))


def _kids_calls(mock_api: aioresponses) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []

    def kids(url: URL, **kwargs: Any) -> CallbackResult:
        bodies.append(kwargs["json"])
        return CallbackResult(status=200, payload={"allowed": True})

    mock_api.post(KIDS_URL, callback=kids, repeat=True)
    return bodies


# ── (a) the snapshot of a device on Hub only ─────────────────────────────


async def test_hub_only_guest_snapshot_is_accepted_as_checked(
    guest: Guest, hass: HomeAssistant
) -> None:
    """The hub sends checked and no kids rules: read-only and local only."""
    assert guest.user.local_only is True
    await guest.apply()
    snap = guest.env.manager.snapshots[GUEST_ID]
    assert snap["control_path"] == "checked"
    assert snap["kids"] is None and snap["child_protection"] is False
    assert [g.id for g in guest.user.groups] == [GROUP_ID_READ_ONLY]
    assert guest.user.local_only is True
    assert not guest.user.is_admin


async def test_hub_only_with_remote_access_stays_read_only(
    guest: Guest, hass: HomeAssistant
) -> None:
    """Remote access lifts local only (a read-only key), never the group."""
    await guest.apply(remote_access=True)
    identity = _request(guest.env.hub.client(GUEST_ID), remote_allowed=True)
    for key in ("token_action", "reason", "request_id"):
        identity.pop(key)
    await guest.env.entry.runtime_data.device_users.async_handle_push(
        {"type": "update_client_identity", "data": identity}
    )
    await _settle(hass)
    assert guest.user.local_only is False
    assert [g.id for g in guest.user.groups] == [GROUP_ID_READ_ONLY]


@pytest.mark.parametrize("path", ["hub", "none", None])
def test_snapshot_with_a_path_home_assistant_does_not_know_is_refused(
    path: Any,
) -> None:
    """Home Assistant knows checked and direct only; anything else fails closed."""
    snap = make_snapshot(GUEST_ID, "a" * 32, {}, control_path="checked")
    assert validate_snapshot(dict(snap))["control_path"] == "checked"
    snap["control_path"] = path
    snap["rules_hash"] = rules_hash(snap)
    with pytest.raises(SnapshotError):
        validate_snapshot(snap)


async def test_unknown_path_holds_the_device_read_only(
    guest: Guest, hass: HomeAssistant
) -> None:
    """A snapshot with the rule value hub instead of checked is not taken."""
    snap = guest.snapshot()
    snap["control_path"] = "hub"
    snap["rules_hash"] = rules_hash(snap)
    with pytest.raises(ServiceValidationError):
        await guest.apply(snap)
    assert GUEST_ID not in guest.env.manager.snapshots
    assert [g.id for g in guest.user.groups] == [GROUP_ID_READ_ONLY]
    await guest.refused("rules_missing", entity_id="light.bed", action="turn_on")


# ── (b) the hub's call goes through every check ──────────────────────────


async def test_hub_call_is_local_and_switches_what_the_profile_allows(
    guest: Guest, mock_api: aioresponses
) -> None:
    """Without a grant, from the app network, without remote access."""
    kids = _kids_calls(mock_api)
    await guest.apply()
    before = len(guest.env.calls)
    response = await guest.hub_call(entity_id="light.bed", action="turn_on")
    assert response["route"] == "local"
    assert response["results"][0]["executed"] is True
    assert len(guest.env.calls) == before + 1
    assert guest.env.calls[-1].context.user_id is None  # runs as the system
    # The same device from outside the home: no remote access, nothing.
    await guest.refused(
        "blocked_remote", remote="203.0.113.7", entity_id="light.bed", action="turn_on"
    )
    assert kids == []


async def test_hub_call_goes_through_every_check_of_the_snapshot(
    guest: Guest, hass: HomeAssistant, mock_api: aioresponses
) -> None:
    kids = _kids_calls(mock_api)
    await guest.apply()
    before = len(guest.env.calls)
    # Area scope.
    await guest.refused("blocked_area", entity_id="light.not_listed", action="turn_on")
    # Allowed controls: view only allows the safe direction.
    await guest.refused(
        "blocked_view_only", entity_id="cover.garage", action="open_cover"
    )
    # Allowlist: action, fields and values.
    await guest.refused("not_allowed_action", entity_id="light.bed", action="set_level")
    await guest.refused(
        "not_allowed_field",
        entity_id="light.bed",
        action="turn_on",
        data={"entity_id": "light.not_listed"},
    )
    await guest.refused(
        "not_allowed_value",
        entity_id="light.bed",
        action="turn_on",
        data={"brightness": 999},
    )
    # Release of scenes and scripts.
    await guest.refused(
        "scene_not_released", entity_id="scene.hidden_effects", action="turn_on"
    )
    # Group members.
    await guest.refused(
        "blocked_group_member", entity_id="light.group_bad", action="turn_on"
    )
    # Targets of scenes and scripts outside the scope.
    await guest.refused(
        "blocked_group_member", entity_id="scene.outside", action="turn_on"
    )
    await guest.refused(
        "blocked_group_member", entity_id="script.outside", action="turn_on"
    )
    assert len(guest.env.calls) == before
    # What the profile allows runs, with its targets.
    response = await guest.hub_call(entity_id="scene.filmabend", action="turn_on")
    assert response["results"][0]["executed"] is True
    response = await guest.hub_call(entity_id="light.group_ok", action="turn_on")
    assert response["results"][0]["executed"] is True
    assert kids == []


@pytest.mark.parametrize(
    "closed",
    [
        {"switch": False, "hours": False, "windows": []},
        {"switch": True, "hours": True, "windows": []},
    ],
)
async def test_hub_call_outside_the_access_is_refused(
    guest: Guest, closed: dict[str, Any]
) -> None:
    await guest.apply(
        access={**closed, "time_zone": "Europe/Zurich", "early_until": None}
    )
    await guest.refused(
        "blocked_access_closed", entity_id="light.bed", action="turn_on"
    )


async def test_hub_calls_count_against_the_rate_limit(guest: Guest) -> None:
    await guest.apply()
    with patch.object(guest.env.manager.allowlist, "max_calls_per_minute", 2):
        await guest.hub_call(entity_id="light.bed", action="turn_on")
        await guest.hub_call(entity_id="light.bed", action="turn_off")
        await guest.refused("rate_limited", entity_id="light.bed", action="turn_on")


async def test_hub_call_over_rest_with_the_device_token(
    guest: Guest,
    hass: HomeAssistant,
    hass_client_no_auth: Any,
    mock_api: aioresponses,
) -> None:
    """The hub's real call: REST with the device's own token, read-only user."""
    mock_api.passthrough_unmatched = True  # the HTTP test server
    assert await async_setup_component(hass, "api", {})
    await guest.apply()
    record = guest.env.entry.runtime_data.device_users.records[GUEST_ID]
    token = hass.auth.async_create_access_token(
        guest.user.refresh_tokens[record["refresh_token_id"]]
    )
    client = await hass_client_no_auth()
    headers = {"Authorization": f"Bearer {token}"}
    # Direct entity actions are refused for the read-only user.
    resp = await client.post(
        "/api/services/input_boolean/turn_on",
        json={"entity_id": "input_boolean.a"},
        headers=headers,
    )
    assert resp.status == 401
    assert hass.states.get("input_boolean.a").state == "off"
    with (
        patch(
            "custom_components.halumar.control.is_cloud_connection",
            return_value=False,
        ),
        patch("custom_components.halumar.control.current_request") as cur,
    ):
        cur.get.return_value = SimpleNamespace(remote=APP_NETWORK)
        resp = await client.post(
            f"/api/services/{DOMAIN}/control?return_response",
            json={"entity_id": "light.bed", "action": "turn_on"},
            headers=headers,
        )
        assert resp.status == 200, await resp.text()
        body = await resp.json()
        assert body["service_response"]["route"] == "local"
        assert body["service_response"]["results"][0]["executed"] is True
        before = len(guest.env.calls)
        resp = await client.post(
            f"/api/services/{DOMAIN}/control?return_response",
            json={"entity_id": "light.not_listed", "action": "turn_on"},
            headers=headers,
        )
        # Refused (Home Assistant versions differ in the status code).
        assert resp.status >= 400
        assert len(guest.env.calls) == before


# ── (c) a grant never loosens anything ───────────────────────────────────


async def test_grant_of_a_device_without_kids_rules_is_ignored(
    guest: Guest, mock_api: aioresponses
) -> None:
    kids = _kids_calls(mock_api)
    grant = "g" * 32
    await guest.apply()
    await guest.refused(
        "blocked_area", entity_id="light.not_listed", action="turn_on", grant=grant
    )
    await guest.refused(
        "blocked_view_only", entity_id="cover.garage", action="open_cover", grant=grant
    )
    await guest.refused(
        "blocked_remote",
        remote="203.0.113.7",
        entity_id="light.bed",
        action="turn_on",
        grant=grant,
    )
    await guest.refused(
        "blocked_group_member", entity_id="scene.outside", action="turn_on", grant=grant
    )
    response = await guest.hub_call(
        entity_id="light.bed", action="turn_on", grant=grant
    )
    assert response["results"][0]["executed"] is True
    assert kids == []  # nobody asked, nothing counted, the grant went nowhere


async def test_grant_does_not_lift_a_childs_local_rules(
    guest: Guest, mock_api: aioresponses
) -> None:
    """Blocked types and bedtime are decided before the hub is asked."""
    kids = _kids_calls(mock_api)
    entities = _guest_entities()
    k_entities = {eid: kids_rules() for eid in entities}
    k_entities["lock.front"] = kids_rules(blocked=True)
    k_entities["light.bed"] = kids_rules(quiet=ALL_DAY, rate=True)
    snap = make_snapshot(
        GUEST_ID,
        guest.user.id,
        entities,
        kids={
            "time_zone": "Europe/Zurich",
            "override_until": None,
            "entities": k_entities,
        },
    )
    await guest.apply(snap)
    grant = "g" * 32
    await guest.refused(
        "blocked_kids", entity_id="lock.front", action="lock", grant=grant
    )
    await guest.refused(
        "blocked_kids_bedtime", entity_id="light.bed", action="turn_on", grant=grant
    )
    assert kids == []


# ── (d) kids check only with kids rules ──────────────────────────────────


async def test_kids_check_only_for_snapshots_with_kids_rules(
    guest: Guest, mock_api: aioresponses
) -> None:
    kids = _kids_calls(mock_api)
    await guest.apply()
    for _ in range(3):
        await guest.hub_call(entity_id="light.ceiling", action="toggle", grant="g" * 20)
    assert kids == []
    entities = _guest_entities()
    k_entities = {eid: kids_rules() for eid in entities}
    k_entities["light.ceiling"] = kids_rules(rate=True)
    await guest.apply(
        make_snapshot(
            GUEST_ID,
            guest.user.id,
            entities,
            kids={
                "time_zone": "Europe/Zurich",
                "override_until": None,
                "entities": k_entities,
            },
        )
    )
    await guest.hub_call(entity_id="light.ceiling", action="toggle", grant="g" * 20)
    assert kids == [
        {
            "client_id": GUEST_ID,
            "entity_id": "light.ceiling",
            "action": "toggle",
            "grant": "g" * 20,
        }
    ]


# ── (e) withdrawing the key ──────────────────────────────────────────────


async def test_rotation_ends_the_token_the_device_held(
    guest: Guest,
    hass: HomeAssistant,
    hass_client_no_auth: Any,
    mock_api: aioresponses,
) -> None:
    """Checked → Hub only: the hub rotates; the old token stops, the user stays."""
    mock_api.passthrough_unmatched = True  # the HTTP test server
    assert await async_setup_component(hass, "api", {})
    await guest.apply()
    hub = guest.env.hub
    held = hub.deliveries[-1]["token"]  # what the app got on Checked
    (old,) = guest.user.refresh_tokens.values()
    assert hass.auth.async_validate_access_token(held) is old

    device_users = guest.env.entry.runtime_data.device_users
    await device_users.async_handle_push(
        {
            "type": "provision_client",
            "data": _request(
                hub.client(GUEST_ID), token_action="rotate", reason="rotate"
            ),
        }
    )
    await _settle(hass)

    user = await _user_of(hass, guest.env.entry, GUEST_ID)
    assert user.id == guest.user.id
    assert [g.id for g in user.groups] == [GROUP_ID_READ_ONLY]
    assert user.local_only is True
    (new,) = user.refresh_tokens.values()
    assert new.id != old.id
    assert hass.auth.async_get_refresh_token(old.id) is None
    assert hass.auth.async_validate_access_token(held) is None
    assert hub.deliveries[-1]["refresh_token_id"] == new.id  # only the hub has it
    client = await hass_client_no_auth()
    resp = await client.get("/api/", headers={"Authorization": f"Bearer {held}"})
    assert resp.status == 401
    # The hub keeps switching for the device with the new token.
    response = await guest.hub_call(entity_id="light.bed", action="turn_on")
    assert response["results"][0]["executed"] is True


async def test_remote_access_ending_rotates_and_makes_the_user_local_only(
    scenes: Env,  # noqa: F811 - fixture
    hass: HomeAssistant,
) -> None:
    """Hub only with remote access (read-only key) → remote access ends."""
    user = await _provision_guest(scenes, remote_allowed=True)
    guest = Guest(scenes, user)
    await guest.apply(remote_access=True)
    assert user.local_only is False
    held = scenes.hub.deliveries[-1]["token"]
    (old,) = user.refresh_tokens.values()

    identity = _request(scenes.hub.client(GUEST_ID), remote_allowed=False)
    for key in ("token_action", "reason", "request_id"):
        identity.pop(key)
    await scenes.entry.runtime_data.device_users.async_handle_push(
        {"type": "update_client_identity", "data": identity}
    )
    await guest.apply()
    await _settle(hass)

    assert await hass.auth.async_get_user(user.id) is user
    assert user.local_only is True
    assert [g.id for g in user.groups] == [GROUP_ID_READ_ONLY]
    (new,) = user.refresh_tokens.values()
    assert new.id != old.id
    assert hass.auth.async_validate_access_token(held) is None
    assert scenes.hub.deliveries[-1]["refresh_token_id"] == new.id
