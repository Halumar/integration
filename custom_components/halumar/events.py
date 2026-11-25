"""Events of the integration for the hub log and Home Assistant.

Every event of the taxonomy (contracts data/events.json, bundled as
event_kinds.json) that originates in the integration is

* fired at once on the HA event bus as ``<domain>_event`` (and, for alarm
  kinds, as ``<domain>_alarm``) so owners can build their own automations;
* queued for the hub (``POST /api/integration/events``), batched, at most
  500 in memory while the hub is unreachable (oldest dropped and counted).

Events the hub creates (pairing, guests, profiles) arrive as the push
message ``ha_event`` and are only fired here, as does a test of any kind
("Send test" of a notification rule, fired with ``test: true``). Data minimisation: an event
carries ids, the entity, the action name, route and reason, never action
data, codes, IP addresses or tokens.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .brand import BRAND_NAME
from .const import (
    DEVICE_USER_NAME_SEPARATOR,
    DOMAIN,
    EVENT_ALARM,
    EVENT_EVENT,
    EVENT_TAG_SCANNED,
)

if TYPE_CHECKING:
    from .coordinator import HubCoordinator

_LOGGER = logging.getLogger(__name__)

EVENT_KINDS_FILE = Path(__file__).parent / "event_kinds.json"
EVENTS_ENDPOINT = "/api/integration/events"

# Queue for the hub while it is unreachable, and batching.
MAX_QUEUE = 500
BATCH_SIZE = 100
FLUSH_INTERVAL = timedelta(seconds=10)
FLUSH_EARLY_AT = 20

# Internal event for the hub only (never fired in HA).
RULES_APPLIED = "rules_applied"
# Hub kind that is also fired as <ns>_tag_scanned.
TAG_SCANNED = "tag_scanned"

_HA_EVENT_FIELDS = (
    "client_id",
    "device_name",
    "owner_label",
    "profile_id",
    "entity_id",
    "action",
    "route",
    "reason",
    "access_reason",
    "count",
    "since",
    "until",
    "tag_id",
    "tag_name",
    "result",
)
_ALARM_FIELDS = (
    "client_id",
    "device_name",
    "entity_id",
    "reason",
    "route",
    "count",
    "since",
)
_HUB_FIELDS = (
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
)


def load_event_kinds() -> dict[str, dict[str, Any]]:
    """Kinds of the taxonomy by name (blocking: call in the executor)."""
    raw = json.loads(EVENT_KINDS_FILE.read_text(encoding="utf-8"))
    return {k["kind"]: k for k in raw["kinds"]}


def _iso(value: datetime) -> str:
    return dt_util.as_utc(value).isoformat()


def device_name_of_user(name: str | None) -> str | None:
    """Device part of a device user name '<brand> · [Guest ][owner ]device'."""
    if not name:
        return None
    prefix = f"{BRAND_NAME}{DEVICE_USER_NAME_SEPARATOR}"
    return name.removeprefix(prefix)[:100] or None


class EventReporter:
    """Fires integration events in HA and hands them to the hub."""

    def __init__(self, hass: HomeAssistant, kinds: dict[str, dict[str, Any]]) -> None:
        """Store collaborators; nothing runs before async_start."""
        self.hass = hass
        self.kinds = kinds
        self._queue: deque[dict[str, Any]] = deque()
        self.dropped = 0
        self.sent = 0
        self._flushing = False
        self._unsub: CALLBACK_TYPE | None = None
        # Returns the coordinator of the loaded entry (None while not loaded).
        self.coordinator_getter: Any = lambda: None
        # Display data of a device: client_id -> (device_name, owner_label).
        self.device_info: Any = lambda client_id: (None, None)

    @property
    def queued(self) -> int:
        """Events waiting for the hub."""
        return len(self._queue)

    @callback
    def async_start(self) -> None:
        """Start the periodic flush."""
        if self._unsub is None:
            self._unsub = async_track_time_interval(
                self.hass, self._async_tick, FLUSH_INTERVAL, cancel_on_shutdown=True
            )

    @callback
    def async_stop(self) -> None:
        """Stop the periodic flush."""
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def record(self, event_type: str, **fields: Any) -> dict[str, Any]:
        """Record one integration event: fire it in HA and queue it for the hub."""
        at = fields.pop("at", None) or dt_util.utcnow()
        event: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "type": event_type,
            "at": _iso(at),
        }
        for key in _HUB_FIELDS:
            value = fields.get(key)
            if value is not None:
                event[key] = _iso(value) if isinstance(value, datetime) else value
        if len(self._queue) >= MAX_QUEUE:
            self._queue.popleft()
            self.dropped += 1
        self._queue.append(event)
        if event_type != RULES_APPLIED:
            self._fire(event_type, event, fields.get("until"))
        if len(self._queue) >= FLUSH_EARLY_AT:
            self._schedule_flush()
        return event

    @callback
    def async_fire_hub_event(self, data: Any) -> bool:
        """Fire an event the hub sent (ws ha_event); False if it is not valid.

        Kinds with source hub are fired as they come. A test ("Send test" of
        a notification rule, ``test: true``) may carry any kind of the
        taxonomy and is fired with ``test: true`` so automations can be tried
        out and can filter tests.
        """
        if not isinstance(data, dict):
            return False
        kind = self.kinds.get(data.get("kind"))  # type: ignore[arg-type]
        test = data.get("test") is True
        if kind is None or (kind["source"] != "hub" and not test):
            return False
        if data.get("category") != kind["category"]:
            return False
        at = data.get("at")
        if not isinstance(at, str) or dt_util.parse_datetime(at) is None:
            return False
        payload: dict[str, Any] = {
            "kind": kind["kind"],
            "category": kind["category"],
            "at": at,
        }
        for key in _HA_EVENT_FIELDS:
            value = data.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                payload[key] = value[:255] if isinstance(value, str) else value
        if test:
            payload["test"] = True
        self.hass.bus.async_fire(EVENT_EVENT, payload)
        if kind["kind"] == TAG_SCANNED:
            self.hass.bus.async_fire(EVENT_TAG_SCANNED, self._tag_payload(payload))
        if kind["alarm"]:
            alarm = self._alarm_payload(kind["kind"], payload)
            if test:
                alarm["test"] = True
            self.hass.bus.async_fire(EVENT_ALARM, alarm)
        return True

    def _fire(
        self, kind_name: str, event: dict[str, Any], until: datetime | None
    ) -> None:
        kind = self.kinds.get(kind_name)
        if kind is None:
            return
        client_id = event.get("client_id")
        device_name, owner_label = (
            self.device_info(client_id) if client_id else (None, None)
        )
        payload: dict[str, Any] = {
            "kind": kind_name,
            "category": kind["category"],
            "at": event["at"],
            "client_id": client_id,
            "device_name": device_name,
            "owner_label": owner_label,
            "entity_id": event.get("entity_id"),
            "action": event.get("action"),
            "route": event.get("route"),
            "reason": event.get("reason"),
        }
        for key in ("count", "since"):
            if key in event:
                payload[key] = event[key]
        if until is not None:
            payload["until"] = _iso(until)
        self.hass.bus.async_fire(EVENT_EVENT, payload)
        if kind["alarm"]:
            self.hass.bus.async_fire(
                EVENT_ALARM, self._alarm_payload(kind_name, payload)
            )

    @staticmethod
    def _tag_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """<ns>_tag_scanned from the <ns>_event data of tag_scanned."""
        tag: dict[str, Any] = {
            key: payload.get(key)
            for key in ("tag_id", "tag_name", "result", "client_id", "device_name")
        }
        tag["at"] = payload["at"]
        if payload.get("test"):
            tag["test"] = True
        return tag

    @staticmethod
    def _alarm_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        alarm = {"kind": kind, "at": payload["at"]}
        for key in _ALARM_FIELDS:
            if key in ("count", "since") and key not in payload:
                continue
            alarm[key] = payload.get(key)
        return alarm

    # ── delivery to the hub ─────────────────────────────────────────────

    @callback
    def _schedule_flush(self) -> None:
        if not self._flushing and self.coordinator_getter() is not None:
            self.hass.async_create_background_task(
                self.async_flush(), name=f"{DOMAIN}-events-flush"
            )

    async def _async_tick(self, _now: datetime) -> None:
        if self._queue:
            await self.async_flush()

    async def async_flush(self) -> None:
        """Send queued events in batches; keep them when the hub fails."""
        coordinator: HubCoordinator | None = self.coordinator_getter()
        if coordinator is None or self._flushing:
            return
        self._flushing = True
        try:
            while self._queue:
                batch = list(self._queue)[:BATCH_SIZE]
                try:
                    await coordinator.api_post_json(
                        EVENTS_ENDPOINT, {"events": batch, "dropped": self.dropped}
                    )
                except Exception as err:  # noqa: BLE001 - retried on the next tick
                    _LOGGER.debug("Events not delivered, kept for later: %s", err)
                    return
                sent_ids = {e["id"] for e in batch}
                self._queue = deque(e for e in self._queue if e["id"] not in sent_ids)
                self.sent += len(batch)
                self.dropped = 0
        finally:
            self._flushing = False
