"""The action halumar.control: switch on behalf of a device.

Devices with the control path "checked" have read-only HA users: Home
Assistant rejects their direct actions. They switch through this action,
which every authenticated user may call. Devices on the hub path ("Hub
only" in the hub) are checked devices here: the hub calls this action for
them with the device's own token, and the call gets exactly the same
checks. It

1. finds the caller's rule snapshot (only device users of this integration;
   admins and system calls run with their own context, other users get
   not_a_device),
2. judges the route like HA's local_only (local, external, unknown =
   external),
3. checks each entity: action and data strictly against the allowlist,
   the snapshot (areas, "when away", quick access, release of scenes and
   scripts), every member of a group entity, every target of a scene or
   script by the rules of the target's own domain, and the kids rules
   (bedtime, colour lock, volume cap locally; counter and pause by asking
   the hub),
4. executes what is allowed as the system with the caller's context as
   parent (the logbook names the device), and
5. reports every execution and rejection (hub log, ``<domain>_event``,
   alarms as ``<domain>_alarm``).

A single entity that is rejected raises ServiceValidationError with the
reason as translation key; a batch reports per entity.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from ipaddress import ip_address
from typing import Any

from homeassistant.components.homeassistant.scene import (
    DATA_PLATFORM as SCENE_PLATFORM,
)
from homeassistant.components.homeassistant.scene import HomeAssistantScene
from homeassistant.core import (
    Context,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    State,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.http import current_request
from homeassistant.helpers.network import is_cloud_connection
from homeassistant.util import dt as dt_util
from homeassistant.util.network import is_local

from .allowlist import ActionRule
from .const import DOMAIN, SERVICE_CONTROL
from .device_users import async_device_client_id
from .enforcement import DATA_ENFORCEMENT, EnforcementManager
from .run_targets import script_targets
from .snapshot import (
    access_closed,
    access_level,
    in_home_networks,
    parse_utc,
    quick_level,
    quiet_until,
    released,
)

try:
    import probatio as vol
except ImportError:  # pragma: no cover - HA < 2026.9 validates with voluptuous
    import voluptuous as vol  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

KIDS_CHECK_ENDPOINT = "/api/integration/kids-check"
KIDS_CHECK_TIMEOUT = 3.0

ROUTE_LOCAL = "local"
ROUTE_EXTERNAL = "external"
ROUTE_UNKNOWN = "unknown"

# States in which toggle switches something off (kids bedtime, same as hub).
_ON_STATES = frozenset(
    {"on", "playing", "open", "opening", "paused", "idle", "buffering"}
)
_VOLUME_STEP = 0.1

_KIDS_HUB_REASONS = {
    "rate_limited": "blocked_kids_cooldown",
    "cooldown": "blocked_kids_cooldown",
    "paused_all": "blocked_kids_paused_all",
    "blocked": "blocked_kids",
    "quiet_hours": "blocked_kids_bedtime",
}
_KIDS_EVENT_BY_REASON = {
    "blocked_kids_bedtime": "kids_bedtime_attempt",
    "blocked_kids_cooldown": "kids_pause",
    "blocked_kids_paused_all": "kids_pause",
}

# A refused target of a scene or script keeps these reasons (and every
# blocked_kids*); any other refusal is blocked_group_member.
_TARGET_OWN_REASONS = frozenset({"blocked_remote", "blocked_quick_access"})
GROUP_MEMBER = "blocked_group_member"
# Stored scene states that make things quieter or darker (kids bedtime).
_REDUCING_STATES = frozenset({"off", "closed"})
# Light colour modes and attributes that set a colour or an effect.
_COLOR_MODES = frozenset({"hs", "rgb", "rgbw", "rgbww", "xy"})
_COLOR_ATTRS = (
    "hs_color",
    "rgb_color",
    "rgbw_color",
    "rgbww_color",
    "xy_color",
    "color_name",
)
_NO_EFFECT = frozenset({"", "none", "off"})


def _members(state: State) -> set[str] | None:
    """Members of a group entity (its ``entity_id`` attribute), None if none."""
    members = state.attributes.get("entity_id")
    if not isinstance(members, (list, tuple)):
        return None
    return {m for m in members if isinstance(m, str)}


def _safe_state(domain: str, state: str) -> bool:
    """A stored scene state in the safe direction (lock locked, alarm armed)."""
    if domain == "lock":
        return state == "locked"
    return domain == "alarm_control_panel" and state.startswith("armed_")


def _sets_colour(stored: State) -> bool:
    """A stored light state that sets a colour or an effect (kids colour lock)."""
    attrs = stored.attributes
    effect = attrs.get("effect")
    if isinstance(effect, str) and effect.lower() not in _NO_EFFECT:
        return True
    if (mode := attrs.get("color_mode")) is not None:
        return mode in _COLOR_MODES
    return any(attrs.get(key) is not None for key in _COLOR_ATTRS)


def _scene_states(hass: HomeAssistant, entity_id: str) -> dict[str, State] | None:
    """Stored states of a Home Assistant scene, None for other scenes."""
    platform = hass.data.get(SCENE_PLATFORM)
    entity = platform.entities.get(entity_id) if platform is not None else None
    if not isinstance(entity, HomeAssistantScene):
        return None
    return dict(entity.scene_config.states)


def _action(value: Any) -> str:
    text: str = cv.string(value)
    if not text or len(text) > 64 or not all(c.islower() or c == "_" for c in text):
        raise vol.Invalid("action must be the action name of the entity's domain")
    return text


SCHEMA_CONTROL = vol.Schema(
    {
        vol.Exclusive("entity_id", "target"): cv.entity_id,
        vol.Exclusive("entity_ids", "target"): vol.All(
            cv.ensure_list, [cv.entity_id], vol.Length(min=1, max=50)
        ),
        vol.Required("action"): _action,
        vol.Optional("data", default=dict): dict,
        vol.Optional("origin", default="app"): vol.In(["app", "quick_access"]),
        vol.Optional("check_only", default=False): cv.boolean,
        vol.Optional("grant"): vol.All(cv.string, vol.Length(min=16, max=128)),
    }
)


@dataclass
class Result:
    """Decision and outcome for one entity."""

    entity_id: str
    reason: str | None = None
    until: datetime | None = None
    action: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    adjusted: dict[str, Any] | None = None
    kids_check: bool = False
    # Members of a group whose counter or pause the hub decides, asked
    # (and counted) like a direct call on each of them.
    kids_members: list[str] = field(default_factory=list)
    executed: bool = False

    @property
    def ok(self) -> bool:
        """Allowed (and, if executed, without error)."""
        return self.reason is None

    def as_dict(self) -> dict[str, Any]:
        """Entry of the response."""
        return {
            "entity_id": self.entity_id,
            "ok": self.ok,
            "executed": self.executed,
            "reason": self.reason,
            "until": self.until.isoformat() if self.until else None,
            "adjusted": self.adjusted,
        }


def connection_route(
    hass: HomeAssistant, home_networks: list[str] | None = None
) -> str:
    """Route of the current call, judged like HA's local_only.

    A caller from one of the household's further home networks (from
    the caller's snapshot) is on the local route as well; Home Assistant
    Cloud stays external.
    """
    if is_cloud_connection(hass):
        return ROUTE_EXTERNAL
    request = current_request.get()
    if request is None or request.remote is None:
        return ROUTE_UNKNOWN
    try:
        if is_local(ip_address(request.remote)):
            return ROUTE_LOCAL
    except ValueError:
        return ROUTE_EXTERNAL
    if home_networks and in_home_networks(home_networks, request.remote):
        return ROUTE_LOCAL
    return ROUTE_EXTERNAL


class _Control:
    """One call of halumar.control."""

    def __init__(self, manager: EnforcementManager, call: ServiceCall) -> None:
        self.manager = manager
        self.hass = manager.hass
        self.call = call
        self.route = connection_route(self.hass)
        self.origin: str = call.data["origin"]
        self.action: str = call.data["action"]
        self.data: dict[str, Any] = dict(call.data["data"])
        self.client_id: str | None = None
        self.snapshot: dict[str, Any] | None = None
        self.now = dt_util.utcnow()

    # ── checks ───────────────────────────────────────────────────────────

    def _member_reason(self, entity_id: str, safe: bool) -> str | None:
        """Snapshot checks of one entity (also used for group members)."""
        snap = self.snapshot
        assert snap is not None
        access = snap["entities"].get(entity_id)
        if access is None:
            return "blocked_area"
        level = access_level(access, self.route, snap["remote_access"])
        if level == "hidden":
            return "blocked_remote"
        if level == "view" and not safe:
            return "blocked_view_only" if self.route == "local" else "blocked_remote"
        if self.origin == "quick_access":
            if quick_level(access, self.route, snap["remote_access"]) != "control":
                return "blocked_quick_access"
        domain = entity_id.split(".", 1)[0]
        if self.manager.allowlist.release_required(domain) and not access.get("run"):
            return "scene_not_released"
        kids = snap["kids"]
        if kids is not None:
            k_rules = kids["entities"].get(entity_id)
            if k_rules is None:
                return "blocked_kids"
            # A release of this entity lifts a blocked device type.
            if k_rules["blocked"] and not released(
                k_rules.get("release_until"), dt_util.utcnow()
            ):
                return "blocked_kids"
        return None

    def _own_entity(self, entity_id: str) -> bool:
        """An entity of this integration (its platform in the entity registry)."""
        entry = er.async_get(self.hass).async_get(entity_id)
        return entry is not None and entry.platform == DOMAIN

    def _check(self, entity_id: str) -> Result:
        result = Result(entity_id, action=self.action, data=dict(self.data))
        domain = entity_id.split(".", 1)[0]
        reason = self.manager.allowlist.check(domain, self.action, self.data)
        if reason is not None:
            result.reason = reason
            return result
        rule = self.manager.allowlist.rule(domain, self.action)
        assert rule is not None
        state = self.hass.states.get(entity_id)
        if state is None:
            result.reason = "unknown_entity"
            return result
        if self.snapshot is None:
            return result  # admin or system call: allowlist only
        if self.manager.allowlist.own_entities_blocked and self._own_entity(entity_id):
            # Wall panel controls, hub buttons and switches are operated
            # only by HA admins and automations, never through a device.
            result.reason = "own_entity"
            return result
        closed, opens_at = access_closed(self.snapshot["access"], self.now)
        if closed:
            # Outside its access hours or with its switch off.
            result.reason, result.until = "blocked_access_closed", opens_at
            return result
        if (reason := self._member_reason(entity_id, rule.safe)) is not None:
            result.reason = reason
            return result
        allowlist = self.manager.allowlist
        if allowlist.runs_targets(domain, self.action):
            result.reason, result.until = self._targets_reason(
                entity_id,
                state,
                1,
                frozenset({entity_id}),
                self._kids_released(entity_id),
            )
            if result.reason is not None:
                return result
        elif domain not in allowlist.target_actions:
            result.reason, result.until = self._members_reason(
                result, rule, state, 1, frozenset({entity_id})
            )
            if result.reason is not None:
                return result
        if self.snapshot["kids"] is not None:
            self._check_kids(result, rule, state)
        return result

    def _members_reason(
        self,
        result: Result,
        rule: ActionRule,
        state: State,
        depth: int,
        seen: frozenset[str],
    ) -> tuple[str | None, datetime | None]:
        """Why a group may not switch its members (None = it may).

        The group's action reaches every member, also those of a group
        among its members: each is checked as if the device switched it
        itself, kids rules with the call's data included (volume caps of
        the members lower ``result.data``). Reasons as for the targets of
        a scene or script.
        """
        members = state.attributes.get("entity_id")
        if not isinstance(members, (list, tuple)):
            return None, None
        domain = state.entity_id.split(".", 1)[0]
        allowlist = self.manager.allowlist
        for member in sorted({m for m in members if isinstance(m, str)} - seen):
            if member.split(".", 1)[0] != domain:
                return GROUP_MEMBER, None
            if allowlist.own_entities_blocked and self._own_entity(member):
                return GROUP_MEMBER, None
            if (reason := self._member_reason(member, rule.safe)) is not None:
                if reason in _TARGET_OWN_REASONS or reason.startswith("blocked_kids"):
                    return reason, None
                return GROUP_MEMBER, None
            member_state = self.hass.states.get(member)
            if member_state is None:
                continue  # does not exist: nothing to switch
            if self.snapshot is not None and self.snapshot["kids"] is not None:
                self._check_kids(result, rule, member_state, member)
                if result.reason is not None:
                    return result.reason, result.until
            if isinstance(member_state.attributes.get("entity_id"), (list, tuple)):
                if depth >= allowlist.target_max_depth:
                    return GROUP_MEMBER, None
                reason, until = self._members_reason(
                    result, rule, member_state, depth + 1, seen | {member}
                )
                if reason is not None:
                    return reason, until
        return None, None

    # ── scenes and scripts: their targets ────────────────────────────────

    def _kids_released(self, entity_id: str) -> bool:
        """A running release of the family manager for this entity."""
        kids = self.snapshot["kids"] if self.snapshot else None
        if kids is None:
            return False
        k_rules = kids["entities"].get(entity_id) or {}
        return released(k_rules.get("release_until"), self.now)

    def _targets(
        self, entity_id: str, state: State
    ) -> tuple[dict[str, State | None], bool]:
        """Entities a scene or script switches (with the stored scene state)
        and whether these are all of them."""
        if entity_id.startswith("scene."):
            stored = _scene_states(self.hass, entity_id)
            if stored is not None:
                return dict(stored), True
            members = state.attributes.get("entity_id")
            if isinstance(members, (list, tuple)):
                return {m: None for m in members if isinstance(m, str)}, True
            return {}, False
        allowlist = self.manager.allowlist
        entities, complete = script_targets(
            self.hass,
            entity_id,
            frozenset(allowlist.domains) | allowlist.read_only_domains,
        )
        return dict.fromkeys(entities), complete

    def _targets_reason(
        self,
        entity_id: str,
        state: State,
        depth: int,
        seen: frozenset[str],
        top_released: bool,
    ) -> tuple[str | None, datetime | None]:
        """Why a scene or script may not switch its targets (None = it may).

        Every target is judged by the rules of its own domain, as if the
        device switched it itself (contracts docs/enforcement.md, section 2).
        """
        targets, complete = self._targets(entity_id, state)
        if (
            not complete
            and self.snapshot is not None
            and self.snapshot["kids"] is not None
            and not (top_released or self._kids_released(entity_id))
        ):
            return "blocked_kids", None
        for target in sorted(targets):
            if target in seen:
                continue  # a cycle: already checked
            reason, until = self._target_reason(
                target, targets[target], depth, seen | {target}, top_released
            )
            if reason is not None:
                _LOGGER.debug(
                    "%s refused for %s: target %s (%s)",
                    entity_id,
                    self.client_id,
                    target,
                    reason,
                )
                return reason, until
        return None, None

    def _target_reason(
        self,
        target: str,
        stored: State | None,
        depth: int,
        seen: frozenset[str],
        top_released: bool,
    ) -> tuple[str | None, datetime | None]:
        allowlist = self.manager.allowlist
        domain = target.split(".", 1)[0]
        if domain in allowlist.read_only_domains:
            return None, None
        state = self.hass.states.get(target)
        if state is None:
            return None, None  # does not exist: nothing to switch
        if domain not in allowlist.domains:
            return GROUP_MEMBER, None
        if allowlist.own_entities_blocked and self._own_entity(target):
            return GROUP_MEMBER, None
        safe = stored is not None and _safe_state(domain, stored.state)
        if (reason := self._member_reason(target, safe)) is not None:
            if reason in _TARGET_OWN_REASONS or reason.startswith("blocked_kids"):
                return reason, None
            return GROUP_MEMBER, None
        if self.snapshot is not None and self.snapshot["kids"] is not None:
            reason, until = self._target_kids(target, stored, top_released)
            if reason is not None:
                return reason, until
        if domain in allowlist.target_actions:
            if depth >= allowlist.target_max_depth:
                return GROUP_MEMBER, None
            return self._targets_reason(target, state, depth + 1, seen, top_released)
        members = state.attributes.get("entity_id")
        if isinstance(members, (list, tuple)):
            # A group among the targets switches its members as well.
            for member in sorted(m for m in members if isinstance(m, str)):
                if member in seen:
                    continue
                reason, until = self._target_reason(
                    member, None, depth, seen | {member}, top_released
                )
                if reason is not None:
                    return reason, until
        return None, None

    def _target_kids(
        self, target: str, stored: State | None, top_released: bool
    ) -> tuple[str | None, datetime | None]:
        """Kids rules of one target: bedtime, colour lock, volume cap."""
        kids = self.snapshot["kids"] if self.snapshot else None
        assert kids is not None
        k_rules = kids["entities"][target]
        now = self.now
        bedtime_free = (
            top_released
            or released(kids["override_until"], now)
            or released(kids.get("bedtime_released_until"), now)
            or released(k_rules.get("release_until"), now)
        )
        reducing = stored is not None and stored.state in _REDUCING_STATES
        if not bedtime_free and not reducing:
            until = quiet_until(k_rules["quiet_hours"], now, kids["time_zone"])
            if until is not None:
                return "blocked_kids_bedtime", until
        if stored is None:
            return None, None
        if (
            k_rules["color_lock"]
            and target.startswith("light.")
            and stored.state == "on"
            and _sets_colour(stored)
        ):
            return "blocked_kids_color", None
        vmax = k_rules["volume_max"]
        volume = stored.attributes.get("volume_level")
        if (
            vmax is not None
            and target.startswith("media_player.")
            and isinstance(volume, (int, float))
            and volume > vmax / 100 + 1e-6
        ):
            return "blocked_kids_volume", None
        return None, None

    def _check_kids(
        self,
        result: Result,
        rule: ActionRule,
        state: State,
        member: str | None = None,
    ) -> None:
        """Kids rules of the entity, or of one ``member`` of a group.

        A member's bedtime and colour lock apply to the group's call, and a
        switched (non-group) member with its own counter is asked of the hub
        like a direct call (``kids_members``); "pause everything" counts a
        group as one action of the group. Volume caps are decided once for the whole
        group (``_check_volume``).
        """
        kids = self.snapshot["kids"] if self.snapshot else None
        assert kids is not None
        k_rules = kids["entities"][member or result.entity_id]
        toggled = self.hass.states.get(result.entity_id) or state
        now = dt_util.utcnow()
        override = released(kids["override_until"], now)
        # Releases of the family manager: bedtime, pause, one entity.
        bedtime_free = (
            override
            or released(kids.get("bedtime_released_until"), now)
            or released(k_rules.get("release_until"), now)
        )
        pause_free = override or released(kids.get("pause_released_until"), now)
        reducing = (
            rule.reduces
            or (
                result.action == "volume_mute"
                and result.data.get("is_volume_muted") is True
            )
            # A group toggles all its members by its own state.
            or (result.action == "toggle" and toggled.state in _ON_STATES)
        )
        if not bedtime_free and not reducing:
            until = quiet_until(k_rules["quiet_hours"], now, kids["time_zone"])
            if until is not None:
                result.reason, result.until = "blocked_kids_bedtime", until
                return
        if k_rules["color_lock"] and rule.has_class(result.data, "color"):
            result.reason = "blocked_kids_color"
            return
        # Counter and pause live in the hub: per entity (rate_limit) and of
        # everything of the device (rate_limit_all).
        pause_all = (kids.get("rate_limit_all") or {}).get("enabled") is True
        if member is not None:
            # A switched member's own counter is asked like a direct call.
            # "Pause everything" counts the group once (below): the hub
            # counts every allowed check as an action of its own entity.
            if (
                not pause_free
                and k_rules["rate_limit"] is not None
                and _members(state) is None
                and member not in result.kids_members
            ):
                result.kids_members.append(member)
            return
        self._check_volume(result, state)
        if result.reason is not None:
            return
        result.kids_check = not pause_free and (
            k_rules["rate_limit"] is not None or pause_all
        )

    def _volume_leaves(
        self, state: State, depth: int, seen: frozenset[str]
    ) -> tuple[list[int], list[float | None]]:
        """Volume caps (entity and members, nested groups included) and the
        current volume of every switched member of a media player group."""
        kids = self.snapshot["kids"] if self.snapshot else None
        assert kids is not None
        caps: list[int] = []
        volumes: list[float | None] = []
        vmax = (kids["entities"].get(state.entity_id) or {}).get("volume_max")
        if isinstance(vmax, int):
            caps.append(vmax)
        members = _members(state)
        if members is None or depth > self.manager.allowlist.target_max_depth:
            return caps, volumes
        for member in sorted(members - seen):
            member_state = self.hass.states.get(member)
            if member_state is None:
                continue
            m_caps, m_volumes = self._volume_leaves(
                member_state, depth + 1, seen | {member}
            )
            caps += m_caps
            if _members(member_state) is not None:
                volumes += m_volumes
            else:
                volume = member_state.attributes.get("volume_level")
                volumes.append(volume if isinstance(volume, (int, float)) else None)
        return caps, volumes

    def _check_volume(self, result: Result, state: State) -> None:
        """Volume cap of a media player, for a group the lowest of all its
        members, decided once.

        ``volume_set`` above the cap is lowered. Home Assistant's group
        steps every member to each member's volume ±0.1 in turn (the last
        wins), so on a group with a capped member a step becomes one
        ``volume_set`` for all: up to the loudest member + 0.1, at most the
        cap; down to the quietest member − 0.1 (nobody gets louder, never
        refused). Members without a volume (off) are left out of loudest
        and quietest; the cap is the lowest of all members.
        """
        if not result.entity_id.startswith("media_player."):
            return
        caps, volumes = self._volume_leaves(state, 1, frozenset({result.entity_id}))
        if not caps:
            return
        cap = min(caps) / 100
        group = _members(state) is not None
        if result.action == "volume_set":
            if result.data.get("volume_level", 0) > cap:
                result.data["volume_level"] = cap
                result.adjusted = {"volume_level": cap}
            return
        if result.action not in ("volume_up", "volume_down"):
            return
        if not group:
            if result.action == "volume_down":
                return
            current = state.attributes.get("volume_level")
            volumes = [current if isinstance(current, (int, float)) else None]
        # Members that are off or unavailable have no volume: left out.
        known = [float(v) for v in volumes if v is not None]
        if result.action == "volume_up":
            if not known:
                result.reason = "blocked_kids_volume"
                return
            loudest = max(known)
            if loudest >= cap - 1e-6:
                result.reason = "blocked_kids_volume"
                return
            if not group and loudest + _VOLUME_STEP <= cap + 1e-6:
                return  # one step stays below the cap
            target = min(round(loudest + _VOLUME_STEP, 4), cap)
        else:
            # Only ever lowers: below the quietest member with a volume.
            quietest = min(known) if known else _VOLUME_STEP
            target = min(max(round(quietest - _VOLUME_STEP, 4), 0.0), cap)
        result.action = "volume_set"
        result.data = {"volume_level": target}
        result.adjusted = {"volume_level": target}

    async def _ask_hub(self, result: Result) -> None:
        """Kids counter and pause live in the hub; no answer = reject.

        Asked for the entity (``kids_check``) and for each switched member of
        a group with its own counter (``kids_members``), in order; the first
        refusal rejects the call.
        Members asked before a refusal stay counted (the hub counts what it
        allowed), so a refused group call can only count more, never less.
        """
        coordinator = self.manager.coordinator()
        if coordinator is None:
            result.reason = "kids_hub_unreachable"
            return
        entity_ids = ([result.entity_id] if result.kids_check else []) + [
            m for m in result.kids_members if m != result.entity_id
        ]
        for entity_id in entity_ids:
            body: dict[str, Any] = {
                "client_id": self.client_id,
                "entity_id": entity_id,
                "action": result.action,
            }
            if grant := self.call.data.get("grant"):
                body["grant"] = grant
            try:
                async with asyncio.timeout(KIDS_CHECK_TIMEOUT):
                    answer = await coordinator.api_post_json(KIDS_CHECK_ENDPOINT, body)
            except Exception as err:  # noqa: BLE001 - any failure rejects the action
                _LOGGER.info("Kids check for %s failed: %s", entity_id, err)
                result.reason = "kids_hub_unreachable"
                return
            if answer.get("allowed") is True:
                continue
            result.reason = _KIDS_HUB_REASONS.get(
                str(answer.get("reason")), "blocked_kids"
            )
            result.until = parse_utc(answer.get("until"))
            return

    # ── run ──────────────────────────────────────────────────────────────

    async def _caller(self) -> None:
        """Resolve the caller; raises for users that may not use the action."""
        user_id = self.call.context.user_id
        if user_id is None:
            return
        user = await self.hass.auth.async_get_user(user_id)
        if user is None:
            raise _denied("not_a_device")
        if user.is_admin:
            return
        client_id = self.manager.client_of_user(user_id)
        if client_id is None:
            reason = (
                "rules_missing"
                if await async_device_client_id(self.hass, user)
                else "not_a_device"
            )
            raise _denied(reason)
        self.client_id = client_id
        self.snapshot = self.manager.snapshots[client_id]
        self.route = connection_route(self.hass, self.snapshot["home_networks"])
        if not self.manager.rate_ok(user_id):
            raise _denied("rate_limited")

    async def run(self) -> ServiceResponse:
        """Check, execute and report."""
        await self._caller()
        single: str | None = self.call.data.get("entity_id")
        entity_ids: list[str] = (
            [single] if single else list(self.call.data.get("entity_ids") or [])
        )
        if not entity_ids:
            raise _denied("not_allowed_field")
        if len(entity_ids) > self.manager.allowlist.max_entities:
            raise _denied("not_allowed_field")
        check_only: bool = self.call.data["check_only"]
        results = [self._check(entity_id) for entity_id in entity_ids]
        if not check_only:
            for result in results:
                if result.ok and (result.kids_check or result.kids_members):
                    await self._ask_hub(result)
            for result in results:
                if result.ok:
                    await self._execute(result)
            self._report(results)
        response: dict[str, Any] = {
            "route": self.route,
            "rules_hash": self.snapshot["rules_hash"] if self.snapshot else None,
            "results": [r.as_dict() for r in results],
        }
        if single is not None and not results[0].ok:
            raise _denied(results[0].reason or "not_allowed_action", results[0])
        if single is None and not self.call.return_response and not check_only:
            denied = [r for r in results if not r.ok]
            if denied:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="control_partly_denied",
                    translation_placeholders={
                        "denied": str(len(denied)),
                        "total": str(len(results)),
                    },
                )
        return response if self.call.return_response else None

    async def _execute(self, result: Result) -> None:
        domain = result.entity_id.split(".", 1)[0]
        # As the system (no user: HA's entity permission check does not
        # apply) with the caller's call as parent: the logbook names the
        # device user. Admins and system calls keep their own context.
        context = (
            Context(parent_id=self.call.context.id)
            if self.snapshot is not None
            else self.call.context
        )
        try:
            await self.hass.services.async_call(
                domain,
                result.action,
                {**result.data, "entity_id": result.entity_id},
                blocking=True,
                context=context,
            )
        except (HomeAssistantError, vol.Invalid) as err:
            _LOGGER.info(
                "%s.%s: %s.%s on %s failed: %s",
                DOMAIN,
                SERVICE_CONTROL,
                domain,
                result.action,
                result.entity_id,
                err,
            )
            result.reason = "action_failed"
            return
        result.executed = True

    def _report(self, results: list[Result]) -> None:
        if self.snapshot is None:
            return
        child = self.snapshot["child_protection"]
        for result in results:
            if result.reason == "action_failed":
                continue  # Home Assistant failed, not a rule
            if result.executed:
                kind = "kids_control" if child else "control"
                reason = None
            else:
                reason = result.reason
                kind = _KIDS_EVENT_BY_REASON.get(reason or "", "denied")
            self.manager.events.record(
                kind,
                client_id=self.client_id,
                entity_id=result.entity_id,
                action=self.action,
                route=self.route,
                origin=self.origin,
                reason=reason,
                until=result.until,
            )


def _denied(reason: str, result: Result | None = None) -> ServiceValidationError:
    entity_id = result.entity_id if result else ""
    until = ""
    if result is not None and result.until is not None:
        until = dt_util.as_local(result.until).strftime("%H:%M")
    placeholders = {"entity_id": entity_id, "until": until}
    if reason == "rate_limited":
        # Shared translation with the privileged actions.
        placeholders.update(service=f"{DOMAIN}.{SERVICE_CONTROL}", wait="60")
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=reason,
        translation_placeholders=placeholders,
    )


async def async_control(call: ServiceCall) -> ServiceResponse:
    """Handle halumar.control (registered in services.py)."""
    manager: EnforcementManager | None = call.hass.data.get(DATA_ENFORCEMENT)
    if manager is None:  # pragma: no cover - registered after the manager
        raise _denied("rules_missing")
    return await _Control(manager, call).run()
