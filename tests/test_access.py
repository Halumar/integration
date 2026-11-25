"""Access hours and access switch.

The snapshot field ``access`` (switch, hours, windows, time zone) makes
halumar.control refuse everything of a device outside its access, with the
integration's own clock; the access switch ``switch.<ns>_<device>_access``
exists for the devices the hub names, is operated only by admins and
automations and goes to the hub; hub events carry ``access_reason``.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any

import pytest
from aioresponses import aioresponses
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from yarl import URL

from custom_components.halumar.brand import BRAND_NAME
from custom_components.halumar.const import DOMAIN
from custom_components.halumar.enforcement_sync import async_handle_push
from custom_components.halumar.snapshot import (
    SnapshotError,
    access_closed,
    next_start,
    validate_snapshot,
)
from custom_components.halumar.switch import has_access_switch

from ._ha import MockConfigEntry, MockUser, async_capture_events
from .common import (
    BASE_URL,
    CLIENT_ID,
    STATUS_URL,
    make_status_payload,
    setup_integration,
)
from . import test_enforce
from .test_enforce import Env, make_snapshot

# Fixtures of the enforcement tests reused here.
env = test_enforce.env
existing_owner = test_enforce.existing_owner

ACCESS_URL = f"{BASE_URL}/api/integration/access-switch"
SWITCH = "switch.halumar_wall_tablet_access"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
OPEN_ACCESS = {
    "open": True,
    "reason": None,
    "until": None,
    "opens_at": None,
    "schedule": False,
    "switch": True,
}


def _today() -> str:
    return DAYS[dt_util.utcnow().weekday()]


def _other_day() -> str:
    return DAYS[(dt_util.utcnow().weekday() + 3) % 7]


def access(
    *, switch: bool = True, windows: list | None = None, early_until: str | None = None
) -> dict[str, Any]:
    return {
        "switch": switch,
        "hours": windows is not None,
        "windows": windows or [],
        "time_zone": None,
        "early_until": early_until,
    }


# ── snapshot ────────────────────────────────────────────────────────────────


def test_snapshot_access_shape() -> None:
    good = make_snapshot(
        CLIENT_ID,
        "a" * 32,
        {},
        access=access(windows=[{"days": ["fri"], "start": "17:00", "end": "01:00"}]),
    )
    assert validate_snapshot(good)["access"]["hours"] is True
    assert validate_snapshot(make_snapshot(CLIENT_ID, "a" * 32, {}))["access"] is None
    for bad in (
        {"switch": True},
        {**access(), "switch": "on"},
        {**access(), "windows": [{"days": ["fri"], "start": "17:00", "end": "01:00"}]},
        {**access(windows=[{"days": ["xyz"], "start": "17:00", "end": "01:00"}])},
        {**access(windows=[]), "time_zone": 5},
    ):
        with pytest.raises(SnapshotError):
            validate_snapshot(make_snapshot(CLIENT_ID, "a" * 32, {}, access=bad))
    missing = make_snapshot(CLIENT_ID, "a" * 32, {})
    del missing["access"]
    with pytest.raises(SnapshotError):
        validate_snapshot(missing)


def test_access_closed_and_next_start() -> None:
    fri_noon = dt_util.parse_datetime("2026-11-20T11:00:00+00:00")
    evening = [{"days": DAYS, "start": "17:00", "end": "01:00"}]
    zurich = {**access(windows=evening), "time_zone": "Europe/Zurich"}
    assert access_closed(None, fri_noon) == (False, None)
    assert access_closed(access(switch=False), fri_noon) == (True, None)
    assert access_closed(access(), fri_noon) == (False, None)
    closed, opens = access_closed(zurich, fri_noon)
    assert closed and opens == dt_util.parse_datetime("2026-11-20T16:00:00+00:00")
    late = dt_util.parse_datetime("2026-11-20T23:30:00+00:00")
    assert access_closed(zurich, late) == (False, None)
    assert access_closed({**access(windows=[]), "hours": True}, fri_noon) == (
        True,
        None,
    )
    assert next_start(evening, late, "Europe/Zurich") is None
    assert next_start([], fri_noon, None) is None


# ── halumar.control ─────────────────────────────────────────────────────────


async def test_control_refused_while_closed(env: Env, hass: HomeAssistant) -> None:
    events = async_capture_events(hass, f"{DOMAIN}_event")
    await env.apply(access=access(switch=False))
    await env.denied(
        "blocked_access_closed", entity_id="light.ceiling", action="turn_on"
    )
    answer = await env.control(
        entity_ids=["light.ceiling", "light.bed"], action="turn_off"
    )
    assert {r["reason"] for r in answer["results"]} == {"blocked_access_closed"}
    assert not env.calls
    assert "denied" in [e.data["kind"] for e in events]

    # schedule: a window on another day -> closed until then; today -> open
    await env.apply(
        access=access(
            windows=[{"days": [_other_day()], "start": "08:00", "end": "09:00"}]
        )
    )
    answer = await env.control(entity_ids=["light.ceiling"], action="turn_on")
    (result,) = answer["results"]
    assert result["reason"] == "blocked_access_closed" and result["until"]
    await env.apply(
        access=access(windows=[{"days": [_today()], "start": "00:00", "end": "00:00"}])
    )
    await env.control(entity_id="light.ceiling", action="turn_on")
    assert env.calls
    # check_only says the same without executing
    await env.apply(access=access(switch=False))
    answer = await env.control(
        entity_ids=["light.ceiling"], action="turn_on", check_only=True
    )
    assert answer["results"][0]["reason"] == "blocked_access_closed"


async def test_system_calls_ignore_device_access(env: Env) -> None:
    await env.apply(access=access(switch=False))
    await env.control(user_id=None, entity_id="light.ceiling", action="turn_on")
    assert env.calls


# ── access switch entity ────────────────────────────────────────────────────


def access_payload(**access_now: Any) -> dict[str, Any]:
    payload = make_status_payload()
    payload["clients"][0]["access"] = {**OPEN_ACCESS, **access_now}
    payload["clients"][1]["access"] = {**OPEN_ACCESS, "switch": None}
    return payload


def test_has_access_switch() -> None:
    assert has_access_switch({"access": OPEN_ACCESS})
    assert not has_access_switch({"access": {**OPEN_ACCESS, "switch": None}})
    assert not has_access_switch({"access": None})
    assert not has_access_switch({})
    assert not has_access_switch({"access": OPEN_ACCESS, "pending_deletion": True})


@pytest.mark.parametrize("as_admin", [True, False])
async def test_switch_is_created_and_goes_to_the_hub(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    as_admin: bool,
) -> None:
    await setup_integration(
        hass, mock_api, config_entry, access_payload(), repeat=False
    )
    state = hass.states.get(SWITCH)
    assert state is not None and state.state == "on"
    assert state.attributes["open"] is True
    assert state.attributes["friendly_name"].endswith("Access")
    assert not [
        e
        for e in er.async_get(hass).entities.values()
        if e.domain == "switch"
        and e.unique_id.endswith("_access")
        and "guest" in e.entity_id
    ]
    closed = {**OPEN_ACCESS, "open": False, "reason": "switch", "switch": False}
    mock_api.post(
        ACCESS_URL, payload={"client_id": CLIENT_ID, "access": closed}, repeat=True
    )
    mock_api.get(STATUS_URL, payload=access_payload(**closed), repeat=True)
    context = None
    if as_admin:
        admin = MockUser(groups=None, is_owner=True, name="Admin").add_to_hass(hass)
        context = Context(user_id=admin.id)
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": SWITCH}, blocking=True, context=context
    )
    body = mock_api.requests[("POST", URL(ACCESS_URL))][-1].kwargs["json"]
    assert body == {
        "client_id": CLIENT_ID,
        "on": False,
        "user": "Admin" if as_admin else None,
    }
    await hass.async_block_till_done()
    state = hass.states.get(SWITCH)
    assert state.state == "off" and state.attributes["reason"] == "switch"


async def test_non_admins_never_switch_access(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry, access_payload())
    user = MockUser().add_to_hass(hass)
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            "switch",
            "turn_on",
            {"entity_id": SWITCH},
            blocking=True,
            context=Context(user_id=user.id),
        )
    assert ("POST", URL(ACCESS_URL)) not in mock_api.requests


async def test_switch_errors_are_translated(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry, access_payload())
    mock_api.post(ACCESS_URL, status=409, payload={"detail": "no_access_switch"})
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": SWITCH}, blocking=True
        )
    assert err.value.translation_key == "access_switch_none"


async def test_switch_follows_the_hub(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(
        hass, mock_api, config_entry, access_payload(), repeat=False
    )
    assert hass.states.get(SWITCH) is not None
    coordinator = config_entry.runtime_data
    payload = access_payload()
    payload["clients"][0]["access"] = {**OPEN_ACCESS, "switch": None}
    mock_api.get(STATUS_URL, payload=payload)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert er.async_get(hass).async_get(SWITCH) is None
    # comes back when the hub names it again
    mock_api.get(STATUS_URL, payload=access_payload(), repeat=True)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(SWITCH) is not None


async def test_switch_is_named_after_the_owner_and_follows_renames(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Name "Halumar: Mum iPhone17Pro Access", not only the model, and a
    rename on the hub reaches the display name (entity id and a name given in
    Home Assistant stay)."""
    payload = access_payload()
    payload["clients"][0].update(name="iPhone17Pro", owner_label="Mum")
    await setup_integration(hass, mock_api, config_entry, payload, repeat=False)
    switch = "switch.halumar_iphone17pro_access"
    state = hass.states.get(switch)
    assert state is not None
    assert state.attributes["friendly_name"] == f"{BRAND_NAME}: Mum iPhone17Pro Access"

    coordinator = config_entry.runtime_data
    renamed = access_payload()
    renamed["clients"][0].update(name="Mum", owner_label="Mum")
    mock_api.get(STATUS_URL, payload=renamed)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(switch).attributes["friendly_name"].rsplit(" ", 1)[0] == (
        f"{BRAND_NAME}: Mum"
    )
    entry = er.async_get(hass).async_get(switch)
    assert entry is not None and entry.entity_id == switch

    device_registry = dr.async_get(hass)
    device = device_registry.async_get(entry.device_id)
    assert device is not None
    device_registry.async_update_device(device.id, name_by_user="Babysitter")
    again = access_payload()
    again["clients"][0].update(name="Mum Phone", owner_label="Mum")
    mock_api.get(STATUS_URL, payload=again, repeat=True)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    device = device_registry.async_get(entry.device_id)
    assert device.name == f"{BRAND_NAME}: Mum Phone"
    assert device.name_by_user == "Babysitter"
    assert hass.states.get(switch).attributes["friendly_name"].rsplit(" ", 1)[0] == (
        "Babysitter"
    )


async def test_registered_switch_keeps_its_entity_id(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Only the suggested entity id ends in _access: a switch already in the
    entity registry keeps its entity id, and the unique id stays
    <entry_id>_client_<client_id>_access."""
    unique_id = f"{config_entry.entry_id}_client_{CLIENT_ID}_access"
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "switch",
        DOMAIN,
        unique_id,
        suggested_object_id="halumar_wall_tablet_zugang",
        config_entry=config_entry,
    )
    await setup_integration(
        hass, mock_api, config_entry, access_payload(), repeat=False
    )
    entity_id = registry.async_get_entity_id("switch", DOMAIN, unique_id)
    assert entity_id == "switch.halumar_wall_tablet_zugang"
    assert hass.states.get(entity_id) is not None
    assert hass.states.get(SWITCH) is None


async def test_new_switch_unique_id(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """A new switch gets the English entity id and the same unique id."""
    await setup_integration(
        hass, mock_api, config_entry, access_payload(), repeat=False
    )
    entry = er.async_get(hass).async_get(SWITCH)
    assert entry is not None
    assert entry.unique_id == f"{config_entry.entry_id}_client_{CLIENT_ID}_access"


# ── hub events ──────────────────────────────────────────────────────────────


async def test_access_events_carry_the_reason(env: Env, hass: HomeAssistant) -> None:
    events = async_capture_events(hass, f"{DOMAIN}_event")
    data = {
        "kind": "access_closed",
        "category": "devices",
        "at": "2026-11-21T00:00:00+00:00",
        "client_id": CLIENT_ID,
        "device_name": "iPhone Kind",
        "access_reason": "schedule",
        "until": (dt_util.utcnow() + timedelta(hours=16)).isoformat(),
    }
    assert async_handle_push(
        hass, None, {"type": "ha_event", "data": copy.deepcopy(data)}
    )
    assert events[-1].data["access_reason"] == "schedule"
    assert events[-1].data["kind"] == "access_closed"


# ── early access ─────────────────────────────────────────────────────


def test_snapshot_early_until_shape() -> None:
    windows = [{"days": ["fri"], "start": "17:00", "end": "01:00"}]
    good = make_snapshot(
        CLIENT_ID,
        "a" * 32,
        {},
        access=access(windows=windows, early_until="2026-11-20T15:20:00Z"),
    )
    assert validate_snapshot(good)["access"]["early_until"] == "2026-11-20T15:20:00Z"
    for bad in ("tomorrow", 5, "x" * 41):
        with pytest.raises(SnapshotError):
            validate_snapshot(
                make_snapshot(
                    CLIENT_ID,
                    "a" * 32,
                    {},
                    access={**access(windows=windows), "early_until": bad},
                )
            )
    missing = access(windows=windows)
    del missing["early_until"]
    with pytest.raises(SnapshotError):
        validate_snapshot(make_snapshot(CLIENT_ID, "a" * 32, {}, access=missing))


def test_access_closed_honours_an_early_access() -> None:
    fri_noon = dt_util.parse_datetime("2026-11-20T11:00:00+00:00")
    evening = [{"days": DAYS, "start": "17:00", "end": "01:00"}]
    zurich = {**access(windows=evening), "time_zone": "Europe/Zurich"}
    assert access_closed(zurich, fri_noon)[0] is True
    early = {**zurich, "early_until": "2026-11-20T12:00:00Z"}
    assert access_closed(early, fri_noon) == (False, None)
    # after it ended: closed again until the window
    closed, opens = access_closed(early, fri_noon + timedelta(hours=1, minutes=1))
    assert closed is True and opens is not None
    # the switch still closes it
    assert access_closed({**early, "switch": False}, fri_noon) == (True, None)


async def test_control_allowed_during_early_access(
    env: Env, hass: HomeAssistant
) -> None:
    other = [{"days": [_other_day()], "start": "08:00", "end": "09:00"}]
    await env.apply(access=access(windows=other))
    await env.denied(
        "blocked_access_closed", entity_id="light.ceiling", action="turn_on"
    )
    soon = (dt_util.utcnow() + timedelta(minutes=30)).isoformat()
    await env.apply(access=access(windows=other, early_until=soon))
    await env.control(entity_id="light.ceiling", action="turn_on")
    assert env.calls
    past = (dt_util.utcnow() - timedelta(minutes=1)).isoformat()
    await env.apply(access=access(windows=other, early_until=past))
    await env.denied(
        "blocked_access_closed", entity_id="light.ceiling", action="turn_off"
    )


async def test_switch_shows_the_early_access(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    early = {
        **OPEN_ACCESS,
        "schedule": True,
        "until": "2026-11-20T15:20:00Z",
        "early": {
            "until": "2026-11-20T15:20:00Z",
            "by": {"kind": "device", "client_id": None, "name": "Papa"},
            "request_id": None,
            "at": "2026-11-20T14:20:00Z",
        },
    }
    await setup_integration(hass, mock_api, config_entry, access_payload(**early))
    state = hass.states.get(SWITCH)
    assert state.attributes["early_until"] == "2026-11-20T15:20:00Z"
