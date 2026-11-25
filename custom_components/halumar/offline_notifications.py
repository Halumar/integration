"""Notifications while the hub is unreachable.

display_notification, clear_notifications and ack_notifications normally go
to the hub. When it does not answer (connection error, timeout, HTTP
502/503/504), the integration applies the change itself to the device's
notification sensor (``sensor.<domain>_client_<slug>_<uid8>_notifications``,
the apps' notification source away from the hub) and keeps it in a
persistent queue. Once the hub answers again the queue is handed over in
order (notifications through POST /api/integration/notifications/handover,
clears and acknowledgements through their usual endpoints); from then on the
hub is the source of truth again.

Duplicates cannot happen: every notification the integration sends to one
device carries a UUID chosen here, the hub stores it under that id and treats
the same id again as a duplicate. While a notification waits for the
handover, its id is listed in the sensor attribute ``pending_handover`` so the
hub's own sensor update keeps it (the apps take the sensor as the removal
authority). Bounds as on the hub: at most 50 queued notifications per device,
none older than 72 hours.

Watches are treated as on the hub: a watch has no hub channel and shows the
notifications of the phone that set it up. "all" leaves watches out, and a
notification for a watch goes into its phone's sensor (platform and set_up_by
from the hub snapshot).
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
import unicodedata
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiohttp

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .brand import BRAND_NAME
from .const import DOMAIN
from .coordinator import HubApiError

if TYPE_CHECKING:
    from .coordinator import HubCoordinator

_LOGGER = logging.getLogger(__name__)

DATA_OFFLINE_NOTIFICATIONS: HassKey[OfflineNotifications] = HassKey(
    f"{DOMAIN}_offline_notifications"
)

STORAGE_KEY = f"{DOMAIN}.offline_notifications"
STORAGE_VERSION = 1

# Same bounds as the hub (notification list of a device, clean-up age).
MAX_QUEUED_PER_CLIENT = 50
MAX_SENSOR_ITEMS = 50
MAX_AGE = timedelta(hours=72)
# Items per handover / ack request (the hub accepts 1-50).
BATCH_SIZE = 50
# Platforms without a hub channel (contracts $defs/platform).
WATCH_PLATFORMS = frozenset({"watchos", "wearos"})

PENDING_HANDOVER = "pending_handover"
NOTIFY_ENDPOINT = "/api/integration/notify"
HANDOVER_ENDPOINT = "/api/integration/notifications/handover"
ACK_ENDPOINT = "/api/integration/notifications/ack"

# Answers of a proxy in front of a hub that is not running.
_UNREACHABLE_STATUSES = frozenset({502, 503, 504})
# The device is gone or deactivated: a queued clear/ack is moot.
_GONE_STATUSES = frozenset({404, 422})
# The hub refuses this one request for good (invalid, too large, unknown
# device, conflict): a single item is dropped, a batch is retried item by
# item (a batch of 50 can exceed the hub's 1 MiB body limit, one item cannot).
_REFUSED_STATUSES = frozenset({400, 404, 409, 413, 422})
# Answers about the token, the network path or the load, not the item: the
# handover pauses and keeps the whole queue (also no answer at all, another
# hub held, or a proxy in front of a hub that is not running).
_PAUSE_STATUSES = frozenset({401, 403, 408, 429}) | _UNREACHABLE_STATUSES
# Any other answer of a reachable hub (5xx, other 4xx) to the same single
# item this often in a row, and for at least SET_ASIDE_AFTER_TIME, sets the
# item aside: it stays in the sensor until it expires but is no longer sent,
# so it cannot hold up the rest. Once the hub accepts a handover request
# again, set-aside items are tried once more; the same answer then sets
# such an item aside for good.
SET_ASIDE_AFTER = 5
SET_ASIDE_AFTER_TIME = timedelta(minutes=10)

_SENSOR_PREFIX = f"sensor.{DOMAIN}_client_"
_SENSOR_SUFFIX = "_notifications"
_SLUG_MAX_LEN = 48

# Service fields copied into the notification item (defaults as on the hub).
_ITEM_DEFAULTS: dict[str, Any] = {
    "notification_type": "default",
    "popup_mode": "none",
    "scheduled_at": None,
    "priority": 50,
    "color": "none",
    "action_type": "none",
    "action_target": None,
    "show_time": True,
}


def hub_unreachable(err: BaseException | None) -> bool:
    """True when ``err`` means the hub did not answer at all."""
    if isinstance(err, HubApiError):
        return err.status in _UNREACHABLE_STATUSES
    return isinstance(err, (aiohttp.ClientError, TimeoutError))


def broadcast_notification_id(broadcast_id: str, client_id: str) -> str:
    """Id of one device's notification of a broadcast ("all") with a stable id.

    UUIDv5 with the broadcast id as namespace and the client id (lowercase) as
    name, exactly as the hub derives it (contract docs/protocol.md): a
    broadcast that timed out although the hub took it is handed over as
    duplicates instead of a second notification.
    """
    return str(uuid.uuid5(uuid.UUID(broadcast_id), client_id.lower()))


# Letters NFKD keeps as they are, spelled like HA's slugify (same table as
# the hub's services/ha_sensors.py).
_TRANSLITERATE = str.maketrans(
    {
        "ß": "ss",
        "ẞ": "ss",
        "æ": "ae",
        "Æ": "ae",
        "œ": "oe",
        "Œ": "oe",
        "ø": "o",
        "Ø": "o",
        "đ": "d",
        "Đ": "d",
        "ð": "d",
        "Ð": "d",
        "ł": "l",
        "Ł": "l",
        "þ": "th",
        "Þ": "th",
        "ħ": "h",
        "Ħ": "h",
        "ı": "i",
        "ŋ": "ng",
        "Ŋ": "ng",
        "ŧ": "t",
        "Ŧ": "t",
    }
)


def _slugify(text: str) -> str:
    """The hub's capped slug of a device name (services/ha_sensors.py _slugify).

    No "_" at either end, also after the cap: a cut right after a separator
    made "__" in the entity id, which HA refuses.
    """
    text = unicodedata.normalize("NFKD", text.translate(_TRANSLITERATE))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text[:_SLUG_MAX_LEN].rstrip("_") or "client"


def _uid8(client_id: str) -> str:
    return re.sub(r"[^a-z0-9]", "", client_id.lower())[:8] or "0"


def notification_sensor_id(name: str, client_id: str) -> str:
    """Entity id the hub gives a device's notification sensor."""
    return f"{_SENSOR_PREFIX}{_slugify(name)}_{_uid8(client_id)}{_SENSOR_SUFFIX}"


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None or parsed.tzinfo is None:
        return None
    return parsed


def _sort_key(item: dict[str, Any]) -> str:
    return str(item.get("created_at") or "")


def build_item(
    notification_id: str, fields: dict[str, Any], created_at: str
) -> dict[str, Any]:
    """Sensor item (push shape, contracts notification-item) of a notification."""
    item: dict[str, Any] = {
        "notification_id": notification_id,
        "title": fields.get("title") or "",
        "message": fields.get("message") or "",
    }
    for key, default in _ITEM_DEFAULTS.items():
        value = fields.get(key)
        item[key] = default if value is None else value
    item["popup"] = item["popup_mode"] != "none"
    item["created_at"] = created_at
    item["status"] = "pending"
    for key in ("icon", "icon_svg"):
        if fields.get(key):
            item[key] = fields[key]
    return item


class OfflineNotifications:
    """Persistent queue of notification changes the hub has not seen yet."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Set up the store; async_load reads it."""
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        # Ordered operations: notify / clear / ack.
        self._ops: list[dict[str, Any]] = []
        # client_id -> {"name", "active", "sensor", "platform", "phone"}:
        # devices of the last hub snapshot, so the fallback also works after
        # an HA restart while the hub is down.
        self._clients: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task[bool] | None = None
        # Why the last handover paused: the error of the request (None
        # after a complete handover). HTTP status of the hub's last answer
        # while paused (None: it did not answer), for the expiry warning.
        self.pause_error: BaseException | None = None
        # Status of the pause already logged by the services (logged once).
        self.reported_status: int | None = None
        self._last_answer: int | None = None

    # ── Persistence ──────────────────────────────────────────────────────

    async def async_load(self) -> None:
        """Read the queue and restore sensors HA lost (restart, hub down)."""
        data = await self._store.async_load() or {}
        ops = data.get("ops")
        clients = data.get("clients")
        self._ops = (
            [op for op in ops if isinstance(op, dict)] if isinstance(ops, list) else []
        )
        last_answer = data.get("last_answer")
        self._last_answer = last_answer if isinstance(last_answer, int) else None
        self._clients = (
            {k: v for k, v in clients.items() if isinstance(v, dict)}
            if isinstance(clients, dict)
            else {}
        )
        self._drop_expired()
        for client_id in self._clients_with_pending():
            if self.hass.states.get(self._sensor_id(client_id)) is None:
                self._rewrite(client_id, [])

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(
            lambda: {
                "ops": self._ops,
                "clients": self._clients,
                "last_answer": self._last_answer,
            },
            1,
        )

    @property
    def has_ops(self) -> bool:
        """Whether something still waits to be handed over (set-aside items
        do not: they are no longer sent)."""
        return any(not op.get("set_aside") for op in self._ops)

    @property
    def ops(self) -> list[dict[str, Any]]:
        """Copy of the queue (tests, diagnostics)."""
        return copy.deepcopy(self._ops)

    # ── Devices ──────────────────────────────────────────────────────────

    @callback
    def remember_clients(self, data: dict[str, Any] | None) -> None:
        """Keep the device list of a successful hub snapshot."""
        if not data:
            return
        clients: dict[str, dict[str, Any]] = {}
        for client in data.get("clients") or []:
            client_id = client.get("id")
            if not isinstance(client_id, str) or not client_id:
                continue
            previous = self._clients.get(client_id, {})
            name = str(client.get("name") or "")
            platform = client.get("platform")
            set_up_by = client.get("set_up_by")
            phone = set_up_by.get("client_id") if isinstance(set_up_by, dict) else None
            clients[client_id] = {
                "name": name,
                "active": bool(client.get("is_active", True))
                and not client.get("pending_deletion"),
                "sensor": previous.get("sensor")
                if previous.get("name") == name
                else None,
                "platform": platform if isinstance(platform, str) else None,
                "phone": phone if isinstance(phone, str) else None,
            }
        if clients != self._clients:
            self._clients = clients
            self._save()

    def known(self, client_id: str) -> bool:
        """Whether the hub listed this device in its last snapshot or wrote
        its notification sensor (a device paired just before the outage)."""
        if client_id in self._clients:
            return True
        return self.hass.states.get(self._sensor_id(client_id)) is not None

    def is_watch(self, client_id: str) -> bool:
        """Whether the device is a watch (no hub channel, no notification view)."""
        return self._clients.get(client_id, {}).get("platform") in WATCH_PLATFORMS

    def active_client_ids(self) -> list[str]:
        """Devices a broadcast ("all") reaches: every active one except watches
        (they show their phone's notifications), as on the hub."""
        return [
            cid
            for cid, c in self._clients.items()
            if c.get("active") and not self.is_watch(cid)
        ]

    def recipient(self, client_id: str) -> str | None:
        """The device that shows notifications for ``client_id``.

        A watch's notifications go to the phone that set it up, like on the
        hub; None when that phone is no longer paired and active.
        """
        if not self.is_watch(client_id):
            return client_id
        phone_id = self._clients.get(client_id, {}).get("phone")
        if not isinstance(phone_id, str):
            return None
        phone = self._clients.get(phone_id)
        if phone is None or not phone.get("active") or self.is_watch(phone_id):
            return None
        return phone_id

    def _sensor_id(self, client_id: str) -> str:
        """The device's notification sensor (as written by the hub)."""
        client = self._clients.get(client_id, {})
        suffix = f"_{_uid8(client_id)}{_SENSOR_SUFFIX}"
        sensor = client.get("sensor")
        if isinstance(sensor, str) and self.hass.states.get(sensor) is not None:
            return sensor
        registry = er.async_get(self.hass)
        for entity_id in self.hass.states.async_entity_ids("sensor"):
            if (
                entity_id.startswith(_SENSOR_PREFIX)
                and entity_id.endswith(suffix)
                and registry.async_get(entity_id) is None
            ):
                if client:
                    client["sensor"] = entity_id
                return entity_id
        return notification_sensor_id(str(client.get("name") or ""), client_id)

    # ── Sensor writes ────────────────────────────────────────────────────

    def _queued_notifies(self, client_id: str) -> list[dict[str, Any]]:
        return [
            op
            for op in self._ops
            if op.get("op") == "notify" and op.get("client_id") == client_id
        ]

    def _clients_with_pending(self) -> list[str]:
        return list(
            dict.fromkeys(
                op["client_id"] for op in self._ops if op.get("op") == "notify"
            )
        )

    @callback
    def _rewrite(
        self,
        client_id: str,
        items: list[dict[str, Any]] | None,
        *,
        drop: set[str] | None = None,
        acked: set[str] | None = None,
    ) -> None:
        """Write the sensor: current items (or ``items``), queued ones on top."""
        entity_id = self._sensor_id(client_id)
        state = self.hass.states.get(entity_id)
        if items is None:
            current = state.attributes.get("notifications") if state else None
            items = (
                [copy.deepcopy(i) for i in current if isinstance(i, dict)]
                if isinstance(current, list)
                else []
            )
        queued = {op["notification_id"]: op for op in self._queued_notifies(client_id)}
        drop = drop or set()
        items = [
            i
            for i in items
            if i.get("notification_id") not in queued
            and i.get("notification_id") not in drop
        ]
        for op in queued.values():
            item = copy.deepcopy(op["item"])
            item["status"] = op.get("status", "pending")
            items.append(item)
        for item in items:
            if acked and item.get("notification_id") in acked:
                item["status"] = "delivered"
        items.sort(key=_sort_key, reverse=True)
        items = items[:MAX_SENSOR_ITEMS]
        name = self._clients.get(client_id, {}).get("name") or client_id
        attributes: dict[str, Any] = {
            "friendly_name": (
                state.attributes.get("friendly_name")
                if state and state.attributes.get("friendly_name")
                else f"{BRAND_NAME}: {name} Notifications"
            ),
            "icon": "mdi:bell-badge" if items else "mdi:bell-outline",
            "count": len(items),
            "notifications": items,
            "updated_at": dt_util.utcnow().isoformat(),
        }
        pending = [
            i["notification_id"] for i in items if i.get("notification_id") in queued
        ]
        if pending:
            attributes[PENDING_HANDOVER] = pending
        self.hass.states.async_set(entity_id, str(len(items)), attributes)

    # ── Queue operations (hub unreachable) ───────────────────────────────

    async def async_notify(
        self, client_ids: list[str], fields: dict[str, Any], ids: dict[str, str]
    ) -> list[str]:
        """Queue a notification for each device and show it in its sensor.

        ``ids`` holds ids already used for this notification (sent to the
        hub before it stopped answering); other devices get a new UUID.
        Returns the notification ids.
        """
        created_at = dt_util.utcnow().isoformat()
        result: list[str] = []
        async with self._lock:
            for client_id in client_ids:
                notification_id = ids.get(client_id) or str(uuid.uuid4())
                self._ops.append(
                    {
                        "op": "notify",
                        "client_id": client_id,
                        "notification_id": notification_id,
                        "created_at": created_at,
                        "status": "pending",
                        "fields": fields,
                        "item": build_item(notification_id, fields, created_at),
                    }
                )
                dropped = self._enforce_limit(client_id)
                self._rewrite(client_id, None, drop=dropped)
                result.append(notification_id)
            self._save()
        return result

    def _enforce_limit(self, client_id: str) -> set[str]:
        queued = self._queued_notifies(client_id)
        excess = queued[: max(0, len(queued) - MAX_QUEUED_PER_CLIENT)]
        if excess:
            _LOGGER.warning(
                "More than %d notifications queued for device %s while the hub "
                "is unreachable; dropping the %d oldest",
                MAX_QUEUED_PER_CLIENT,
                client_id,
                len(excess),
            )
        for op in excess:
            self._ops.remove(op)
        return {op["notification_id"] for op in excess}

    async def async_clear(self, client_id: str) -> None:
        """Queue a clear of the device and empty its sensor."""
        async with self._lock:
            # Queued notifications and acks of the device are moot now.
            self._ops = [
                op
                for op in self._ops
                if op.get("client_id") != client_id or op.get("op") == "clear"
            ]
            self._ops.append(
                {
                    "op": "clear",
                    "client_id": client_id,
                    "created_at": dt_util.utcnow().isoformat(),
                }
            )
            self._rewrite(client_id, [])
            self._save()

    async def async_cleared_on_hub(self, client_id: str) -> None:
        """The hub cleared the device directly: its set-aside changes are moot
        (a later probe must not bring a cleared notification back)."""
        async with self._lock:
            moot = [op for op in self._ops if op.get("client_id") == client_id]
            if not moot:
                return
            for op in moot:
                self._ops.remove(op)
            dropped = {op["notification_id"] for op in moot if op.get("op") == "notify"}
            if dropped:
                self._rewrite(client_id, None, drop=dropped)
            self._save()

    async def async_acked_on_hub(
        self, client_id: str, notification_ids: list[str]
    ) -> None:
        """The hub took an acknowledgement directly: set-aside notifications
        among the ids are handed over as delivered, never as pending again."""
        async with self._lock:
            acked = {
                op["notification_id"]
                for op in self._queued_notifies(client_id)
                if op["notification_id"] in notification_ids
                and op.get("status") == "pending"
            }
            if not acked:
                return
            for op in self._queued_notifies(client_id):
                if op["notification_id"] in acked:
                    op["status"] = "delivered"
            self._rewrite(client_id, None, acked=acked)
            self._save()

    async def async_ack(
        self, client_id: str, notification_ids: list[str]
    ) -> tuple[list[str], list[str]]:
        """Confirm delivery while the hub is unreachable.

        Queued notifications are handed over as delivered; pending ones the
        hub wrote before it went down get a queued ack. Like the hub, only
        pending notifications of this device count; everything else is
        skipped. Returns (acked, skipped).
        """
        async with self._lock:
            queued = {
                op["notification_id"]: op for op in self._queued_notifies(client_id)
            }
            state = self.hass.states.get(self._sensor_id(client_id))
            current = state.attributes.get("notifications") if state else None
            pending_in_sensor = {
                i.get("notification_id")
                for i in (current if isinstance(current, list) else [])
                if isinstance(i, dict) and i.get("status") == "pending"
            }
            acked: list[str] = []
            skipped: list[str] = []
            hub_ids: list[str] = []
            for notification_id in dict.fromkeys(notification_ids):
                op = queued.get(notification_id)
                if op is not None:
                    if op.get("status") == "pending":
                        op["status"] = "delivered"
                        acked.append(notification_id)
                    else:
                        skipped.append(notification_id)
                elif notification_id in pending_in_sensor:
                    hub_ids.append(notification_id)
                    acked.append(notification_id)
                else:
                    skipped.append(notification_id)
            if hub_ids:
                self._queue_ack(client_id, hub_ids)
            if acked:
                self._rewrite(client_id, None, acked=set(acked))
                self._save()
        return acked, skipped

    def _queue_ack(self, client_id: str, ids: list[str]) -> None:
        last = self._ops[-1] if self._ops else None
        if (
            last
            and last.get("op") == "ack"
            and last.get("client_id") == client_id
            and len(last["notification_ids"]) + len(ids) <= BATCH_SIZE
        ):
            last["notification_ids"].extend(
                i for i in ids if i not in last["notification_ids"]
            )
            return
        for start in range(0, len(ids), BATCH_SIZE):
            self._ops.append(
                {
                    "op": "ack",
                    "client_id": client_id,
                    "notification_ids": ids[start : start + BATCH_SIZE],
                    "created_at": dt_util.utcnow().isoformat(),
                }
            )

    def _drop_expired(self) -> None:
        oldest = dt_util.utcnow() - MAX_AGE
        expired = [
            op
            for op in self._ops
            if (created := _parse_time(op.get("created_at"))) is None
            or created < oldest
        ]
        if not expired:
            return
        causes: dict[str, int] = {}
        for op in expired:
            aside = op.get("set_aside")
            if isinstance(aside, dict):
                cause = f"the hub answered HTTP {aside.get('status')} to it every time"
            elif self._last_answer is not None:
                cause = f"the hub kept answering HTTP {self._last_answer}"
            else:
                cause = "the hub was not reachable in time"
            causes[cause] = causes.get(cause, 0) + 1
        for cause, count in causes.items():
            _LOGGER.warning(
                "Dropping %d queued notification change(s) older than %d hours: %s",
                count,
                MAX_AGE.total_seconds() // 3600,
                cause,
            )
        for op in expired:
            self._ops.remove(op)
        for client_id in {
            op["client_id"] for op in expired if op.get("op") == "notify"
        }:
            if self.hass.states.get(self._sensor_id(client_id)) is not None:
                self._rewrite(
                    client_id,
                    None,
                    drop={
                        op["notification_id"]
                        for op in expired
                        if op.get("op") == "notify" and op["client_id"] == client_id
                    },
                )
        self._save()

    # ── Handover ─────────────────────────────────────────────────────────

    @callback
    def async_schedule_flush(self, coordinator: HubCoordinator) -> None:
        """Hand the queue over in the background (one run at a time)."""
        if not (self.has_ops or self._probe_due() is not None) or (
            self._flush_task and not self._flush_task.done()
        ):
            return
        self._flush_task = coordinator.config_entry.async_create_background_task(
            self.hass, self.async_flush(coordinator), name=f"{DOMAIN}-notify-handover"
        )

    async def async_flush(self, coordinator: HubCoordinator) -> bool:
        """Hand over everything in order; True when nothing is left to send.

        Pauses (keeps the rest, ``pause_error`` says why) when the hub does
        not answer, another hub is held, or the answer is about the token or
        the load (401, 403, 408, 429, 502-504). A batch the hub refuses otherwise is
        retried item by item. A single item the hub refuses for good (400,
        404, 409, 413, 422) is dropped; one that keeps getting another
        answer (5xx) pauses the handover until it is set aside
        (SET_ASIDE_AFTER).
        """
        async with self._lock:
            self._drop_expired()
            if not self.has_ops:
                await self._async_probe(coordinator)
            handed_over = False
            batch_size = BATCH_SIZE
            while pending := [op for op in self._ops if not op.get("set_aside")]:
                op = pending[0]
                if op.get("op") == "notify":
                    batch: list[dict[str, Any]] = []
                    for candidate in pending:
                        if candidate.get("op") != "notify" or len(batch) >= batch_size:
                            break
                        batch.append(candidate)
                    request = (HANDOVER_ENDPOINT, self._handover_body(batch))
                else:
                    batch = [op]
                    request = self._single_request(op)
                try:
                    result = await coordinator.api_post_json(*request)
                except HubApiError as err:
                    if err.status is None or err.status in _PAUSE_STATUSES:
                        return self._pause(err)
                    if len(batch) > 1:
                        # One item must not cost the others (or the whole
                        # batch was too large): retry item by item.
                        batch_size = 1
                        continue
                    if err.status not in _REFUSED_STATUSES:
                        if not self._failed(op, err):
                            return self._pause(err)
                        self._rewrite(op["client_id"], None)
                        self._save()
                        continue
                    self._log_refused(op, err)
                    refused = op if op.get("op") == "notify" else None
                except (aiohttp.ClientError, TimeoutError) as err:
                    _LOGGER.debug("Handover paused, hub not reachable: %s", err)
                    self.pause_error = err
                    self._last_answer = None
                    return False
                else:
                    self._log_result(op, batch, result)
                    refused = None
                    self._retry_set_aside()
                for done in batch:
                    self._ops.remove(done)
                if refused is not None:
                    self._rewrite(
                        refused["client_id"], None, drop={refused["notification_id"]}
                    )
                handed_over = True
                self._save()
            self.pause_error = None
            self.reported_status = None
            if self._last_answer is not None:
                self._last_answer = None
                self._save()
            if handed_over:
                _LOGGER.info("All queued notification changes handed over to the hub")
            return True

    def _pause(self, err: HubApiError) -> bool:
        _LOGGER.debug("Handover paused: %s", err)
        self.pause_error = err
        if err.status is not None and err.status != self._last_answer:
            self._last_answer = err.status
            self._save()
        return False

    def _failed(self, op: dict[str, Any], err: HubApiError) -> bool:
        """Count another answer ``err`` to the single item ``op``; True when
        it is set aside now (the same answer SET_ASIDE_AFTER times in a row,
        the first at least SET_ASIDE_AFTER_TIME ago)."""
        now = dt_util.utcnow()
        if op.get("retried") == err.status:
            # Tried once more after the hub recovered: same answer, for good.
            op["set_aside"] = {"status": err.status, "at": now.isoformat()}
            _LOGGER.warning(
                "The hub answered HTTP %s again to the queued %s for device %s "
                "after recovering; it is no longer sent",
                err.status,
                op.get("op"),
                op.get("client_id"),
            )
            return True
        failures = op.get("failures")
        if not isinstance(failures, dict) or failures.get("status") != err.status:
            failures = {"status": err.status, "count": 0, "since": now.isoformat()}
        failures["count"] = int(failures.get("count") or 0) + 1
        op["failures"] = failures
        since = _parse_time(failures.get("since")) or now
        if failures["count"] < SET_ASIDE_AFTER or now - since < SET_ASIDE_AFTER_TIME:
            self._save()
            return False
        op["set_aside"] = {"status": err.status, "at": now.isoformat()}
        _LOGGER.warning(
            "The hub answered HTTP %s to the queued %s for device %s %d times "
            "since %s; it is no longer sent (it stays on the device until it "
            "expires) so the rest can be handed over",
            err.status,
            op.get("op"),
            op.get("client_id"),
            failures["count"],
            failures["since"],
        )
        return True

    def _probe_due(self) -> dict[str, Any] | None:
        """The oldest set-aside item not yet retried, when nothing else waits
        and it was set aside at least SET_ASIDE_AFTER_TIME ago."""
        if self.has_ops:
            return None
        oldest = dt_util.utcnow() - SET_ASIDE_AFTER_TIME
        candidates = [
            op
            for op in self._ops
            if isinstance(op.get("set_aside"), dict)
            and "retried" not in op
            and (_parse_time(op["set_aside"].get("at")) or oldest) <= oldest
        ]
        return min(
            candidates, key=lambda op: str(op["set_aside"].get("at")), default=None
        )

    async def _async_probe(self, coordinator: HubCoordinator) -> bool:
        """Only set-aside items are left: send the oldest due one as a probe.

        Taken: it is handed over and the other set-aside items get their one
        more try (True). Any other outcome sets it aside again from now
        (refused for good: dropped), nothing else changes (False).
        """
        op = self._probe_due()
        if op is None:
            return False
        if op.get("op") == "notify":
            request = (HANDOVER_ENDPOINT, self._handover_body([op]))
        else:
            request = self._single_request(op)
        try:
            await coordinator.api_post_json(*request)
        except HubApiError as err:
            if err.status in _REFUSED_STATUSES:
                self._log_refused(op, err)
                self._ops.remove(op)
                if op.get("op") == "notify":
                    self._rewrite(op["client_id"], None, drop={op["notification_id"]})
            else:
                op["set_aside"]["at"] = dt_util.utcnow().isoformat()
            self._save()
            return False
        except (aiohttp.ClientError, TimeoutError):
            op["set_aside"]["at"] = dt_util.utcnow().isoformat()
            self._save()
            return False
        _LOGGER.info(
            "The hub took a queued %s for device %s it had refused before; "
            "the other set-aside changes are tried once more",
            op.get("op"),
            op.get("client_id"),
        )
        self._ops.remove(op)
        self._retry_set_aside()
        self._save()
        return True

    def _retry_set_aside(self) -> None:
        """The hub took a request: set-aside items get one more try each."""
        for op in self._ops:
            aside = op.get("set_aside")
            if isinstance(aside, dict) and "retried" not in op:
                op.pop("set_aside")
                op.pop("failures", None)
                op["retried"] = aside.get("status")

    @staticmethod
    def _handover_body(batch: list[dict[str, Any]]) -> dict[str, Any]:
        notifications = []
        for op in batch:
            item = {k: v for k, v in op["fields"].items() if v is not None}
            item.update(
                client_id=op["client_id"],
                notification_id=op["notification_id"],
                created_at=op["created_at"],
                status=op.get("status", "pending"),
            )
            notifications.append(item)
        return {"notifications": notifications}

    @staticmethod
    def _single_request(op: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        client_id = op["client_id"]
        if op.get("op") == "clear":
            return f"/api/integration/clients/{client_id}/clear_notifications", {}
        return ACK_ENDPOINT, {
            "client_id": client_id,
            "notification_ids": op["notification_ids"],
        }

    @staticmethod
    def _log_refused(op: dict[str, Any], err: HubApiError) -> None:
        level = logging.INFO if err.status in _GONE_STATUSES else logging.WARNING
        _LOGGER.log(
            level,
            "The hub refused a queued %s for device %s (%s); dropped",
            op.get("op"),
            op.get("client_id"),
            err.detail,
        )

    @staticmethod
    def _log_result(
        op: dict[str, Any], batch: list[dict[str, Any]], result: dict[str, Any]
    ) -> None:
        if op.get("op") != "notify":
            _LOGGER.info(
                "Handed a queued %s for device %s over to the hub",
                op.get("op"),
                op.get("client_id"),
            )
            return
        skipped = result.get("skipped") or []
        _LOGGER.info(
            "Handed %d queued notification(s) over to the hub (%d new, %d already "
            "known, %d skipped)",
            len(batch),
            len(result.get("created") or []),
            len(result.get("duplicates") or []),
            len(skipped),
        )
        for entry in skipped:
            if isinstance(entry, dict):
                _LOGGER.warning(
                    "The hub skipped queued notification %s: %s",
                    entry.get("notification_id"),
                    entry.get("reason"),
                )


async def async_setup_offline_notifications(hass: HomeAssistant) -> None:
    """Create and load the queue once per HA instance."""
    if DATA_OFFLINE_NOTIFICATIONS in hass.data:
        return
    queue = OfflineNotifications(hass)
    hass.data[DATA_OFFLINE_NOTIFICATIONS] = queue
    await queue.async_load()
