"""Enforcement in Home Assistant.

Owns, once per HA instance (also while the config entry is not loaded):

* the rule snapshot of every device (pushed by the hub, kept in .storage,
  decisions work without the hub);
* the HA group of every device user with a snapshot: ``system-read-only``
  for the control path "checked", ``system-users`` otherwise (a device whose
  rules could not be read is held read-only when it has no earlier ones or
  when the hub's ``control_paths`` next to the snapshots does not name it
  "direct": unreadable rules never loosen); a ``user_updated`` watcher restores it at
  once, a check at start restores what changed while the integration was
  not running (emergency exit: an admin who saves the device user in the HA
  user settings while the integration is down gets a full user; it is set
  back when the integration returns);
* detection of an outage of the integration (time gap between its last
  sign of life and Home Assistant's last saved states) and best-effort
  detection of repeated direct actions of read-only device users (from the
  WebSocket API's log record, which carries the Unauthorized exception);
* rate limiting of halumar.control per user.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.auth.const import GROUP_ID_READ_ONLY, GROUP_ID_USER
from homeassistant.auth.models import User
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_CALL_SERVICE, EVENT_HOMEASSISTANT_FINAL_WRITE
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .allowlist import Allowlist
from .const import DOMAIN, SERVICE_CONTROL, SERVICE_REPORT_LOCATION, SIGNAL_LOCATION
from .events import RULES_APPLIED, EventReporter, device_name_of_user
from .snapshot import (
    CONTROL_PATH_CHECKED,
    CONTROL_PATH_DIRECT,
    SnapshotError,
    is_uuid,
    parse_utc,
    validate_snapshot,
)

if TYPE_CHECKING:
    from .coordinator import HubCoordinator
    from .device_users import DeviceUserManager

_LOGGER = logging.getLogger(__name__)

DATA_ENFORCEMENT = f"{DOMAIN}_enforcement"
STORAGE_KEY = f"{DOMAIN}.enforcement"
STORAGE_VERSION = 1

# HA's user_updated event (homeassistant.auth.EVENT_USER_UPDATED); spelled
# out so the minimum supported HA release needs no newer import.
EVENT_USER_UPDATED = "user_updated"

HEARTBEAT = timedelta(minutes=10)
# Home Assistant saves its states every 15 minutes: a gap above this between
# our last sign of life and HA's means HA ran without the integration.
OUTAGE_GAP = timedelta(minutes=30)

# Best effort: Home Assistant has no event for a rejected action. Its
# WebSocket API logs "Error during service call to <domain>.<action>: %s"
# at ERROR with the Unauthorized exception as argument, which carries the
# user and the entity. REST calls are not logged that way (they answer 401).
WS_LOGGER = "homeassistant.components.websocket_api.http.connection"
DIRECT_WINDOW = timedelta(minutes=10)
DIRECT_THRESHOLD = 3

ISSUE_RESTORED = "device_user_restored"
ISSUE_OUTAGE = "enforcement_outage"
ISSUE_UNREADABLE = "rules_unreadable"
ISSUE_SERVICE_CALLED = "device_service_called"
# Actions of this integration a device user calls on purpose. Actions of
# the allowlist and actions with a target are left to Home Assistant: it
# checks the entity rights of a user (refusals count as direct attempts).
DEVICE_ACTIONS = frozenset(
    {
        (DOMAIN, SERVICE_CONTROL),
        (DOMAIN, SERVICE_REPORT_LOCATION),
        (DOMAIN, "ack_notifications"),
    }
)
_TARGET_KEYS = ("entity_id", "device_id", "area_id", "floor_id", "label_id")
# Calls kept per device for the count of one report.
_SERVICE_CALLS_KEPT = 100
# Contexts of a device user's expected calls (a script or scene runs its
# steps in the caller's context): later calls in them are not reported.
_CONTEXTS_KEPT = 256
_CONTEXT_TTL = timedelta(minutes=5)
# Action field of an enforcement event (contract enforcement-event.json).
_ACTION_NAME = re.compile(r"[a-z_]{1,64}")
# How long a device named in control_paths may wait for its first snapshot
# before the repair says its rules are missing.
AWAITING_GRACE = timedelta(minutes=3)

GROUP_NAMES = {GROUP_ID_READ_ONLY: "read_only", GROUP_ID_USER: "users"}


class _DirectAttemptHandler(logging.Handler):
    """Counts rejected direct actions of read-only device users (best effort)."""

    def __init__(self, manager: EnforcementManager) -> None:
        super().__init__(logging.ERROR)
        self._manager = manager

    def emit(self, record: logging.LogRecord) -> None:
        args = record.args if isinstance(record.args, tuple) else ()
        for arg in args:
            if isinstance(arg, Unauthorized) and arg.user_id:
                try:
                    self._manager.hass.loop.call_soon_threadsafe(
                        self._manager.async_direct_attempt, arg.user_id, arg.entity_id
                    )
                except RuntimeError:  # loop closed
                    pass
                return


class EnforcementManager:
    """Rule snapshots, device user groups and their watchers."""

    def __init__(
        self, hass: HomeAssistant, allowlist: Allowlist, events: EventReporter
    ) -> None:
        """Store collaborators; async_setup loads the state."""
        self.hass = hass
        self.allowlist = allowlist
        self.events = events
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.snapshots: dict[str, dict[str, Any]] = {}
        # client_id -> HA user id of devices whose rules could not be read
        # and that have no earlier snapshot: read-only until rules arrive.
        self._held: dict[str, str] = {}
        # Held devices the hub names in control_paths but never sent rules
        # for, with the time first seen: while the last status still lists
        # the device in provisioning[] (still being set up) and for at most
        # AWAITING_GRACE no repair; after that its snapshot was not built.
        self._awaiting: dict[str, str] = {}
        # Devices whose latest rules were rejected (earlier ones still apply).
        self._rejected: set[str] = set()
        # Devices the hub sent no rules for although its status still lists
        # them as active (earlier rules still apply).
        self._missing: set[str] = set()
        # Group this integration last set per client (to tell a group changed
        # outside the integration from a rule change).
        self._applied_groups: dict[str, str] = {}
        self._alive_at: str | None = None
        self._stopped_at: str | None = None
        self._names: dict[str, str] = {}
        self._calls: dict[str, deque[float]] = {}
        self._direct: dict[str, deque[datetime]] = {}
        self._direct_quiet_until: dict[str, datetime] = {}
        self._service_calls: dict[str, deque[datetime]] = {}
        # Last action without a target named in the device's repair.
        self._service_reported: dict[str, str] = {}
        self._expected_contexts: dict[str, datetime] = {}
        self.last_outage: dict[str, str] | None = None
        self._handler: _DirectAttemptHandler | None = None
        self._unsubs: list[CALLBACK_TYPE] = []

    # ── lifecycle ────────────────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Load the state, detect an outage, start the watchers."""
        data = await self._store.async_load() or {}
        held = data.get("held")
        if isinstance(held, dict):
            self._held = {
                k: v for k, v in held.items() if is_uuid(k) and isinstance(v, str)
            }
        for client_id, snap in (data.get("snapshots") or {}).items():
            try:
                self.snapshots[client_id] = validate_snapshot(snap)
            except SnapshotError as err:
                _LOGGER.warning(
                    "Dropping stored rule snapshot of %s: %s", client_id, err
                )
                # Rules existed but cannot be read any more: never loosen.
                user_id = snap.get("ha_user_id") if isinstance(snap, dict) else None
                if is_uuid(client_id) and isinstance(user_id, str):
                    self._held[client_id] = user_id
        awaiting = data.get("awaiting")
        if isinstance(awaiting, dict):
            self._awaiting = {
                c: t
                for c, t in awaiting.items()
                if c in self._held and isinstance(t, str)
            }
        groups = data.get("applied_groups")
        if isinstance(groups, dict):
            self._applied_groups = {
                k: v
                for k, v in groups.items()
                if v in GROUP_NAMES and (k in self.snapshots or k in self._held)
            }
        self._alive_at = data.get("alive_at")
        self._stopped_at = data.get("stopped_at")
        self._names = {
            k: v
            for k, v in (data.get("names") or {}).items()
            if k in self.snapshots or k in self._held
        }

        self._detect_outage()
        self._update_unreadable_issue()
        self.events.record("integration_started", version=await self._async_version())
        self._alive_at = dt_util.utcnow().isoformat()
        self._save(0)

        self._unsubs.append(
            self.hass.bus.async_listen(EVENT_USER_UPDATED, self._async_user_updated)
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._async_heartbeat, HEARTBEAT, cancel_on_shutdown=True
            )
        )
        self._unsubs.append(
            self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_FINAL_WRITE, self._async_final_write
            )
        )
        self._unsubs.append(
            self.hass.bus.async_listen(EVENT_CALL_SERVICE, self._async_service_called)
        )
        self._handler = _DirectAttemptHandler(self)
        logging.getLogger(WS_LOGGER).addHandler(self._handler)
        self._unsubs.append(async_at_started(self.hass, self._async_started))
        self.events.async_start()

    @callback
    def async_shutdown(self) -> None:
        """Remove listeners and the log handler (tests, unload of HA)."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._handler is not None:
            logging.getLogger(WS_LOGGER).removeHandler(self._handler)
            self._handler = None
        self.events.async_stop()

    async def _async_version(self) -> str:
        from homeassistant.loader import async_get_integration  # noqa: PLC0415

        integration = await async_get_integration(self.hass, DOMAIN)
        return str(integration.version)[:32]

    async def _async_started(self, _hass: HomeAssistant) -> None:
        await self.async_sync_groups()

    async def _async_heartbeat(self, _now: datetime) -> None:
        self._alive_at = dt_util.utcnow().isoformat()
        self._save(0)

    async def _async_final_write(self, _event: Event) -> None:
        self._stopped_at = dt_util.utcnow().isoformat()
        await self._store.async_save(self._data())

    def _data(self) -> dict[str, Any]:
        return {
            "snapshots": self.snapshots,
            "held": self._held,
            "awaiting": self._awaiting,
            "applied_groups": self._applied_groups,
            "names": self._names,
            "alive_at": self._alive_at,
            "stopped_at": self._stopped_at,
        }

    @callback
    def _save(self, delay: float = 1) -> None:
        self._store.async_delay_save(self._data, delay)

    # ── outage ───────────────────────────────────────────────────────────

    def _ha_last_seen(self) -> datetime | None:
        """When Home Assistant last saved its states before this start."""
        try:
            from homeassistant.helpers.restore_state import (  # noqa: PLC0415
                async_get as restore_state,
            )

            data = restore_state(self.hass)
            # HA 2025.11: last_states; newer: split by entity id and registry id.
            pools = (
                getattr(data, name, None)
                for name in (
                    "last_states",
                    "last_states_by_entity_id",
                    "last_states_by_entity_registry_id",
                )
            )
            seen: list[datetime] = [
                stored.last_seen
                for pool in pools
                if isinstance(pool, dict)
                for stored in pool.values()
            ]
            return max(seen, default=None)
        except Exception:  # noqa: BLE001 - detection is best effort
            return None

    def _detect_outage(self) -> None:
        stamps = [
            t for t in (parse_utc(self._alive_at), parse_utc(self._stopped_at)) if t
        ]
        if not stamps:
            return
        last_alive = max(stamps)
        ha_last = self._ha_last_seen()
        if ha_last is None or ha_last - last_alive <= OUTAGE_GAP:
            return
        self.last_outage = {
            "since": last_alive.isoformat(),
            "until": ha_last.isoformat(),
        }
        _LOGGER.warning(
            "The integration was not running from %s until at least %s while "
            "Home Assistant ran: devices with the checked control path could "
            "not switch",
            last_alive,
            ha_last,
        )
        self.events.record(
            "integration_outage", since=last_alive, reliability="best_effort"
        )
        if any(
            s["control_path"] == CONTROL_PATH_CHECKED for s in self.snapshots.values()
        ):
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_OUTAGE,
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_OUTAGE,
                translation_placeholders={
                    "since": dt_util.as_local(last_alive).strftime("%Y-%m-%d %H:%M"),
                    "until": dt_util.as_local(ha_last).strftime("%Y-%m-%d %H:%M"),
                },
            )

    # ── lookups ──────────────────────────────────────────────────────────

    def client_of_user(self, user_id: str) -> str | None:
        """Client id whose snapshot names this HA user."""
        for client_id, snap in self.snapshots.items():
            if snap["ha_user_id"] == user_id:
                return client_id
        return None

    def desired_group_id(self, client_id: str) -> str:
        """HA group the device user of ``client_id`` must have."""
        if client_id in self._held:
            # Unreadable rules without earlier ones, or the hub says the
            # device is not "direct" (control_paths): read-only (fail closed).
            return GROUP_ID_READ_ONLY
        snap = self.snapshots.get(client_id)
        if snap is None:
            return GROUP_ID_USER
        if snap["control_path"] == CONTROL_PATH_CHECKED:
            return GROUP_ID_READ_ONLY
        return GROUP_ID_USER

    def coordinator(self) -> HubCoordinator | None:
        """Coordinator of the loaded config entry, if any."""
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                coordinator: HubCoordinator = entry.runtime_data
                return coordinator
        return None

    def device_info(self, client_id: str) -> tuple[str | None, str | None]:
        """(device name, owner label) for events."""
        coordinator = self.coordinator()
        client = coordinator.client_by_id.get(client_id) if coordinator else None
        if client:
            name = client.get("name")
            owner = client.get("owner_label")
            return (
                name[:100] if isinstance(name, str) else None,
                owner[:50] if isinstance(owner, str) else None,
            )
        return device_name_of_user(self._names.get(client_id)), None

    def hashes(self) -> dict[str, str]:
        """client_id -> rules_hash of every stored snapshot."""
        return {cid: snap["rules_hash"] for cid, snap in self.snapshots.items()}

    @callback
    def rate_ok(self, user_id: str) -> bool:
        """Sliding one-minute window per user (allowlist limit)."""
        now = self.hass.loop.time()
        calls = self._calls.setdefault(user_id, deque())
        while calls and now - calls[0] >= 60:
            calls.popleft()
        if len(calls) >= self.allowlist.max_calls_per_minute:
            return False
        calls.append(now)
        return True

    # ── snapshots ────────────────────────────────────────────────────────

    async def async_apply(
        self,
        snapshots: Any,
        removed: Any,
        complete: bool,
        device_users: DeviceUserManager,
        control_paths: Any,
    ) -> int:
        """Apply snapshots from the hub (or an admin); returns how many were taken.

        A snapshot is taken only when it is valid, its hash matches and its
        HA user is the user this integration created for the device; it is
        never applied to any other user.

        A rejected snapshot never loosens: the device keeps its earlier
        snapshot, or without one its user is held read-only, and a repair
        issue names it. ``control_paths`` (client_id -> control path, next to
        the snapshots and outside their versioned body) tightens it: a device
        it does not name "direct" whose snapshot was not taken is held
        read-only even with an earlier "direct" snapshot. It never loosens. The same holds for a device the hub sends no rules
        for (absent from a complete set, or in ``removed``) while its status
        still lists it as an active device: it is forgotten only once the
        status no longer lists it or its device user is removed. A message
        without a list of snapshots changes nothing.
        """
        if not isinstance(snapshots, list):
            _LOGGER.warning("Ignoring rule snapshots: no list of snapshots")
            return 0
        taken: dict[str, dict[str, Any]] = {}
        rejected: set[str] = set()
        unnamed = 0
        for raw in snapshots:
            named = raw.get("client_id") if isinstance(raw, dict) else None
            try:
                snap = validate_snapshot(raw)
            except SnapshotError as err:
                _LOGGER.warning("Ignoring rule snapshot of %s: %s", named, err)
                if isinstance(named, str) and is_uuid(named):
                    rejected.add(named)
                else:
                    unnamed += 1
                continue
            client_id = snap["client_id"]
            if device_users.user_id_of(client_id) != snap["ha_user_id"]:
                _LOGGER.warning(
                    "Ignoring rule snapshot of %s: its HA user is not the device "
                    "user of this integration",
                    client_id,
                )
                rejected.add(client_id)
                continue
            taken[client_id] = snap
        paths = control_paths if isinstance(control_paths, dict) else {}
        # Not "direct" (checked, or a value this version does not know) and
        # no snapshot taken for it: hold read-only whatever it had before.
        tighten = {
            client_id
            for client_id, path in paths.items()
            if is_uuid(client_id)
            and path != CONTROL_PATH_DIRECT
            and client_id not in taken
        }
        unreadable = rejected - set(taken)
        rejected |= tighten
        rejected -= set(taken)
        gone = (
            {c for c in removed if is_uuid(c)} if isinstance(removed, list) else set()
        )
        if complete and not unnamed:
            # With an entry that names no device nobody can tell which
            # device is absent: forget none then.
            gone |= (set(self.snapshots) | set(self._held)) - set(taken) - rejected
            self._rejected &= rejected
        for client_id in gone - set(taken):
            if self._still_listed(client_id, device_users):
                if client_id not in self._missing:
                    _LOGGER.warning(
                        "The hub sent no rules for device %s but still lists it; "
                        "keeping its earlier rules",
                        client_id,
                    )
                self._missing.add(client_id)
                continue
            await self.async_forget(client_id, save=False)
        for client_id in rejected:
            user_id = device_users.user_id_of(client_id)
            if user_id is None:
                continue  # no device user of this integration: nothing to keep
            if (
                client_id not in unreadable
                and client_id not in self.snapshots
                and (client_id not in self._held or client_id in self._awaiting)
            ):
                # Named before its first snapshot (still being set up):
                # read-only until the rules arrive, nothing to repair.
                self._awaiting.setdefault(client_id, dt_util.utcnow().isoformat())
                self._held[client_id] = user_id
                continue
            self._awaiting.pop(client_id, None)
            self._rejected.add(client_id)
            if client_id not in self.snapshots or client_id in tighten:
                self._held[client_id] = user_id
        changed = []
        for client_id, snap in taken.items():
            old = self.snapshots.get(client_id)
            self.snapshots[client_id] = snap
            self._held.pop(client_id, None)
            self._awaiting.pop(client_id, None)
            self._rejected.discard(client_id)
            self._missing.discard(client_id)
            if old is None or old["rules_hash"] != snap["rules_hash"]:
                changed.append(client_id)
            if snap["location"] == "off" and (old or {}).get("location") != "off":
                # The device may no longer share: forget its position.
                async_dispatcher_send(self.hass, SIGNAL_LOCATION, client_id, None)
        self._save()
        await self.async_sync_groups()
        self._update_unreadable_issue()
        for client_id in changed:
            self.events.record(
                RULES_APPLIED,
                client_id=client_id,
                rules_hash=self.snapshots[client_id]["rules_hash"],
                ha_group=GROUP_NAMES[self.desired_group_id(client_id)],
            )
        return len(taken)

    def _still_listed(self, client_id: str, device_users: DeviceUserManager) -> bool:
        """Whether the device has a device user and the status lists it as active.

        The status is the one of the last successful poll. Without any
        (no loaded entry) nothing is known: the device counts as listed, so
        nothing is freed on a guess.
        """
        if device_users.user_id_of(client_id) is None:
            return False
        coordinator = self.coordinator()
        if coordinator is None or coordinator.data is None:
            return True
        return _active(coordinator.client_by_id.get(client_id))

    def unlisted(self) -> set[str]:
        """Devices kept without rules of the hub that the status no longer lists."""
        coordinator = self.coordinator()
        if coordinator is None or coordinator.data is None:
            return set()
        return {
            client_id
            for client_id in self._missing
            if not _active(coordinator.client_by_id.get(client_id))
        }

    async def async_forget_unlisted(self) -> None:
        """Forget the devices of unlisted() (after a successful poll)."""
        for client_id in self.unlisted():
            await self.async_forget(client_id)

    async def async_forget(self, client_id: str, *, save: bool = True) -> None:
        """Drop a device's snapshot; its user (if any) goes back to system-users."""
        snap = self.snapshots.pop(client_id, None)
        held = self._held.pop(client_id, None)
        self._awaiting.pop(client_id, None)
        self._rejected.discard(client_id)
        self._missing.discard(client_id)
        self._update_unreadable_issue()
        self._applied_groups.pop(client_id, None)
        async_dispatcher_send(self.hass, SIGNAL_LOCATION, client_id, None)
        self._names.pop(client_id, None)
        self._service_calls.pop(client_id, None)
        self._service_reported.pop(client_id, None)
        ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_RESTORED}_{client_id}")
        ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_SERVICE_CALLED}_{client_id}")
        user_id = snap["ha_user_id"] if snap is not None else held
        if user_id is not None:
            user = await self.hass.auth.async_get_user(user_id)
            if (
                user is not None
                and _plain(user)
                and [g.id for g in user.groups] == [GROUP_ID_READ_ONLY]
            ):
                await self.hass.auth.async_update_user(user, group_ids=[GROUP_ID_USER])
        if save:
            self._save()

    # ── groups ───────────────────────────────────────────────────────────

    async def async_sync_groups(self) -> None:
        """Bring every device user with a snapshot to its group."""
        users = {cid: snap["ha_user_id"] for cid, snap in self.snapshots.items()}
        for client_id, user_id in {**self._held, **users}.items():
            user = await self.hass.auth.async_get_user(user_id)
            if user is not None:
                await self._async_ensure_group(client_id, user)

    def _still_provisioning(self) -> set[str]:
        """Awaiting devices that may still be waiting for their first snapshot.

        The hub names a device without a snapshot either because building it
        failed or in the short time before it stored the device's user; in
        that time the request is still in provisioning[] of the status.
        """
        if not self._awaiting:
            return set()
        coordinator = self.coordinator()
        data = coordinator.data if coordinator is not None else None
        listed = {
            item.get("client_id")
            for item in (data or {}).get("provisioning") or []
            if isinstance(item, dict)
        }
        now = dt_util.utcnow()
        return {
            client_id
            for client_id, since in self._awaiting.items()
            if (data is None or client_id in listed)
            and now - (parse_utc(since) or now) < AWAITING_GRACE
        }

    @callback
    def async_status_updated(self) -> None:
        """After a successful poll: a device that left provisioning[] or waited
        too long for its first snapshot gets the repair."""
        if self._awaiting:
            self._update_unreadable_issue()

    @callback
    def _update_unreadable_issue(self) -> None:
        """Repair issue while a device has no rules of the hub that can be read."""
        affected = sorted(
            (self._rejected | self._missing | set(self._held))
            - self._still_provisioning()
        )
        if not affected:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_UNREADABLE)
            return
        names = [self.device_info(cid)[0] or cid for cid in affected[:10]]
        if len(affected) > len(names):
            names.append("…")
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_UNREADABLE,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_UNREADABLE,
            translation_placeholders={"devices": ", ".join(names)},
        )

    async def async_ensure_group(self, client_id: str, user: User) -> None:
        """Bring one device user to its group (also from the device user reconcile)."""
        await self._async_ensure_group(client_id, user)

    async def _async_ensure_group(self, client_id: str, user: User) -> None:
        desired = self.desired_group_id(client_id)
        if user.name and self._names.get(client_id) != user.name:
            self._names[client_id] = user.name
            self._save()
        current = [group.id for group in user.groups]
        if current != [desired]:
            if not _plain(user):
                _LOGGER.error(
                    "HA user %s of device %s is an owner, system user or has login "
                    "data; not changing its groups",
                    user.id,
                    client_id,
                )
                return
            applied = self._applied_groups.get(client_id)
            changed_outside = applied is not None and current != [applied]
            await self.hass.auth.async_update_user(user, group_ids=[desired])
            _LOGGER.info(
                "HA user %s of device %s: groups %s -> %s",
                user.id,
                client_id,
                current,
                desired,
            )
            if changed_outside:
                self._report_restored(client_id, user, desired)
        if self._applied_groups.get(client_id) != desired:
            self._applied_groups[client_id] = desired
            self._save()

    def _report_restored(self, client_id: str, user: User, desired: str) -> None:
        _LOGGER.warning(
            "HA user %s of device %s had other rights; set back to %s",
            user.id,
            client_id,
            desired,
        )
        self.events.record(
            "access_group_restored", client_id=client_id, ha_group=GROUP_NAMES[desired]
        )
        name = (
            self.device_info(client_id)[0]
            or device_name_of_user(user.name)
            or client_id
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_RESTORED}_{client_id}",
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_RESTORED,
            translation_placeholders={"device": name},
        )

    async def _async_user_updated(self, event: Event) -> None:
        user_id = event.data.get("user_id")
        if not isinstance(user_id, str):
            return
        client_id = self.client_of_user(user_id)
        if client_id is None:
            client_id = next(
                (cid for cid, held in self._held.items() if held == user_id), None
            )
        if client_id is None:
            return
        user = await self.hass.auth.async_get_user(user_id)
        if user is not None:
            await self._async_ensure_group(client_id, user)

    # ── direct attempts (best effort) ────────────────────────────────────

    @callback
    def async_direct_attempt(self, user_id: str, entity_id: str | None) -> None:
        """Home Assistant rejected a direct action of a user (from its log)."""
        client_id = self.client_of_user(user_id)
        if (
            client_id is None
            or self._applied_groups.get(client_id) != GROUP_ID_READ_ONLY
        ):
            return
        now = dt_util.utcnow()
        attempts = self._direct.setdefault(client_id, deque())
        while attempts and now - attempts[0] > DIRECT_WINDOW:
            attempts.popleft()
        attempts.append(now)
        quiet = self._direct_quiet_until.get(client_id)
        if len(attempts) < DIRECT_THRESHOLD or (quiet is not None and now < quiet):
            return
        self.events.record(
            "direct_access_attempt",
            client_id=client_id,
            entity_id=entity_id,
            count=len(attempts),
            since=attempts[0],
            reliability="best_effort",
        )
        self._direct_quiet_until[client_id] = now + DIRECT_WINDOW
        attempts.clear()

    @callback
    def _async_service_called(self, event: Event) -> None:
        """A device user called an action without a target (detection only).

        Home Assistant runs actions without an entity (``mqtt.publish``,
        ``rest_command.*``, ``shell_command.*``) for every user, read-only
        ones included, and offers no way to refuse them. They are reported
        like direct attempts: an event for the hub and a repair naming the
        device and the action.
        """
        context = event.context
        user_id = context.user_id
        domain = event.data.get("domain")
        service = event.data.get("service")
        if (
            user_id is None
            or not isinstance(domain, str)
            or not isinstance(service, str)
        ):
            return
        client_id = self._device_of_user(user_id)
        if client_id is None:
            return
        now = dt_util.utcnow()
        if self._expected(context.id, now) or (
            context.parent_id is not None and self._expected(context.parent_id, now)
        ):
            return  # a step of a script or scene the device started
        data = event.data.get("service_data")
        if (
            (domain, service) in DEVICE_ACTIONS
            or self.allowlist.rule(domain, service)
            or (
                self._entity_domain(domain)
                and isinstance(data, dict)
                and any(data.get(k) for k in _TARGET_KEYS)
            )
        ):
            # Expected (Home Assistant checks the entity rights of calls
            # with a target of an entity domain): what runs in its context
            # is not reported.
            self._expect(context.id, now)
            return
        self.async_service_attempt(client_id, domain, service)

    def _entity_domain(self, domain: str) -> bool:
        """A domain whose actions with a target are entity actions (Home
        Assistant checks the user's entity rights). Elsewhere a target key is
        just data: ``shell_command`` and ``rest_command`` take any field."""
        return (
            domain in self.allowlist.domains
            or domain in self.allowlist.read_only_domains
            or domain == "homeassistant"
        )

    def _expected(self, context_id: str, now: datetime) -> bool:
        until = self._expected_contexts.get(context_id)
        return until is not None and now < until

    def _expect(self, context_id: str, now: datetime) -> None:
        """Remember an expected call's context (bounded, oldest first out)."""
        contexts = self._expected_contexts
        contexts.pop(context_id, None)
        contexts[context_id] = now + _CONTEXT_TTL
        while len(contexts) > _CONTEXTS_KEPT or (
            contexts and next(iter(contexts.values())) <= now
        ):
            del contexts[next(iter(contexts))]

    def _device_of_user(self, user_id: str) -> str | None:
        """Device the integration created this HA user for (its own mapping)."""
        if (client_id := self.client_of_user(user_id)) is not None:
            return client_id
        for client_id, held in self._held.items():
            if held == user_id:
                return client_id
        coordinator = self.coordinator()
        device_users = coordinator.device_users if coordinator is not None else None
        return device_users.client_id_of_user(user_id) if device_users else None

    @callback
    def async_service_attempt(self, client_id: str, domain: str, service: str) -> None:
        """Report an action without a target called by a device user.

        The alarm (``direct_access_attempt``) shares one throttle per device
        with the direct attempts Home Assistant refuses: one burst gives one
        alarm in DIRECT_WINDOW, whichever comes first (the event counts the
        calls in the window). The repair names the latest action; it is
        updated when the action changes. Nothing is written to disk here.
        """
        now = dt_util.utcnow()
        calls = self._service_calls.setdefault(
            client_id, deque(maxlen=_SERVICE_CALLS_KEPT)
        )
        while calls and now - calls[0] > DIRECT_WINDOW:
            calls.popleft()
        calls.append(now)
        quiet = self._direct_quiet_until.get(client_id)
        alarm = quiet is None or now >= quiet
        label = f"{domain}.{service}"[:200]
        if not alarm and self._service_reported.get(client_id) == label:
            return
        self._service_reported[client_id] = label
        name = self.device_info(client_id)[0] or client_id
        _LOGGER.warning(
            "The Home Assistant user of device %s called %s; Home Assistant "
            "does not let the integration refuse actions without an entity",
            name,
            label,
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_SERVICE_CALLED}_{client_id}",
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SERVICE_CALLED,
            translation_placeholders={"device": name, "service": label},
        )
        if not alarm:
            return
        self._direct_quiet_until[client_id] = now + DIRECT_WINDOW
        self._direct.pop(client_id, None)
        self.events.record(
            "direct_access_attempt",
            client_id=client_id,
            action=service if _ACTION_NAME.fullmatch(service) else None,
            count=len(calls),
            since=calls[0],
            reliability="best_effort",
        )

    # ── diagnostics ──────────────────────────────────────────────────────

    def diagnostics(self) -> dict[str, Any]:
        """Secret-free state for the diagnostics download."""
        return {
            "allowlist_version": self.allowlist.version,
            "devices": {
                client_id: {
                    "rules_hash": snap["rules_hash"],
                    "control_path": snap["control_path"],
                    "remote_access": snap["remote_access"],
                    "child_protection": snap["child_protection"],
                    "entities": len(snap["entities"]),
                    "group": GROUP_NAMES.get(
                        self._applied_groups.get(client_id, ""), None
                    ),
                }
                for client_id, snap in self.snapshots.items()
            },
            "events_queued": self.events.queued,
            "events_dropped": self.events.dropped,
            "events_sent": self.events.sent,
            "last_outage": self.last_outage,
            "direct_attempt_watcher": self._handler is not None,
        }


def _active(client: dict[str, Any] | None) -> bool:
    """An active device of the status (same rule as the device user reconcile)."""
    return (
        client is not None
        and client.get("is_active", True) is not False
        and not client.get("pending_deletion")
    )


def _plain(user: User) -> bool:
    """Not owner, not system-generated, no login credentials."""
    return not (user.is_owner or user.system_generated or user.credentials)
