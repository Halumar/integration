"""What halumar.control may execute (contracts data/control-allowlist.json).

The JSON file next to this module is a verbatim copy of the contract data
file (scripts/ci/contracts-check.sh compares them). Per Home Assistant domain
it lists the allowed actions and per action the allowed data fields with
their types and ranges. Everything else is rejected: the action runs as the
system, so the list is the only thing between a device and arbitrary
actions.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

ALLOWLIST_FILE = Path(__file__).parent / "control_allowlist.json"

# Reasons ($defs/control_denial_reason) produced here.
NOT_ALLOWED_ACTION = "not_allowed_action"
NOT_ALLOWED_FIELD = "not_allowed_field"
NOT_ALLOWED_VALUE = "not_allowed_value"

# Data keys that never pass, whatever the list says (defence in depth: the
# contract check forbids them in the list as well).
_ALWAYS_FORBIDDEN = frozenset(
    {"entity_id", "device_id", "area_id", "floor_id", "label_id", "target"}
)


@dataclass(frozen=True)
class ActionRule:
    """One allowed action of a domain."""

    safe: bool
    reduces: bool
    fields: dict[str, dict[str, Any]]

    def has_class(self, data: dict[str, Any], cls: str) -> bool:
        """Whether data sets a field of the given class (color, volume, code)."""
        return any(self.fields.get(key, {}).get("class") == cls for key in data)


@dataclass(frozen=True)
class DomainRule:
    """Allowed actions of one domain."""

    release_required: bool
    actions: dict[str, ActionRule] = field(default_factory=dict)


class Allowlist:
    """Parsed control allowlist."""

    def __init__(self, raw: dict[str, Any]) -> None:
        """Index the contract data."""
        limits = raw["limits"]
        self.version: int = raw["version"]
        self.max_entities: int = limits["max_entities"]
        self.max_data_fields: int = limits["max_data_fields"]
        self.max_calls_per_minute: int = limits["max_calls_per_minute"]
        self.forbidden: frozenset[str] = (
            frozenset(raw["forbidden_fields"]) | _ALWAYS_FORBIDDEN
        )
        # Entities of this integration itself (panel controls, hub
        # buttons and switches) are never operated by a device.
        self.own_entities_blocked: bool = (
            raw.get("own_entities", "blocked") == "blocked"
        )
        self.domains: dict[str, DomainRule] = {
            dom["domain"]: DomainRule(
                release_required=dom["release_required"],
                actions={
                    act["action"]: ActionRule(
                        safe=act["safe"],
                        reduces=act["reduces"],
                        fields={f["name"]: f for f in act["fields"]},
                    )
                    for act in dom["actions"]
                },
            )
            for dom in raw["domains"]
        }
        # Scenes and scripts: the actions that run their targets.
        targets = raw["run_targets"]
        self.target_actions: dict[str, frozenset[str]] = {
            dom["domain"]: frozenset(dom["actions"]) for dom in targets["domains"]
        }
        self.target_max_depth: int = targets["max_depth"]
        self.read_only_domains: frozenset[str] = frozenset(targets["read_only_domains"])

    @classmethod
    def load(cls) -> Allowlist:
        """Read the bundled file (blocking: call in the executor)."""
        return cls(json.loads(ALLOWLIST_FILE.read_text(encoding="utf-8")))

    @classmethod
    async def async_load(cls, hass: HomeAssistant) -> Allowlist:
        """Read the bundled file in the executor."""
        return await hass.async_add_executor_job(cls.load)

    def rule(self, domain: str, action: str) -> ActionRule | None:
        """The rule of an allowed action, None if not allowed."""
        dom = self.domains.get(domain)
        return dom.actions.get(action) if dom else None

    def release_required(self, domain: str) -> bool:
        """Whether entities of the domain must be released for the device."""
        dom = self.domains.get(domain)
        return bool(dom and dom.release_required)

    def runs_targets(self, domain: str, action: str) -> bool:
        """Whether the action runs other entities (scene or script targets)."""
        return action in self.target_actions.get(domain, frozenset())

    def check(self, domain: str, action: str, data: dict[str, Any]) -> str | None:
        """Reason why ``action`` with ``data`` is not allowed, None if it is."""
        rule = self.rule(domain, action)
        if rule is None:
            return NOT_ALLOWED_ACTION
        if len(data) > self.max_data_fields:
            return NOT_ALLOWED_FIELD
        for key, value in data.items():
            if key in self.forbidden or key not in rule.fields:
                return NOT_ALLOWED_FIELD
            if not _value_ok(rule.fields[key], value):
                return NOT_ALLOWED_VALUE
        return None


def _number_ok(spec: dict[str, Any], value: Any, *, integer: bool) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if integer and not isinstance(value, int):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return bool(spec.get("min", -math.inf) <= value <= spec.get("max", math.inf))


def _string_ok(spec: dict[str, Any], value: Any) -> bool:
    return isinstance(value, str) and len(value) <= spec.get("max_length", 255)


def _list_ok(spec: dict[str, Any], value: Any, item_type: str) -> bool:
    if not isinstance(value, list):
        return False
    if not spec.get("min_items", 0) <= len(value) <= spec.get("max_items", 0):
        return False
    if item_type == "string":
        return all(_string_ok(spec, item) for item in value)
    return all(_number_ok(spec, item, integer=item_type == "integer") for item in value)


def _value_ok(spec: dict[str, Any], value: Any) -> bool:
    kind = spec["type"]
    if kind == "boolean":
        return isinstance(value, bool)
    if kind in ("integer", "number"):
        return _number_ok(spec, value, integer=kind == "integer")
    if kind == "string":
        return _string_ok(spec, value)
    if kind == "enum":
        return isinstance(value, str) and value in spec["values"]
    if kind.endswith("_list"):
        return _list_ok(spec, value, kind.removesuffix("_list"))
    if not isinstance(value, str) or len(value) > 64:
        return False
    if kind == "date":
        return dt_util.parse_date(value) is not None
    if kind == "time":
        return dt_util.parse_time(value) is not None
    if kind == "datetime":
        return dt_util.parse_datetime(value) is not None
    return False
