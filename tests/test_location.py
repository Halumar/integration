"""Location (contracts docs/location.md).

halumar.report_location for devices with a Home Assistant key (also
read-only device users), client_location from the hub for devices with
parental controls, the device_tracker and the metadata to the hub.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from aioresponses import CallbackResult, aioresponses
from homeassistant.core import Context, HomeAssistant, State
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from yarl import URL

from custom_components.halumar.const import DOMAIN
from custom_components.halumar.location import async_handle_push

from ._ha import MockUser, mock_restore_cache
from .common import BASE_URL, CLIENT_ID
from .test_device_users import _settle
from . import test_enforce
from .test_enforce import ENTITIES, Env, make_snapshot

# Fixtures of the enforcement tests reused here.
env = test_enforce.env
existing_owner = test_enforce.existing_owner

STATUS_URL = f"{BASE_URL}/api/integration/location-status"


def _fix(**extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "latitude": 47.3769,
        "longitude": 8.5417,
        "accuracy": 12.0,
        "altitude": 408.0,
        "speed": 1.5,
        "course": 90.0,
        "battery_level": 80,
        "background": False,
        "ts": dt_util.utcnow().isoformat(),
    }
    body.update(extra)
    return body


def _tracker_id(hass: HomeAssistant) -> str:
    registry = er.async_get(hass)
    for entry in registry.entities.values():
        if entry.domain == "device_tracker" and entry.unique_id.endswith(
            f"_client_{CLIENT_ID}_tracker"
        ):
            return entry.entity_id
    raise AssertionError("no tracker")


async def _report(env: Env, user_id: str | None = "device", **data: Any) -> Any:
    uid = env.user.id if user_id == "device" else user_id
    result = await env.hass.services.async_call(
        DOMAIN,
        "report_location",
        _fix(**data),
        blocking=True,
        context=Context(user_id=uid),
        return_response=True,
    )
    await _settle(env.hass)
    return result


async def _denied(env: Env, reason: str, **data: Any) -> None:
    with pytest.raises(ServiceValidationError) as err:
        await _report(env, **data)
    assert err.value.translation_key == reason


@pytest.fixture
def statuses(mock_api: aioresponses) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []

    def record(url: URL, **kwargs: Any) -> CallbackResult:
        bodies.append(kwargs["json"])
        return CallbackResult(
            status=200,
            payload={
                "client_id": CLIENT_ID,
                "location_shared": "background",
                "location_at": kwargs["json"]["ts"],
            },
        )

    mock_api.post(STATUS_URL, callback=record, repeat=True)
    return bodies


async def test_read_only_device_reports_its_location(
    env: Env, hass: HomeAssistant, statuses: list[dict[str, Any]]
) -> None:
    """Control path checked (read-only user): the action works, no 401."""
    await env.apply(location="background")
    assert env.manager.desired_group_id(CLIENT_ID) == "system-read-only"
    result = await _report(env, background=True, location_shared="background")
    assert result == {"applied": True}
    state = hass.states.get(_tracker_id(hass))
    assert state is not None
    assert state.attributes["latitude"] == 47.3769
    assert state.attributes["longitude"] == 8.5417
    assert state.attributes["gps_accuracy"] == 12.0
    assert state.attributes["altitude"] == 408.0
    assert state.attributes["speed"] == 1.5
    # Only metadata reached the hub.
    assert statuses == [
        {
            "client_id": CLIENT_ID,
            "background": True,
            "ts": statuses[0]["ts"],
            "location_shared": "background",
            "battery_level": 80,
        }
    ]


async def test_rule_age_rate_and_order(
    env: Env, hass: HomeAssistant, statuses: list[dict[str, Any]]
) -> None:
    await env.apply(location="foreground")
    await _denied(env, "location_background_not_allowed", background=True)
    old = (dt_util.utcnow() - timedelta(minutes=16)).isoformat()
    await _denied(env, "location_stale", ts=old)
    future = (dt_util.utcnow() + timedelta(minutes=6)).isoformat()
    await _denied(env, "location_stale", ts=future)
    assert await _report(env) == {"applied": True}
    await _denied(env, "rate_limited")
    state = hass.data["halumar_location"]
    state.accepted_at[CLIENT_ID] -= 10
    earlier = (dt_util.utcnow() - timedelta(minutes=1)).isoformat()
    assert await _report(env, ts=earlier) == {"applied": False}
    assert len(statuses) == 1


async def test_location_off_and_callers(
    env: Env, hass: HomeAssistant, statuses: list[dict[str, Any]]
) -> None:
    await env.apply(location="off")
    await _denied(env, "location_not_allowed")
    other = MockUser(name="Family member").add_to_hass(hass)
    with pytest.raises(ServiceValidationError) as err:
        await _report(env, user_id=other.id)
    assert err.value.translation_key == "not_a_device"
    with pytest.raises(ServiceValidationError) as err:
        await _report(env, user_id=None)
    assert err.value.translation_key == "not_a_device"
    assert statuses == []


async def test_invalid_fields(env: Env) -> None:
    await env.apply(location="background")
    import voluptuous

    for bad in ({"latitude": 91}, {"accuracy": -1}, {"ts": "2026-11-21T18:00:00"}):
        with pytest.raises((voluptuous.Invalid, ServiceValidationError)):
            await _report(env, **bad)


async def test_hub_push_for_parental_controls(env: Env, hass: HomeAssistant) -> None:
    await env.apply(location="foreground")
    fix = {
        "latitude": 46.9481,
        "longitude": 7.4474,
        "accuracy": 30.0,
        "altitude": None,
        "vertical_accuracy": None,
        "speed": None,
        "course": None,
        "ts": dt_util.utcnow().isoformat(),
    }
    message = {
        "type": "client_location",
        "data": {"client_id": CLIENT_ID, "location": fix},
    }
    assert async_handle_push(hass, message)
    await hass.async_block_till_done()
    state = hass.states.get(_tracker_id(hass))
    assert state is not None
    assert state.attributes["latitude"] == 46.9481
    # Malformed or other types are ignored; another message type is not ours.
    assert async_handle_push(
        hass, {"type": "client_location", "data": {"client_id": "x"}}
    )
    assert not async_handle_push(hass, {"type": "ha_event", "data": {}})
    # Rule off: the snapshot clears the tracker, a later push is ignored.
    await env.apply(location="off")
    state = hass.states.get(_tracker_id(hass))
    assert state is not None and "latitude" not in state.attributes
    fix["ts"] = dt_util.utcnow().isoformat()
    assert async_handle_push(hass, message)
    await hass.async_block_till_done()
    state = hass.states.get(_tracker_id(hass))
    assert state is not None and "latitude" not in state.attributes


async def test_tracker_restores_the_last_fix(
    hass: HomeAssistant,
    mock_api: aioresponses,
    existing_owner: MockUser,  # noqa: F811
) -> None:
    from .test_device_users import _provisioned, _user_of

    entity_id = "device_tracker.halumar_iphone_location"
    entry, _hub = await _provisioned(hass, mock_api, CLIENT_ID)
    user = await _user_of(hass, entry, CLIENT_ID)
    assert _tracker_id(hass) == entity_id
    last = State(
        entity_id,
        "not_home",
        {"latitude": 47.0, "longitude": 8.0, "gps_accuracy": 20, "speed": 2.0},
    )
    # Without a snapshot that allows the location nothing is restored.
    await hass.config_entries.async_unload(entry.entry_id)
    mock_restore_cache(hass, [last])
    await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass)
    state = hass.states.get(entity_id)
    assert state is not None and "latitude" not in state.attributes
    snap = make_snapshot(CLIENT_ID, user.id, ENTITIES, location="background")
    await hass.services.async_call(
        DOMAIN,
        "apply_rules",
        {"snapshots": [snap]},
        blocking=True,
        return_response=True,
    )
    await hass.config_entries.async_unload(entry.entry_id)
    mock_restore_cache(hass, [last])
    await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass)
    state = hass.states.get(_tracker_id(hass))
    assert state is not None
    assert state.attributes["latitude"] == 47.0
    assert state.attributes["gps_accuracy"] == 20
    assert state.attributes["speed"] == 2.0


async def test_push_edges(env: Env, hass: HomeAssistant) -> None:
    """Malformed fixes, older fixes and a missing manager are ignored."""
    await env.apply(location="background")
    tracker = _tracker_id(hass)
    good = {
        "latitude": 46.9,
        "longitude": 7.4,
        "accuracy": 30.0,
        "ts": dt_util.utcnow().isoformat(),
    }
    bad_fixes = [
        "not a dict",
        {**good, "battery_level": 50},
        {k: v for k, v in good.items() if k != "latitude"},
        {**good, "speed": "fast"},
        {**good, "course": 400},
        {**good, "ts": "2026-11-21T18:00:00"},
        {**good, "ts": None},
    ]
    for fix in bad_fixes:
        message = {
            "type": "client_location",
            "data": {"client_id": CLIENT_ID, "location": fix},
        }
        assert async_handle_push(hass, message)
    assert async_handle_push(hass, {"type": "client_location", "data": "x"})
    await hass.async_block_till_done()
    state = hass.states.get(tracker)
    assert state is not None and "latitude" not in state.attributes
    message = {
        "type": "client_location",
        "data": {"client_id": CLIENT_ID, "location": good},
    }
    assert async_handle_push(hass, message)
    older = {
        **good,
        "latitude": 1.0,
        "ts": (dt_util.utcnow() - timedelta(minutes=1)).isoformat(),
    }
    message = {
        "type": "client_location",
        "data": {"client_id": CLIENT_ID, "location": older},
    }
    assert async_handle_push(hass, message)
    await hass.async_block_till_done()
    state = hass.states.get(tracker)
    assert state is not None and state.attributes["latitude"] == 46.9


async def test_hub_unreachable_or_entry_unloaded(
    env: Env, hass: HomeAssistant, mock_api: aioresponses
) -> None:
    """The metadata is best effort: an error or no loaded entry changes nothing."""
    await env.apply(location="background")
    mock_api.post(STATUS_URL, status=500)
    assert await _report(env) == {"applied": True}
    await hass.config_entries.async_unload(env.entry.entry_id)
    hass.data["halumar_location"].accepted_at.clear()
    later = (dt_util.utcnow() + timedelta(seconds=1)).isoformat()
    assert await _report(env, ts=later) == {"applied": True}


async def test_numbers_must_be_finite(env: Env) -> None:
    await env.apply(location="background")
    import voluptuous

    for bad in ({"latitude": True}, {"speed": float("inf")}):
        with pytest.raises((voluptuous.Invalid, ServiceValidationError)):
            await _report(env, **bad)
