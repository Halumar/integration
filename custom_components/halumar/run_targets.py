"""Entities a Home Assistant script switches.

Rules: contracts docs/enforcement.md, section 2.

The walk goes through the script's validated sequence (blueprint inputs
already filled in) and resolves every target the way Home Assistant does
when the script runs:

- ``entity_id: all`` = every entity of the action's domain (of every domain
  for ``homeassistant.*``), ``none`` = nothing;
- devices, areas, floors and labels through the registries (Home
  Assistant's own target resolution, old style groups expanded);
- registry ids of entities, scripts called as actions
  (``script.<name>``), the entities of ``scene.apply``/``scene.create``,
  device actions, and entity ids named in other data fields;
- entities of conditions and waits count as targets as well.

Fail closed: what cannot be determined before the script runs makes the
result incomplete – templates in an action, its target or its data,
unknown devices, areas, floors, labels or registry ids, ``all`` for an
action of an integration, fired events, and actions of an integration
without any target (they may switch anything). An incomplete script runs
only on its release (with parental controls only on a release of the
family manager).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from homeassistant.components import script
from homeassistant.const import ENTITY_MATCH_ALL, ENTITY_MATCH_NONE
from homeassistant.core import HomeAssistant, split_entity_id, valid_entity_id
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import target as target_helpers
from homeassistant.helpers.template import Template

SELECTOR_KEYS = ("entity_id", "device_id", "area_id", "floor_id", "label_id")
_INDIRECT_KEYS = SELECTOR_KEYS[1:]
# Script actions of the script domain itself (anything else calls a script).
_SCRIPT_ACTIONS = frozenset({"turn_on", "turn_off", "toggle", "reload"})
# Actions without targets that do not switch anything.
QUIET_DOMAINS = frozenset(
    {"logbook", "notify", "persistent_notification", "system_log"}
)
_STEP_KEYS = ("action", "service", "service_template")

_Selection = getattr(target_helpers, "TargetSelection", None) or getattr(
    target_helpers, "TargetSelectorData"
)


def _text(value: Any) -> Any:
    """Plain value of a validated config value (templates as their source)."""
    return value.template if isinstance(value, Template) else value


def is_template(value: Any) -> bool:
    """A value Home Assistant renders when the script runs."""
    value = _text(value)
    return isinstance(value, str) and ("{{" in value or "{%" in value or "{#" in value)


def _ids(value: Any) -> list[str] | None:
    """Ids of a selector value; None = rendered at run time or malformed."""
    value = _text(value)
    if isinstance(value, str):
        items: Iterable[Any] = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = value
    else:
        return None
    out: list[str] = []
    for item in items:
        item = _text(item)
        if not isinstance(item, str) or is_template(item):
            return None
        item = item.strip()
        if item:
            out.append(item)
    return out


class _Walk:
    def __init__(self, hass: HomeAssistant, entity_domains: frozenset[str]) -> None:
        self.hass = hass
        self.entity_domains = entity_domains
        self.registry = er.async_get(hass)
        self.found: set[str] = set()
        self.complete = True

    # ── helpers ───────────────────────────────────────────────────────────

    def _entity_domain(self, domain: str) -> bool:
        return domain in self.entity_domains or bool(
            self.hass.states.async_entity_ids(domain)
        )

    def _all(self, domain: str) -> set[str]:
        """``entity_id: all`` of an action of ``domain``."""
        if domain == "homeassistant":
            # Old style groups are covered by their members.
            return {
                eid
                for eid in self.hass.states.async_entity_ids()
                if not eid.startswith("group.")
            }
        if self._entity_domain(domain):
            return set(self.hass.states.async_entity_ids(domain))
        self.complete = False  # all entities of an integration
        return set()

    def _entity_ids(self, values: Iterable[str]) -> list[str]:
        out = []
        for value in values:
            lowered = value.lower()
            if valid_entity_id(lowered):
                out.append(lowered)
            elif (
                resolved := er.async_resolve_entity_id(self.registry, value)
            ) is not None:
                out.append(resolved)
            else:
                self.complete = False  # registry id that does not exist
        return out

    def _resolve(self, selector: dict[str, list[str]], domain: str | None) -> set[str]:
        """Entities of a target selector as Home Assistant resolves them."""
        entity_ids = [
            v
            for v in selector.get("entity_id", [])
            if v not in (ENTITY_MATCH_ALL, ENTITY_MATCH_NONE)
        ]
        found: set[str] = set()
        if ENTITY_MATCH_ALL in selector.get("entity_id", []):
            found |= self._all(domain or "homeassistant")
        config: dict[str, Any] = {
            key: values
            for key, values in selector.items()
            if key != "entity_id" and values
        }
        config["entity_id"] = self._entity_ids(entity_ids)
        selected = target_helpers.async_extract_referenced_entity_ids(
            self.hass, _Selection(config), True
        )
        if (
            selected.missing_devices
            or selected.missing_areas
            or selected.missing_floors
            or selected.missing_labels
        ):
            self.complete = False
        indirect = set(selected.indirectly_referenced)
        if (
            domain is not None
            and domain != "homeassistant"
            and self._entity_domain(domain)
        ):
            # An action of a domain switches only entities of that domain.
            indirect = {e for e in indirect if split_entity_id(e)[0] == domain}
        return found | set(selected.referenced) | indirect

    def _selector(self, *sources: Any) -> tuple[dict[str, list[str]], bool]:
        """Selector keys of the given dicts; False = something is rendered."""
        selector: dict[str, list[str]] = {}
        ok = True
        for source in sources:
            if not isinstance(source, dict):
                continue
            for key in SELECTOR_KEYS:
                value = source.get(key)
                if value in (None, "", []):
                    continue
                ids = _ids(value)
                if ids is None:
                    ok = False
                    continue
                if key == "entity_id":
                    ids = [i.lower() for i in ids]
                selector.setdefault(key, []).extend(ids)
        return selector, ok

    def _named(self, node: Any) -> None:
        """Entity ids named anywhere in action data (e.g. a media player of tts)."""
        node = _text(node)
        if isinstance(node, str):
            lowered = node.strip().lower()
            if valid_entity_id(lowered) and self.hass.states.get(lowered) is not None:
                self.found.add(lowered)
        elif isinstance(node, dict):
            for key, value in node.items():
                self._named(_text(key))
                self._named(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                self._named(item)

    # ── steps ─────────────────────────────────────────────────────────────

    def action(self, step: dict[str, Any]) -> None:
        name = _text(step.get("action", step.get("service")))
        if "service_template" in step or not isinstance(name, str) or is_template(name):
            self.complete = False  # the action itself is rendered
            return
        domain, _, service = name.partition(".")
        target = step.get("target")
        if target is not None and not isinstance(_text(target), dict):
            self.complete = False  # the whole target is a template
            target = None
        sources: list[Any] = [step, target]
        for key in ("data", "data_template"):
            data = step.get(key)
            if data is None:
                continue
            if not isinstance(data, dict):
                self.complete = False
                continue
            sources.append(data)
            self._named(data)
        selector, ok = self._selector(*sources)
        if not ok:
            self.complete = False
        if domain == "script" and service not in _SCRIPT_ACTIONS:
            self.found.add(f"script.{service}")  # a script called directly
            return
        if domain == "scene" and service in ("apply", "create"):
            for source in sources[2:]:
                entities = _text(source.get("entities"))
                if entities is None:
                    continue
                if isinstance(entities, dict):
                    ids = [_text(k) for k in entities]
                    if any(not isinstance(i, str) or is_template(i) for i in ids):
                        self.complete = False
                    self.found |= set(
                        self._entity_ids(
                            [
                                i
                                for i in ids
                                if isinstance(i, str) and not is_template(i)
                            ]
                        )
                    )
                else:
                    self.complete = False
        if any(selector.values()):
            self.found |= self._resolve(selector, domain)
        elif not (domain in QUIET_DOMAINS or self._entity_domain(domain)):
            # An action of an integration without a target may switch anything.
            self.complete = False

    def walk(self, node: Any) -> None:
        node = _text(node)
        if isinstance(node, (list, tuple)):
            for item in node:
                self.walk(item)
            return
        if not isinstance(node, dict):
            return
        if any(key in node for key in _STEP_KEYS):
            self.action(node)
            return
        if isinstance(_text(node.get("event")), str) and not (
            "trigger" in node or "platform" in node
        ):
            self.complete = False  # a fired event may start anything
        scene = _text(node.get("scene"))
        if isinstance(scene, str):
            self.found |= set(self._entity_ids([scene]))
        # Conditions, waits and device actions: their entities count as well
        # (templates there decide nothing that is switched).
        selector, _ok = self._selector(node)
        if selector.get("entity_id"):
            selector["entity_id"] = [
                v
                for v in selector["entity_id"]
                if v not in (ENTITY_MATCH_ALL, ENTITY_MATCH_NONE)
            ]
        domain = _text(node.get("domain"))
        if "type" in node and isinstance(domain, str) and selector.get("device_id"):
            # Device action, condition or trigger: its entity, else the
            # entities of the device in that domain.
            if selector.get("entity_id"):
                selector = {"entity_id": selector["entity_id"]}
            self.found |= self._resolve(selector, domain)
        elif any(selector.values()):
            self.found |= self._resolve(selector, None)
        for key, value in node.items():
            if key not in SELECTOR_KEYS:
                self.walk(value)


def script_targets(
    hass: HomeAssistant, entity_id: str, entity_domains: frozenset[str]
) -> tuple[list[str], bool]:
    """Entities a script switches and whether these are all of them."""
    component = hass.data.get(script.DOMAIN)
    entity = component.get_entity(entity_id) if component is not None else None
    sequence = getattr(getattr(entity, "script", None), "sequence", None)
    if entity is None or sequence is None:
        return [], False  # a script of another integration
    walk = _Walk(hass, entity_domains)
    walk.walk(sequence)
    return sorted(walk.found), walk.complete
