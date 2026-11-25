"""Scripts in halumar.control: what they switch, resolved fail closed.

``entity_id: all``, devices, areas, floors and labels resolve to the
entities Home Assistant would switch; every one of them is judged by the
rule of its own domain. What cannot be determined before the script runs
(templates, events, actions of an integration without a target, unknown
registry ids) runs only on the release, for children only on a release of
the family manager (contracts docs/enforcement.md, section 2).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.halumar.run_targets import _ids, script_targets

from .common import CLIENT_ID
from .test_enforce import (  # noqa: F401 - fixtures
    Env,
    access,
    env,
    existing_owner,
    kids_rules,
    make_snapshot,
)


def _seq(*steps: dict[str, Any]) -> dict[str, Any]:
    return {"sequence": list(steps)}


def _light(target: Any, action: str = "light.turn_off") -> dict[str, Any]:
    return {"action": action, "target": target}


SCRIPTS: dict[str, Any] = {
    # A typical "Alles aus" (everything off) script.
    "alles_aus": _seq(_light({"entity_id": "all"})),
    "alles_aus_data": _seq({"action": "light.turn_off", "data": {"entity_id": "all"}}),
    "everything_off": _seq({"action": "homeassistant.turn_off", "entity_id": "all"}),
    "nothing": _seq(_light({"entity_id": "none"})),
    "kitchen": _seq(_light({"area_id": "kitchen"}, "light.turn_on")),
    "kitchen_all": _seq(
        {"action": "homeassistant.turn_off", "target": {"area_id": "kitchen"}}
    ),
    "ground_floor": _seq(_light({"floor_id": "ground"})),
    "labelled": _seq(_light({"label_id": "night"})),
    "lamp_device": _seq(_light({"device_id": "LAMP"})),
    "device_action": _seq(
        {
            "device_id": "LAMP",
            "domain": "light",
            "type": "turn_off",
            "entity_id": "KLIGHT",
        },
    ),
    "unknown_area": _seq(_light({"area_id": "cellar"})),
    "by_registry_id": _seq(_light({"entity_id": "KLIGHT"})),
    "unknown_registry_id": _seq(
        _light({"entity_id": "0123456789abcdef0123456789abcdef"})
    ),
    "service_template": _seq(
        {"service_template": "light.turn_{{ 'on' }}", "entity_id": "light.bed"}
    ),
    "apply_template": _seq(
        {"action": "scene.apply", "data": {"entities": "{{ {'light.bed': 'on'} }}"}}
    ),
    "scene_step": _seq({"scene": "scene.movie"}),
    "apply_template_key": _seq(
        {"action": "scene.apply", "data": {"entities": {"{{ x }}": "on"}}}
    ),
    "create_snapshot": _seq(
        {
            "action": "scene.create",
            "data": {"scene_id": "before", "snapshot_entities": ["light.bed"]},
        }
    ),
    "all_of_integration": _seq(
        {"action": "hue.activate_scene", "target": {"entity_id": "all"}}
    ),
    "data_rendered": _seq(
        {"action": "light.turn_on", "data": "{{ {'entity_id': 'light.bed'} }}"}
    ),
    "data_list": _seq(
        {
            "action": "light.turn_on",
            "target": {"entity_id": "light.bed"},
            "data": {"members": ["media_player.tv", "not an entity"]},
        }
    ),
    "template_target": _seq(_light({"entity_id": "{{ 'light.' ~ 'bed' }}"})),
    "template_whole_target": _seq(_light("{{ {'entity_id': 'light.bed'} }}")),
    "template_action": _seq(
        {"action": "{{ 'light.turn_on' }}", "target": {"entity_id": "light.bed"}}
    ),
    "template_data": _seq(
        {
            "action": "light.turn_on",
            "data": {
                "entity_id": "{{ states.light | map(attribute='entity_id') | list }}"
            },
        }
    ),
    "calls_script": _seq({"action": "script.outside_one"}),
    "outside_one": _seq(_light({"entity_id": "light.not_listed"})),
    "integration_action": _seq({"action": "rest_command.garage"}),
    "notifies": _seq(
        {"action": "notify.notify", "data": {"message": "{{ now() }}"}},
        _light({"entity_id": "light.bed"}),
    ),
    "fires_event": _seq({"event": "open_garage"}),
    "group_target": _seq(_light({"entity_id": "light.group_bad"})),
    "apply_scene": _seq(
        {"action": "scene.apply", "data": {"entities": {"light.not_listed": "on"}}}
    ),
    "media_in_data": _seq(
        {
            "action": "tts.speak",
            "target": {"entity_id": "tts.cloud"},
            "data": {"media_player_entity_id": "media_player.tv", "message": "Hi"},
        }
    ),
    "choose_inside": _seq(
        {
            "choose": [
                {
                    "conditions": [
                        {"condition": "state", "entity_id": "sun.sun", "state": "x"}
                    ],
                    "sequence": [_light({"entity_id": "all"})],
                }
            ]
        }
    ),
}

RELEASED = [f"script.{name}" for name in SCRIPTS]


def _entities(**overrides: Any) -> dict[str, Any]:
    entities: dict[str, Any] = {
        "light.bed": access(),
        "light.ceiling": access(),
        "light.group_ok": access(),
        "light.group_bad": access(),
        "media_player.tv": access(),
        **{eid: access(run=True) for eid in RELEASED},
    }
    entities.update(overrides)
    return entities


@pytest.fixture
async def targets(env: Env, hass: HomeAssistant) -> Env:  # noqa: F811 - fixture
    """Registries with a kitchen on the ground floor, a lamp device, a label."""
    for entity_id in ("sun.sun", "switch.kettle", "tts.cloud", "counter.visits"):
        hass.states.async_set(entity_id, "off")
    floors = fr.async_get(hass)
    ground = floors.async_create("Ground")
    areas = ar.async_get(hass)
    kitchen = areas.async_create("Kitchen", floor_id=ground.floor_id)
    assert kitchen.id == "kitchen"
    labels = lr.async_get(hass)
    night = labels.async_create("Night")
    assert night.label_id == "night"
    devices = dr.async_get(hass)
    lamp = devices.async_get_or_create(
        config_entry_id=env.entry.entry_id,
        identifiers={("test", "lamp")},
        suggested_area="Kitchen",
    )
    entities = er.async_get(hass)
    # Kitchen light without own area: in the kitchen through its device.
    k_light = entities.async_get_or_create(
        "light", "test", "kitchen_light", device_id=lamp.id
    )
    kettle = entities.async_get_or_create("switch", "test", "kettle")
    entities.async_update_entity(kettle.entity_id, area_id="kitchen")
    # The night label sits on light.bed.
    bed = entities.async_get_or_create("light", "test", "bed")
    entities.async_update_entity(bed.entity_id, labels={"night"})
    for entry in (k_light, kettle, bed):
        hass.states.async_set(entry.entity_id, "off")
    env.ids = {  # type: ignore[attr-defined]
        "lamp": lamp.id,
        "kitchen_light": k_light.entity_id,
        "kettle": kettle.entity_id,
        "bed": bed.entity_id,
    }
    scripts = {
        name: _replace(_replace(config, "LAMP", lamp.id), "KLIGHT", k_light.id)
        for name, config in SCRIPTS.items()
    }
    hass.services.async_register("homeassistant", "turn_off", env.calls.append)
    with patch(
        "homeassistant.components.blueprint.models.DomainBlueprints.async_populate"
    ):
        assert await async_setup_component(hass, "script", {"script": scripts})
    await hass.async_block_till_done()
    return env


def _replace(node: Any, old: str, new: str) -> Any:
    if isinstance(node, dict):
        return {k: _replace(v, old, new) for k, v in node.items()}
    if isinstance(node, list):
        return [_replace(v, old, new) for v in node]
    return new if node == old else node


def _targets(hass: HomeAssistant, name: str) -> tuple[list[str], bool]:
    domains = frozenset({"light", "switch", "media_player", "lock", "script"})
    return script_targets(hass, f"script.{name}", domains)


def _all_lights(e: Env) -> dict[str, Any]:
    ids = e.ids  # type: ignore[attr-defined]
    return {
        "light.not_listed": access(),
        ids["kitchen_light"]: access(),
        ids["bed"]: access(),
    }


# ── resolution ───────────────────────────────────────────────────────────


async def test_entity_id_all_and_none(targets: Env, hass: HomeAssistant) -> None:
    lights = sorted(hass.states.async_entity_ids("light"))
    assert _targets(hass, "alles_aus") == (lights, True)
    assert _targets(hass, "alles_aus_data") == (lights, True)
    assert _targets(hass, "choose_inside") == (sorted([*lights, "sun.sun"]), True)
    everything, complete = _targets(hass, "everything_off")
    assert complete
    assert "switch.kettle" in everything and "counter.visits" in everything
    assert _targets(hass, "nothing") == ([], True)


async def test_areas_floors_labels_devices(targets: Env, hass: HomeAssistant) -> None:
    ids = targets.ids  # type: ignore[attr-defined]
    # An action of a domain switches only that domain of the area.
    assert _targets(hass, "kitchen") == ([ids["kitchen_light"]], True)
    assert _targets(hass, "kitchen_all") == (
        sorted([ids["kitchen_light"], ids["kettle"]]),
        True,
    )
    assert _targets(hass, "ground_floor") == ([ids["kitchen_light"]], True)
    assert _targets(hass, "labelled") == ([ids["bed"]], True)
    assert _targets(hass, "lamp_device") == ([ids["kitchen_light"]], True)
    assert _targets(hass, "device_action") == ([ids["kitchen_light"]], True)
    # An area that does not exist (yet): not determinable.
    assert _targets(hass, "unknown_area") == ([], False)


async def test_templates_events_and_integration_actions(
    targets: Env, hass: HomeAssistant
) -> None:
    for name in (
        "template_target",
        "template_whole_target",
        "template_action",
        "template_data",
        "integration_action",
        "fires_event",
        "unknown_registry_id",
        "service_template",
        "apply_template",
        "apply_template_key",
        "all_of_integration",
        "data_rendered",
    ):
        assert not _targets(hass, name)[1], name
    ids = targets.ids  # type: ignore[attr-defined]
    assert _targets(hass, "by_registry_id") == ([ids["kitchen_light"]], True)
    assert _targets(hass, "scene_step") == (["scene.movie"], True)
    assert _targets(hass, "create_snapshot") == (["light.bed"], True)
    assert _targets(hass, "data_list") == (["light.bed", "media_player.tv"], True)
    assert _ids(5) is None
    # A notification renders a template but switches nothing.
    assert _targets(hass, "notifies") == (["light.bed"], True)
    assert _targets(hass, "calls_script") == (["script.outside_one"], True)
    assert _targets(hass, "apply_scene") == (["light.not_listed"], True)
    assert _targets(hass, "media_in_data") == (
        ["media_player.tv", "tts.cloud"],
        True,
    )
    assert script_targets(hass, "script.ghost", frozenset()) == ([], False)


# ── control ──────────────────────────────────────────────────────────────


async def test_alles_aus_is_refused_outside_the_areas(
    targets: Env, hass: HomeAssistant
) -> None:
    """entity_id all switched every light, also outside the areas."""
    await targets.apply(make_snapshot(CLIENT_ID, targets.user.id, _entities()))
    before = len(targets.calls)
    for name in ("alles_aus", "alles_aus_data", "choose_inside", "everything_off"):
        await targets.denied(
            "blocked_group_member", entity_id=f"script.{name}", action="turn_on"
        )
    assert not [c for c in targets.calls[before:] if c.domain == "light"]
    # Every light of the house released: it runs.
    await targets.apply(
        make_snapshot(CLIENT_ID, targets.user.id, _entities(**_all_lights(targets)))
    )
    response = await targets.control(entity_id="script.alles_aus", action="turn_on")
    await hass.async_block_till_done()
    assert response["results"][0]["executed"]
    assert [c for c in targets.calls[before:] if c.domain == "light"]
    await targets.control(entity_id="script.nothing", action="turn_on", check_only=True)
    # homeassistant.turn_off all also reaches the counter: never allowed.
    await targets.denied(
        "blocked_group_member", entity_id="script.everything_off", action="turn_on"
    )


async def test_area_targets_are_judged(targets: Env, hass: HomeAssistant) -> None:
    ids = targets.ids  # type: ignore[attr-defined]
    await targets.apply(make_snapshot(CLIENT_ID, targets.user.id, _entities()))
    for name in ("kitchen", "ground_floor", "lamp_device", "device_action"):
        await targets.denied(
            "blocked_group_member", entity_id=f"script.{name}", action="turn_on"
        )
    await targets.apply(
        make_snapshot(
            CLIENT_ID,
            targets.user.id,
            _entities(**{ids["kitchen_light"]: access()}),
        )
    )
    for name in ("kitchen", "ground_floor", "lamp_device", "device_action"):
        await targets.control(
            entity_id=f"script.{name}", action="turn_on", check_only=True
        )
    # The kettle of the kitchen is switched by homeassistant.turn_off only.
    await targets.denied(
        "blocked_group_member", entity_id="script.kitchen_all", action="turn_on"
    )
    # Label on a light outside the areas, scripts called directly, scene.apply,
    # a light group with a member outside: refused.
    for name in ("calls_script", "apply_scene", "group_target"):
        await targets.denied(
            "blocked_group_member", entity_id=f"script.{name}", action="turn_on"
        )
    await targets.denied(
        "blocked_group_member", entity_id="script.labelled", action="turn_on"
    )


async def test_undeterminable_scripts_need_a_release(
    targets: Env, hass: HomeAssistant
) -> None:
    undeterminable = (
        "template_target",
        "template_whole_target",
        "template_action",
        "template_data",
        "integration_action",
        "fires_event",
        "unknown_area",
        "unknown_registry_id",
        "service_template",
    )
    entities = _entities()
    await targets.apply(make_snapshot(CLIENT_ID, targets.user.id, entities))
    # Adults: the admin's release covers them.
    for name in undeterminable:
        await targets.control(
            entity_id=f"script.{name}", action="turn_on", check_only=True
        )
    # Kids: only on a release of the family manager.
    kids: dict[str, Any] = {
        "time_zone": "Europe/Zurich",
        "override_until": None,
        "entities": {eid: kids_rules() for eid in entities},
    }
    await targets.apply(make_snapshot(CLIENT_ID, targets.user.id, entities, kids=kids))
    for name in undeterminable:
        await targets.denied(
            "blocked_kids", entity_id=f"script.{name}", action="turn_on"
        )
    await targets.control(
        entity_id="script.notifies", action="turn_on", check_only=True
    )
    future = (dt_util.utcnow() + timedelta(hours=1)).isoformat()
    for name in undeterminable:
        kids["entities"][f"script.{name}"] = {**kids_rules(), "release_until": future}
    await targets.apply(make_snapshot(CLIENT_ID, targets.user.id, entities, kids=kids))
    for name in undeterminable:
        await targets.control(
            entity_id=f"script.{name}", action="turn_on", check_only=True
        )


async def test_kids_alles_aus(targets: Env, hass: HomeAssistant) -> None:
    """A child runs "all off" only when every light is its own to switch."""
    entities = _entities()
    kids = {
        "time_zone": "Europe/Zurich",
        "override_until": None,
        "entities": {eid: kids_rules() for eid in entities},
    }
    await targets.apply(make_snapshot(CLIENT_ID, targets.user.id, entities, kids=kids))
    await targets.denied(
        "blocked_group_member", entity_id="script.alles_aus", action="turn_on"
    )
    entities = _entities(**_all_lights(targets))
    kids["entities"] = {eid: kids_rules() for eid in entities}
    kids["entities"]["light.not_listed"] = kids_rules(blocked=True)
    await targets.apply(make_snapshot(CLIENT_ID, targets.user.id, entities, kids=kids))
    await targets.denied("blocked_kids", entity_id="script.alles_aus", action="turn_on")
    kids["entities"]["light.not_listed"] = kids_rules()
    await targets.apply(make_snapshot(CLIENT_ID, targets.user.id, entities, kids=kids))
    await targets.control(
        entity_id="script.alles_aus", action="turn_on", check_only=True
    )
