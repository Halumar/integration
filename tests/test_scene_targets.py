"""Scenes and scripts in halumar.control: their targets.

A scene or script switches other entities as the system. It runs for a
device only when, besides its own release, every target is allowed for the
device by the rules of the target's own domain: areas, route, quick access,
allowlist, own entities, nested scenes and scripts, and the kids rules
(contracts docs/enforcement.md, section 2). Real Home Assistant scenes and scripts,
so the targets come from their configuration as on a live system.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.halumar.allowlist import Allowlist
from custom_components.halumar.const import DOMAIN

from .common import CLIENT_ID
from .test_enforce import (  # noqa: F401 - fixtures
    ALL_DAY,
    Env,
    access,
    env,
    existing_owner,
    kids_rules,
    make_snapshot,
)

# A typical scene: two lights of the living room, nothing of another domain.
FILMABEND = {
    "id": "filmabend",
    "name": "Filmabend",
    "entities": {
        "light.bed": {
            "state": "on",
            "brightness": 120,
            "color_mode": "color_temp",
            "color_temp_kelvin": 2700,
        },
        "light.ceiling": {"state": "on", "brightness": 60},
    },
}

SCENES: list[dict[str, Any]] = [
    FILMABEND,
    {
        "name": "Outside",
        "entities": {"light.bed": "on", "light.not_listed": "on"},
    },
    {
        "name": "Leave",
        "entities": {"light.ceiling": "off", "lock.front": "locked"},
    },
    {
        "name": "Arrive",
        "entities": {"light.ceiling": "on", "lock.front": "unlocked"},
    },
    {
        "name": "Sensors too",
        "entities": {
            "light.ceiling": "on",
            "sensor.power": "12",
            "binary_sensor.motion": "off",
            "light.gone": "on",
        },
    },
    {"name": "Counter", "entities": {"light.ceiling": "on", "counter.visits": "3"}},
    {"name": "Night", "entities": {"light.bed": "off", "media_player.tv": "off"}},
    {
        "name": "Party",
        "entities": {
            "light.bed": {"state": "on", "color_mode": "hs", "hs_color": [300, 80]},
            "media_player.tv": {"state": "playing", "volume_level": 0.9},
        },
    },
    {
        "name": "Plain colour",
        "entities": {"light.bed": {"state": "on", "rgb_color": [255, 0, 0]}},
    },
    {
        "name": "Effect",
        "entities": {"light.bed": {"state": "on", "effect": "rainbow"}},
    },
    {
        "name": "Calm",
        "entities": {
            "light.bed": {"state": "on", "color_mode": "color_temp", "effect": "none"},
            "media_player.tv": {"state": "playing", "volume_level": 0.3},
        },
    },
]

SCRIPTS: dict[str, Any] = {
    "evening": {
        "sequence": [
            {"condition": "state", "entity_id": "sun.sun", "state": "below_horizon"},
            {"action": "light.turn_on", "target": {"entity_id": "light.ceiling"}},
            {"scene": "scene.filmabend"},
        ]
    },
    "outside": {
        "sequence": [
            {"action": "light.turn_on", "target": {"entity_id": "light.not_listed"}}
        ]
    },
    "by_device": {
        "sequence": [
            {"action": "light.turn_on", "target": {"device_id": "abc123"}},
        ]
    },
    "unlock": {
        "sequence": [
            {"action": "lock.unlock", "target": {"entity_id": "lock.front"}},
        ]
    },
    "loop_a": {
        "sequence": [
            {"action": "script.turn_on", "target": {"entity_id": "script.loop_b"}}
        ]
    },
    "loop_b": {
        "sequence": [
            {"action": "script.turn_on", "target": {"entity_id": "script.loop_a"}}
        ]
    },
    "deep_1": {
        "sequence": [
            {"action": "script.turn_on", "target": {"entity_id": "script.deep_2"}}
        ]
    },
    "deep_2": {
        "sequence": [
            {"action": "script.turn_on", "target": {"entity_id": "script.deep_3"}}
        ]
    },
    "deep_3": {
        "sequence": [
            {"action": "script.turn_on", "target": {"entity_id": "script.deep_4"}}
        ]
    },
    "deep_4": {
        "sequence": [
            {"action": "light.turn_on", "target": {"entity_id": "light.ceiling"}}
        ]
    },
}

RELEASED = [
    "scene.filmabend",
    "scene.outside",
    "scene.leave",
    "scene.arrive",
    "scene.sensors_too",
    "scene.counter",
    "scene.night",
    "scene.party",
    "scene.effect",
    "scene.plain_colour",
    "script.ghost",
    "scene.calm",
    "scene.hue_relax",
    "scene.listed_members",
    *(f"script.{name}" for name in SCRIPTS),
]


def _entities(**overrides: dict[str, Any]) -> dict[str, Any]:
    entities: dict[str, Any] = {
        "light.bed": access(remote="control", quick="control"),
        "light.ceiling": access(remote="control", quick="control"),
        "lock.front": access(remote="view", quick="status"),
        "media_player.tv": access(),
        "counter.visits": access(),
        **{
            eid: access(remote="control", quick="control", run=True) for eid in RELEASED
        },
    }
    entities.update(overrides)
    return entities


def _kids(
    entities: dict[str, Any],
    *,
    override: dict[str, dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    rules = {eid: kids_rules() for eid in entities}
    rules["lock.front"] = kids_rules(blocked=True)  # blocked device type
    rules.update(override or {})
    return {
        "time_zone": "Europe/Zurich",
        "override_until": None,
        "entities": rules,
        **extra,
    }


@pytest.fixture
async def scenes(env: Env, hass: HomeAssistant) -> Env:  # noqa: F811 - fixture
    """The enforcement env with real scenes and scripts."""
    for entity_id, state in {
        "light.bed": "off",
        "light.ceiling": "off",
        "light.not_listed": "off",
        "lock.front": "locked",
        "media_player.tv": "off",
        "counter.visits": "0",
        "sensor.power": "10",
        "binary_sensor.motion": "off",
        "sun.sun": "below_horizon",
    }.items():
        hass.states.async_set(entity_id, state)
    for action in ("turn_on", "turn_off", "toggle"):
        hass.services.async_register("media_player", action, env.calls.append)
    for action in ("set_value", "increment"):
        hass.services.async_register("counter", action, env.calls.append)
    assert await async_setup_component(hass, "scene", {"scene": SCENES})
    # The script integration copies its default blueprints into the config
    # folder (the tests folder here): not needed.
    with patch(
        "homeassistant.components.blueprint.models.DomainBlueprints.async_populate"
    ):
        assert await async_setup_component(hass, "script", {"script": SCRIPTS})
    await hass.async_block_till_done()
    # A scene of another integration (no configuration, no member list) and
    # one that only lists its members as the state attribute.
    hass.states.async_set("scene.hue_relax", "2026-11-21T18:00:00+00:00")
    # A script state without a script entity (another integration's).
    hass.states.async_set("script.ghost", "off")
    hass.states.async_set(
        "scene.listed_members",
        "2026-11-21T18:00:00+00:00",
        {"entity_id": ["light.bed", "light.not_listed"]},
    )
    return env


def _switched(scenes: Env, before: int) -> list[tuple[str, str, Any]]:
    return [
        (c.domain, c.service, c.data.get("entity_id"))
        for c in scenes.calls[before:]
        if c.domain not in ("scene", "script")
    ]


# ── adults ───────────────────────────────────────────────────────────────


async def test_filmabend_runs_when_its_lights_are_released(
    scenes: Env, hass: HomeAssistant
) -> None:
    """Regression: a real scene with members of another domain than scene
    was refused with blocked_group_member although every light was released."""
    assert hass.states.get("scene.filmabend").attributes["entity_id"] == [
        "light.bed",
        "light.ceiling",
    ]
    await scenes.apply(make_snapshot(CLIENT_ID, scenes.user.id, _entities()))
    before = len(scenes.calls)
    response = await scenes.control(entity_id="scene.filmabend", action="turn_on")
    await hass.async_block_till_done()
    result = response["results"][0]
    assert result["ok"] and result["executed"], result
    switched = _switched(scenes, before)
    assert ("light", "turn_on", "light.bed") in switched, switched
    assert ("light", "turn_on", "light.ceiling") in switched, switched
    # The same check over REST and as a batch (quick access widgets).
    response = await scenes.control(entity_ids=["scene.filmabend"], action="turn_on")
    assert response["results"][0]["ok"]
    response = await scenes.control(
        entity_ids=["scene.filmabend"], action="turn_on", origin="quick_access"
    )
    assert response["results"][0]["ok"]


async def test_scene_with_a_device_outside_the_areas_is_refused(
    scenes: Env, hass: HomeAssistant
) -> None:
    await scenes.apply(make_snapshot(CLIENT_ID, scenes.user.id, _entities()))
    before = len(scenes.calls)
    await scenes.denied(
        "blocked_group_member", entity_id="scene.outside", action="turn_on"
    )
    await scenes.denied(
        "blocked_group_member", entity_id="scene.listed_members", action="turn_on"
    )
    assert _switched(scenes, before) == []
    # Admins keep the direct way (allowlist only).
    admin = await hass.auth.async_create_user("Admin", group_ids=["system-admin"])
    response = await scenes.control(
        user_id=admin.id, entity_id="scene.outside", action="turn_on"
    )
    assert response["results"][0]["executed"]


async def test_scene_target_view_only_hidden_or_not_released(
    scenes: Env, hass: HomeAssistant
) -> None:
    # View only at home: the scene would switch it anyway → refused.
    await scenes.apply(
        make_snapshot(
            CLIENT_ID,
            scenes.user.id,
            _entities(**{"light.ceiling": access(local="view")}),
        )
    )
    await scenes.denied(
        "blocked_group_member", entity_id="scene.filmabend", action="turn_on"
    )
    # A released scene stays released, but its targets decide.
    await scenes.apply(make_snapshot(CLIENT_ID, scenes.user.id, _entities()))
    await scenes.control(entity_id="scene.filmabend", action="turn_on")
    # The scene itself not released: its own reason first.
    await scenes.apply(
        make_snapshot(
            CLIENT_ID, scenes.user.id, _entities(**{"scene.filmabend": access()})
        )
    )
    await scenes.denied(
        "scene_not_released", entity_id="scene.filmabend", action="turn_on"
    )


async def test_read_only_targets_ignored_other_domains_refused(
    scenes: Env, hass: HomeAssistant
) -> None:
    await scenes.apply(make_snapshot(CLIENT_ID, scenes.user.id, _entities()))
    # Sensors stored with the device are ignored, a missing entity is skipped.
    await scenes.control(entity_id="scene.sensors_too", action="turn_on")
    # counter is not in the allowlist: the scene could switch it unchecked.
    await scenes.denied(
        "blocked_group_member", entity_id="scene.counter", action="turn_on"
    )
    allowlist: Allowlist = scenes.manager.allowlist
    assert "sensor" in allowlist.read_only_domains
    assert not allowlist.read_only_domains & set(allowlist.domains)
    assert allowlist.runs_targets("scene", "turn_on")
    assert allowlist.runs_targets("script", "toggle")
    assert not allowlist.runs_targets("script", "turn_off")


async def test_critical_targets_from_away_and_widgets(
    scenes: Env, hass: HomeAssistant
) -> None:
    """Rules of critical target domains apply through a scene: the lock is
    only viewable from away and only a status in widgets."""
    await scenes.apply(
        make_snapshot(CLIENT_ID, scenes.user.id, _entities(), remote_access=True)
    )
    # At home both scenes run (lock level control locally).
    await scenes.control(entity_id="scene.arrive", action="turn_on")
    await scenes.control(entity_id="scene.leave", action="turn_on")
    # From away: locking is the safe direction, unlocking is refused.
    await scenes.control(route="external", entity_id="scene.leave", action="turn_on")
    await scenes.denied(
        "blocked_remote", route="external", entity_id="scene.arrive", action="turn_on"
    )
    # Widgets: the lock is not controllable there, in either direction.
    for scene in ("scene.leave", "scene.arrive"):
        response = await scenes.control(
            entity_ids=[scene], action="turn_on", origin="quick_access"
        )
        assert response["results"][0]["reason"] == "blocked_quick_access"
    # A script that unlocks: same rule.
    await scenes.denied(
        "blocked_remote", route="external", entity_id="script.unlock", action="turn_on"
    )
    await scenes.control(entity_id="script.unlock", action="turn_on")


async def test_own_entity_as_target_is_refused(
    scenes: Env, hass: HomeAssistant
) -> None:
    own = er.async_get(hass).async_get_or_create(
        "light", DOMAIN, f"{scenes.entry.entry_id}_panel_light"
    )
    hass.states.async_set(own.entity_id, "off")
    hass.states.async_set(
        "scene.panel", "unknown", {"entity_id": ["light.ceiling", own.entity_id]}
    )
    entities = _entities(**{own.entity_id: access(), "scene.panel": access(run=True)})
    await scenes.apply(make_snapshot(CLIENT_ID, scenes.user.id, entities))
    await scenes.denied(
        "blocked_group_member", entity_id="scene.panel", action="turn_on"
    )


async def test_scripts_nested_cycles_depth_and_undeterminable(
    scenes: Env, hass: HomeAssistant
) -> None:
    await scenes.apply(make_snapshot(CLIENT_ID, scenes.user.id, _entities()))
    # Condition on sun.sun (read only), a light, an activated scene: all fine.
    before = len(scenes.calls)
    await scenes.control(entity_id="script.evening", action="turn_on")
    await hass.async_block_till_done()
    assert ("light", "turn_on", ["light.ceiling"]) in _switched(scenes, before), (
        _switched(scenes, before)
    )
    await scenes.denied(
        "blocked_group_member", entity_id="script.outside", action="turn_on"
    )
    # Cycles end, three levels are followed, the fourth is refused.
    await scenes.control(entity_id="script.loop_a", action="turn_on", check_only=True)
    await scenes.control(entity_id="script.deep_2", action="turn_on", check_only=True)
    await scenes.denied(
        "blocked_group_member", entity_id="script.deep_1", action="turn_on"
    )
    # Turning a script off runs nothing: no target check.
    await scenes.control(entity_id="script.outside", action="turn_off")
    # The nested scene must be released for the device itself.
    await scenes.apply(
        make_snapshot(
            CLIENT_ID, scenes.user.id, _entities(**{"scene.filmabend": access()})
        )
    )
    await scenes.denied(
        "blocked_group_member", entity_id="script.evening", action="turn_on"
    )
    # Targets not determinable: the release covers them for adults.
    await scenes.control(
        entity_id="script.by_device", action="turn_on", check_only=True
    )
    await scenes.control(entity_id="scene.hue_relax", action="turn_on", check_only=True)
    await scenes.control(entity_id="script.ghost", action="turn_on", check_only=True)


# ── kids ─────────────────────────────────────────────────────────────────


async def test_kids_scene_with_a_lock_is_refused(
    scenes: Env, hass: HomeAssistant
) -> None:
    entities = _entities()
    await scenes.apply(
        make_snapshot(CLIENT_ID, scenes.user.id, entities, kids=_kids(entities))
    )
    before = len(scenes.calls)
    await scenes.control(entity_id="scene.filmabend", action="turn_on")
    for scene in ("scene.leave", "scene.arrive"):
        await scenes.denied("blocked_kids", entity_id=scene, action="turn_on")
    await scenes.denied("blocked_kids", entity_id="script.unlock", action="turn_on")
    assert not [c for c in scenes.calls[before:] if c.domain == "lock"]
    # A release of the lock by the family manager lifts the blocked type.
    future = (dt_util.utcnow() + timedelta(hours=1)).isoformat()
    kids = _kids(
        entities,
        override={"lock.front": {**kids_rules(blocked=True), "release_until": future}},
    )
    await scenes.apply(make_snapshot(CLIENT_ID, scenes.user.id, entities, kids=kids))
    await scenes.control(entity_id="scene.leave", action="turn_on", check_only=True)


async def test_kids_target_bedtime_colour_and_volume(
    scenes: Env, hass: HomeAssistant
) -> None:
    entities = _entities()
    quiet = {"light.bed": kids_rules(quiet=ALL_DAY)}
    await scenes.apply(
        make_snapshot(
            CLIENT_ID, scenes.user.id, entities, kids=_kids(entities, override=quiet)
        )
    )
    err = await scenes.denied(
        "blocked_kids_bedtime", entity_id="scene.filmabend", action="turn_on"
    )
    assert err.translation_placeholders["until"]
    # Switching it off is allowed at bedtime, also through a scene.
    await scenes.control(entity_id="scene.night", action="turn_on", check_only=True)
    # A release of the scene by the family manager covers its targets' bedtime.
    future = (dt_util.utcnow() + timedelta(hours=1)).isoformat()
    released = {
        **quiet,
        "scene.filmabend": {**kids_rules(), "release_until": future},
    }
    await scenes.apply(
        make_snapshot(
            CLIENT_ID, scenes.user.id, entities, kids=_kids(entities, override=released)
        )
    )
    await scenes.control(entity_id="scene.filmabend", action="turn_on", check_only=True)
    # Colour lock: a colour or an effect is refused, white light is fine.
    rules = {
        "light.bed": kids_rules(color_lock=True),
        "media_player.tv": kids_rules(volume_max=40),
    }
    await scenes.apply(
        make_snapshot(
            CLIENT_ID, scenes.user.id, entities, kids=_kids(entities, override=rules)
        )
    )
    await scenes.denied("blocked_kids_color", entity_id="scene.party", action="turn_on")
    await scenes.denied(
        "blocked_kids_color", entity_id="scene.effect", action="turn_on"
    )
    await scenes.denied(
        "blocked_kids_color", entity_id="scene.plain_colour", action="turn_on"
    )
    await scenes.control(entity_id="scene.calm", action="turn_on", check_only=True)
    await scenes.control(entity_id="scene.filmabend", action="turn_on", check_only=True)
    # Volume cap: a stored volume above it is refused.
    rules["light.bed"] = kids_rules()
    await scenes.apply(
        make_snapshot(
            CLIENT_ID, scenes.user.id, entities, kids=_kids(entities, override=rules)
        )
    )
    await scenes.denied(
        "blocked_kids_volume", entity_id="scene.party", action="turn_on"
    )


async def test_kids_undeterminable_targets_need_a_release(
    scenes: Env, hass: HomeAssistant
) -> None:
    entities = _entities()
    await scenes.apply(
        make_snapshot(CLIENT_ID, scenes.user.id, entities, kids=_kids(entities))
    )
    await scenes.denied("blocked_kids", entity_id="scene.hue_relax", action="turn_on")
    await scenes.denied("blocked_kids", entity_id="script.by_device", action="turn_on")
    await scenes.denied("blocked_kids", entity_id="script.ghost", action="turn_on")
    future = (dt_util.utcnow() + timedelta(hours=1)).isoformat()
    released = {
        "scene.hue_relax": {**kids_rules(), "release_until": future},
        "script.by_device": {**kids_rules(), "release_until": future},
    }
    await scenes.apply(
        make_snapshot(
            CLIENT_ID, scenes.user.id, entities, kids=_kids(entities, override=released)
        )
    )
    await scenes.control(entity_id="scene.hue_relax", action="turn_on", check_only=True)
    await scenes.control(
        entity_id="script.by_device", action="turn_on", check_only=True
    )
    # Target outside the areas stays refused for kids as well.
    await scenes.denied(
        "blocked_group_member", entity_id="scene.outside", action="turn_on"
    )
