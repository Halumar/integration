"""DataUpdateCoordinator for Halumar integration.

Polls the REST API of the Halumar Hub periodically to fetch the full status
snapshot (hub info, areas, clients). All platform entities read their
state from the coordinator's data dict.

Fires HA events on state changes:
- halumar_client_connected: when a client connects
- halumar_client_disconnected: when a client disconnects
- halumar_client_area_changed: when a client's current area changes
- halumar_pairing_started: when pairing mode activates ({is_guest})
- halumar_pairing_stopped: when pairing mode deactivates
- halumar_guest_expired: when a guest client disappears (expired/kicked)
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from collections.abc import Awaitable, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import aiohttp
from multidict import CIMultiDict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .const import (
    CLIENT_DEVICE_ID_PREFIX,
    CONF_HUB_INSTANCE_ID,
    DEFAULT_USE_SSL,
    DOMAIN,
    MIN_ADDON_VERSION,
    QR_MODE_OFF,
    QR_MODES,
    UPDATE_INTERVAL,
)
from .snapshot import is_uuid
from .token_renewal import async_token_after_rejection

if TYPE_CHECKING:
    from .device_users import DeviceUserManager

_LOGGER = logging.getLogger(__name__)

type HubConfigEntry = ConfigEntry[HubCoordinator]

# Event type constants
EVENT_CLIENT_CONNECTED = f"{DOMAIN}_client_connected"
EVENT_CLIENT_DISCONNECTED = f"{DOMAIN}_client_disconnected"
EVENT_CLIENT_AREA_CHANGED = f"{DOMAIN}_client_area_changed"
EVENT_PAIRING_STARTED = f"{DOMAIN}_pairing_started"
EVENT_PAIRING_STOPPED = f"{DOMAIN}_pairing_stopped"
EVENT_GUEST_EXPIRED = f"{DOMAIN}_guest_expired"

# Upper bound on how many clients / areas we process from a single poll.
# A malformed or oversized hub response must not be able to spawn an
# unbounded number of entities/devices; anything beyond this is truncated
# (and logged) rather than processed silently.
MAX_ENTITIES_PER_POLL = 500

# Per-request timeout of the REST calls (the shared HA session has none of
# its own that fits a LAN hub).
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Repair issue raised while the hub is older than MIN_ADDON_VERSION.
ISSUE_ADDON_VERSION = "addon_version_incompatible"
# Repair issue raised while the status names another hub than the entry's.
ISSUE_OTHER_HUB = "other_hub"

# Issues are linked to their config entry where HA supports it (after the
# 2025.11 minimum, which has no config_entry_id):
# HA then hides them while the entry is disabled and removes them with it.
_ISSUE_HAS_ENTRY_ID = (
    "config_entry_id" in inspect.signature(ir.async_create_issue).parameters
)

# Unique-id suffixes of the per-area entities on the hub device
# (sensor.py HubAreaSensor, switch.py HubAreaSwitch). "_hub_area_count" is
# the hub's area counter, not an area.
_AREA_SWITCH_UID = "_hub_area_switch_"
_AREA_SENSOR_UID = "_hub_area_"
_NOT_AN_AREA = frozenset({"count"})


def area_id_of_unique_id(entry_id: str, unique_id: str | None) -> str | None:
    """Area id of a per-area entity's unique id, None for any other entity."""
    if not unique_id or not unique_id.startswith(entry_id):
        return None
    rest = unique_id[len(entry_id) :]
    for prefix in (_AREA_SWITCH_UID, _AREA_SENSOR_UID):
        if rest.startswith(prefix):
            area_id = rest[len(prefix) :]
            if area_id and area_id not in _NOT_AN_AREA:
                return area_id
            return None
    return None


def update_failed(key: str, **placeholders: str) -> UpdateFailed:
    """A translated UpdateFailed."""
    return UpdateFailed(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders,
    )


# Lists every status answer must carry (required by the contract) and the
# string field that identifies each of their items (None: the item itself).
# An item without it would silently drop out, and with it its device.
_STATUS_LISTS: dict[str, str | None] = {
    "areas": "id",
    "clients": "id",
    "guests": "client_id",
    "provisioning": "client_id",
    "ha_user_holds": None,
}


def _has_id(item: Any, id_key: str | None) -> bool:
    value = item if id_key is None else isinstance(item, dict) and item.get(id_key)
    return isinstance(value, str) and bool(value)


class _TokenRejected(Exception):
    """The hub answered 401 to the integration token."""


def other_hub_issue_id(entry_id: str) -> str:
    """Issue id of the "this is a different hub" repair of one entry."""
    return f"{ISSUE_OTHER_HUB}_{entry_id}"


def hub_instance_id(data: Any) -> str | None:
    """``hub.instance_id`` of a status answer (lower case), None if absent or no uuid."""
    hub = data.get("hub") if isinstance(data, dict) else None
    iid = hub.get("instance_id") if isinstance(hub, dict) else None
    return iid.lower() if isinstance(iid, str) and is_uuid(iid) else None


def addon_issue_id(entry_id: str) -> str:
    """Issue id of the hub version repair of one config entry."""
    return f"{ISSUE_ADDON_VERSION}_{entry_id}"


class HubApiError(UpdateFailed):
    """A Halumar API call failed; carries the HTTP status when known.

    Subclasses UpdateFailed so every existing caller keeps working; the
    status lets service handlers map specific codes (e.g. 404 = unknown
    client) to actionable error messages.
    """

    def __init__(
        self,
        message: str,
        status: int | None = None,
        detail: str | None = None,
        code: str | None = None,
    ) -> None:
        """Store the HTTP status, the hub's reason and its contract code."""
        super().__init__(message)
        self.status = status
        self.detail = detail or message
        self.code = code


class OtherHubError(HubApiError):
    """Not sent: the status names another hub than the entry's (repair issue
    ``other_hub``). Nothing goes to that hub until the admin confirms it;
    callers retry later as after any failed call (status None).

    Internal: callers turn it into the translated ``other_hub_held`` error.
    """

    def __init__(self, url: str) -> None:
        """Name the URL of the hub that was not confirmed."""
        super().__init__(
            f"not sent: {url} is another hub than this entry's", detail=url
        )


def _error_code(text: str) -> str | None:
    """Contract code of an error body ({"detail": {"code": ...}}), if any."""
    try:
        body = json.loads(text)
    except ValueError:
        return None
    detail = body.get("detail") if isinstance(body, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    return code if isinstance(code, str) and len(code) <= 64 else None


def _error_detail(text: str, status: int) -> str:
    """The hub's reason from an error body ({"detail": ...}), shortened."""
    try:
        body = json.loads(text)
    except ValueError:
        body = None
    detail: Any = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("code")
    if isinstance(detail, list) and detail and isinstance(detail[0], dict):
        detail = detail[0].get("msg")
    if not isinstance(detail, str) or not detail:
        detail = text.strip() or f"HTTP {status}"
    return f"{detail[:200]} (HTTP {status})"


# HTTP statuses of the hub that mean "the caller asked for something that
# does not exist or is invalid" (unknown client/area, bad payload): reported
# as ServiceValidationError, everything else as HomeAssistantError.
_VALIDATION_STATUSES = frozenset({400, 404})


async def hub_call[T](
    action: str,
    request: Awaitable[T],
    placeholders: dict[str, str] | None = None,
    status_keys: dict[int, str] | None = None,
) -> T:
    """Await a hub request and raise a translated error on any failure.

    ``action`` names the translation key ``<action>_failed`` (exceptions in
    strings.json); its message gets ``placeholders`` plus ``detail``.
    ``status_keys`` maps specific HTTP statuses to their own keys. Services
    never swallow errors: a failed call fails the service
    call, so automations and the UI see it.
    """
    values = dict(placeholders or {})
    try:
        return await request
    except OtherHubError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="other_hub_held",
            translation_placeholders={"url": err.detail or ""},
        ) from err
    except HubApiError as err:
        if status_keys and err.status in status_keys:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key=status_keys[err.status],
                translation_placeholders=values,
            ) from err
        values["detail"] = err.detail
        error = (
            ServiceValidationError
            if err.status in _VALIDATION_STATUSES
            else HomeAssistantError
        )
        raise error(
            translation_domain=DOMAIN,
            translation_key=f"{action}_failed",
            translation_placeholders=values,
        ) from err
    except (aiohttp.ClientError, TimeoutError) as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="not_reachable",
            translation_placeholders={"error": str(err) or type(err).__name__},
        ) from err


class HubCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that polls GET /api/integration/status."""

    config_entry: HubConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: HubConfigEntry,
        host: str,
        port: int,
        api_token: str,
        use_ssl: bool = DEFAULT_USE_SSL,
    ) -> None:
        """Initialize the coordinator."""
        # config_entry is passed explicitly so the base coordinator can start
        # the reauth flow when ConfigEntryAuthFailed is raised (the implicit
        # context lookup is deprecated).
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
            config_entry=config_entry,
        )
        self._api_token = api_token
        scheme = "https" if use_ssl else "http"
        self._base_url = f"{scheme}://{host}:{port}"
        # Requests go through the shared HA session (inject-websession); the
        # credentials travel per request, never as session defaults.
        self._session = async_get_clientsession(hass)
        self._integration_version: str | None = None
        # One HA user per device, attached by async_setup_entry.
        self.device_users: DeviceUserManager | None = None
        # Start of the request that produced the current snapshot, and whether
        # its client list was truncated. The device-user reconcile never
        # deletes a user created after the snapshot was requested, nor
        # anything at all when the list was truncated.
        self.snapshot_started_at: datetime | None = None
        self.clients_truncated = False
        # The WS push background task (push.HubPushClient.async_run),
        # attached by async_setup_entry when the hub advertises ws_push.
        # async_unload_entry cancels it before the entry goes away.
        self.push_task: asyncio.Task[None] | None = None
        # Device-registry id of the hub device, registered by async_setup_entry
        # before the platforms so client devices can link to it.
        self.hub_device_id: str | None = None
        # O(1) lookup index: client_id -> client dict, rebuilt after each update
        self.client_by_id: dict[str, dict[str, Any]] = {}
        # Cached per-client compliance blobs (client_id -> {status, ts, sig?})
        # and the hub's endpoint toggle, consumed by the token-less
        # compliance view (compliance.py). Kept as dedicated attributes so
        # the view keeps serving the last known state while a refresh fails.
        self.compliance_by_id: dict[str, dict[str, Any]] = {}
        self.compliance_endpoint_enabled: bool = True
        # Instance id of another hub the status named: set while the
        # admin has not confirmed it; the push session then handles nothing.
        self.other_hub: str | None = None
        # Track previous state for event detection. The first snapshot after
        # setup (HA start, entry reload) only records the baseline: clients
        # that were already connected are not "newly connected".
        self._prev_client_states: dict[str, dict[str, Any]] = {}
        self._prev_pairing_active: bool = False
        self._event_baseline_taken = False

    @property
    def base_url(self) -> str:
        """Return the base URL of the Halumar Hub."""
        return self._base_url

    @property
    def session(self) -> aiohttp.ClientSession:
        """The shared HA client session all hub requests go through."""
        return self._session

    async def async_request_headers(self) -> dict[str, str]:
        """Headers of every hub request (REST and the WS handshake).

        Every request carries the integration version so the hub can
        expose it via its integration-status telemetry.
        """
        if self._integration_version is None:
            integration = await async_get_integration(self.hass, DOMAIN)
            self._integration_version = str(integration.version)
        return {
            "Authorization": f"Bearer {self._api_token}",
            "Content-Type": "application/json",
            "X-Hub-Integration-Version": self._integration_version,
        }

    def _fire_events(self, data: dict[str, Any]) -> None:
        """Compare new data with previous state and fire HA events on changes.

        The first snapshot after setup is the baseline: it fires nothing.
        Otherwise every HA start and entry reload (also after each hub
        restart) reported all connected clients as newly connected and an
        open pairing window as newly started.
        """
        new_client_states: dict[str, dict[str, Any]] = {}
        for client in data.get("clients", []):
            cid = client.get("id")
            if not cid:
                # A client without an id can't be tracked or addressed; skip it
                # rather than letting a KeyError break the whole update cycle.
                continue
            new_client_states[cid] = {
                "is_connected": bool(client.get("is_connected", False)),
                "current_area_id": client.get("current_area_id"),
                "current_area_name": client.get("current_area_name"),
                "name": client.get("name", ""),
                "device_name": client.get("device_name"),
                "is_guest": bool(client.get("is_guest", False)),
            }
        pairing_active = bool(data.get("pairing_active", False))

        if not self._event_baseline_taken:
            self._event_baseline_taken = True
            self._prev_client_states = new_client_states
            self._prev_pairing_active = pairing_active
            return

        # ── Client connection / area change events ───────────────────────
        for cid, state in new_client_states.items():
            prev = self._prev_client_states.get(cid)
            is_connected = state["is_connected"]
            was_connected = bool(prev and prev.get("is_connected"))

            if is_connected and not was_connected:
                self.hass.bus.async_fire(
                    EVENT_CLIENT_CONNECTED,
                    {
                        "client_id": cid,
                        "client_name": state["name"],
                        "device_name": state["device_name"],
                    },
                )
            elif was_connected and not is_connected:
                self.hass.bus.async_fire(
                    EVENT_CLIENT_DISCONNECTED,
                    {"client_id": cid, "client_name": state["name"]},
                )

            # Area changed (only for a known client that is connected)
            if prev is not None and is_connected:
                old_area = prev.get("current_area_id")
                new_area = state["current_area_id"]
                if old_area != new_area and new_area is not None:
                    self.hass.bus.async_fire(
                        EVENT_CLIENT_AREA_CHANGED,
                        {
                            "client_id": cid,
                            "client_name": state["name"],
                            "old_area_id": old_area,
                            "new_area_id": new_area,
                            "new_area_name": state["current_area_name"],
                        },
                    )

        # Clients that vanished from the snapshot
        for cid, prev in self._prev_client_states.items():
            if cid in new_client_states:
                continue
            if prev.get("is_connected"):
                self.hass.bus.async_fire(
                    EVENT_CLIENT_DISCONNECTED,
                    {"client_id": cid, "client_name": prev.get("name", "")},
                )
            # Fire guest expired event when a guest disappears entirely
            if prev.get("is_guest"):
                self.hass.bus.async_fire(
                    EVENT_GUEST_EXPIRED,
                    {"client_id": cid, "client_name": prev.get("name", "")},
                )

        self._prev_client_states = new_client_states

        # ── Pairing state events ─────────────────────────────────────────
        if pairing_active and not self._prev_pairing_active:
            # The status payload is secret-free by contract: the pairing PIN
            # never appears in it (the QR camera conveys it), so the event
            # carries only non-secret window metadata.
            self.hass.bus.async_fire(
                EVENT_PAIRING_STARTED,
                {"is_guest": data.get("pairing_is_guest", False)},
            )
        elif not pairing_active and self._prev_pairing_active:
            self.hass.bus.async_fire(EVENT_PAIRING_STOPPED, {})
        self._prev_pairing_active = pairing_active

    def _cleanup_orphans(self, data: dict[str, Any], *, truncated: bool) -> None:
        """Remove registry leftovers of clients and areas that no longer exist.

        Client devices go with their entities; area sensors and switches hang
        on the hub device and are removed one by one. Nothing is
        removed when the snapshot was truncated: a missing id then does not
        mean the client or area is gone.
        """
        if truncated:
            return
        entry_id = self.config_entry.entry_id

        dev_reg = dr.async_get(self.hass)
        current_client_ids = {
            c.get("id") for c in data.get("clients", []) if c.get("id")
        }
        for device in dr.async_entries_for_config_entry(dev_reg, entry_id):
            for identifier in device.identifiers:
                if identifier[0] == DOMAIN and identifier[1].startswith(
                    CLIENT_DEVICE_ID_PREFIX
                ):
                    client_id = identifier[1][len(CLIENT_DEVICE_ID_PREFIX) :]
                    if client_id not in current_client_ids:
                        _LOGGER.info("Removing stale device for client %s", client_id)
                        dev_reg.async_remove_device(device.id)

        ent_reg = er.async_get(self.hass)
        current_area_ids = {a.get("id") for a in data.get("areas", []) if a.get("id")}
        for entity in er.async_entries_for_config_entry(ent_reg, entry_id):
            area_id = area_id_of_unique_id(entry_id, entity.unique_id)
            if area_id is not None and area_id not in current_area_ids:
                _LOGGER.info(
                    "Removing %s of deleted area %s", entity.entity_id, area_id
                )
                ent_reg.async_remove(entity.entity_id)

    @staticmethod
    def _parse_version(version_str: str) -> tuple[int, ...]:
        """Parse a version string into a tuple for comparison.

        '0.10.9' -> (0, 10, 9)
        '2026.12.0-dev' -> (2026, 12, 0)  (pre-release: release part only)
        """
        match = re.match(r"\d+(?:\.\d+)*", version_str or "")
        if not match:
            return (0, 0, 0)
        return tuple(int(x) for x in match.group(0).split("."))

    def _check_addon_version(self, data: dict[str, Any]) -> None:
        """Raise or clear the repair issue for a hub that is too old.

        Actionable: the user updates the hub, and the issue goes away with
        the next successful poll.
        """
        hub = data.get("hub")
        addon_version = str((hub if isinstance(hub, dict) else {}).get("version", ""))
        issue_id = addon_issue_id(self.config_entry.entry_id)
        if self._parse_version(addon_version) < self._parse_version(MIN_ADDON_VERSION):
            extra: dict[str, Any] = {}
            if _ISSUE_HAS_ENTRY_ID:
                extra["config_entry_id"] = self.config_entry.entry_id
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_ADDON_VERSION,
                translation_placeholders={
                    "current_version": addon_version or "?",
                    "min_version": MIN_ADDON_VERSION,
                },
                **extra,
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    @staticmethod
    def _cap_list(items: Any, label: str) -> list[dict[str, Any]]:
        """Return items as a dict-list capped at MAX_ENTITIES_PER_POLL.

        Non-list payloads collapse to an empty list and non-dict elements are
        dropped, so all downstream consumers can rely on iterating dicts.
        Over-long lists are truncated and the truncation is logged, because
        silently dropping entries would hide a misbehaving hub.
        """
        if not isinstance(items, list):
            return []
        if len(items) > MAX_ENTITIES_PER_POLL:
            _LOGGER.warning(
                "The hub returned %d %s; processing only the first %d",
                len(items),
                label,
                MAX_ENTITIES_PER_POLL,
            )
            items = items[:MAX_ENTITIES_PER_POLL]
        return [item for item in items if isinstance(item, dict)]

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch the full status from the Halumar Hub."""
        try:
            return await self._async_fetch_status()
        except _TokenRejected:
            # The hub renews the token on every start. When Core came up
            # (or reloaded) before it processed the re-discovery, take over
            # the renewed token and retry once; reauth is the fallback.
            rejected = self._api_token
            renewed, announced = await async_token_after_rejection(
                self.hass, self.config_entry, rejected
            )
            if renewed is not None:
                self._api_token = renewed
                try:
                    return await self._async_fetch_status()
                except _TokenRejected:
                    rejected = renewed
            if announced is not None and announced == rejected:
                # The hub refused the token it announced itself: it is
                # switching to it right now. Retry instead of reauth.
                raise update_failed("token_switching") from None
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="auth_failed",
            ) from None

    async def _async_fetch_status(self) -> dict[str, Any]:
        """One GET /api/integration/status; raises _TokenRejected on 401."""
        started_at = dt_util.utcnow()
        try:
            async with self._session.get(
                f"{self._base_url}/api/integration/status",
                headers=await self.async_request_headers(),
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status == 401:
                    raise _TokenRejected
                if resp.status != 200:
                    raise update_failed("update_http_error", status=str(resp.status))

                # Parse the body defensively: a non-JSON / malformed payload
                # must surface as a clean UpdateFailed, not an unhandled
                # exception that spams the log with a stack trace.
                try:
                    data = await resp.json()
                except (aiohttp.ClientError, ValueError) as err:
                    raise update_failed("update_malformed", error=str(err)) from err
        except (TimeoutError, aiohttp.ClientError) as err:
            # TimeoutError is included so a hung hub behaves like a
            # refused connection (it is not an aiohttp.ClientError subclass).
            # The coordinator logs the outage once and the recovery once
            # (log-when-unavailable); the entities become unavailable.
            raise update_failed(
                "cannot_connect",
                url=self._base_url,
                error=str(err) or type(err).__name__,
            ) from err

        if not isinstance(data, dict):
            raise update_failed("update_malformed", error=type(data).__name__)
        # Minimal shape of the contract (integration/status.response.json).
        # An answer without it is a failed poll, never an empty hub: the
        # lists below decide which devices and device users are removed,
        # so every item must name what it stands for.
        if not isinstance(data.get("hub"), dict):
            raise update_failed("update_malformed", error="hub")
        self._check_hub_identity(data)
        for key, id_key in _STATUS_LISTS.items():
            items = data.get(key)
            if not isinstance(items, list) or not all(
                _has_id(item, id_key) for item in items
            ):
                raise update_failed("update_malformed", error=key)
        return self._process_snapshot(data, started_at)

    def _check_hub_identity(self, data: dict[str, Any]) -> None:
        """Take the status only from the hub this entry belongs to.

        The first status stores ``hub.instance_id`` in the entry. A later one
        with another id is another hub (hub reinstalled, entry pointed at
        another host): the poll fails before anything is reconciled, so no
        device, HA user or token is removed, and a repair issue asks the
        admin to confirm it in reconfigure or reauthenticate, which stores
        the new id.
        """
        iid = hub_instance_id(data)
        if iid is None:
            raise update_failed("update_malformed", error="hub.instance_id")
        entry = self.config_entry
        stored = entry.data.get(CONF_HUB_INSTANCE_ID)
        issue_id = other_hub_issue_id(entry.entry_id)
        if stored is None:
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_HUB_INSTANCE_ID: iid}
            )
        elif stored != iid:
            self.other_hub = iid
            extra: dict[str, Any] = {}
            if _ISSUE_HAS_ENTRY_ID:
                extra["config_entry_id"] = entry.entry_id
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_OTHER_HUB,
                translation_placeholders={"host": self._base_url},
                **extra,
            )
            raise update_failed("other_hub", url=self._base_url)
        self.other_hub = None
        ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def _process_snapshot(
        self, data: dict[str, Any], started_at: datetime
    ) -> dict[str, Any]:
        """Sanitize a status snapshot and apply its side effects."""
        # Bound how many clients/areas we process so a malformed or oversized
        # payload cannot spawn an unbounded number of entities. guests is
        # sanitized the same way: the guests sensor iterates it directly, so
        # a null/non-list payload must collapse to [].
        raw_clients = data.get("clients")
        raw_areas = data.get("areas")
        truncated = (
            isinstance(raw_clients, list) and len(raw_clients) > MAX_ENTITIES_PER_POLL
        )
        areas_truncated = (
            isinstance(raw_areas, list) and len(raw_areas) > MAX_ENTITIES_PER_POLL
        )
        data["clients"] = self._cap_list(raw_clients, "clients")
        data["areas"] = self._cap_list(raw_areas, "areas")
        data["guests"] = self._cap_list(data.get("guests"), "guests")

        # Hub toggle that disables PIN minting through the integration.
        # Normalized to a real bool; a payload without the field allows it.
        data["allow_pairing_via_integration"] = bool(
            data.get("allow_pairing_via_integration", True)
        )

        # Hub option pairing_qr_camera: when the pairing camera may show
        # the QR code with the PIN. Anything else (or missing) means off.
        if data.get("pairing_qr_camera") not in QR_MODES:
            data["pairing_qr_camera"] = QR_MODE_OFF

        # Hub toggle for the token-less compliance view. A payload
        # without the field means enabled.
        data["compliance_endpoint_enabled"] = bool(
            data.get("compliance_endpoint_enabled", True)
        )

        # Rebuild the O(1) client lookup index for platform entities,
        # skipping any entry missing its required id.
        self.client_by_id = {c["id"]: c for c in data["clients"] if c.get("id")}

        # Cache the per-client compliance blobs for the token-less
        # compliance view. Only dict-shaped blobs are kept; the view
        # collapses everything else to the neutral body anyway.
        self.compliance_by_id = {
            cid: c["compliance"]
            for cid, c in self.client_by_id.items()
            if isinstance(c.get("compliance"), dict)
        }
        # Merge tombstones: hidden/dismissed pending-deletion clients are
        # excluded from data["clients"], but the hub keeps shipping their
        # signed "deleted" blobs in the top-level tombstones array so the
        # compliance view can still serve a verifiable deleted to dormant
        # devices instead of the neutral body.
        for tomb in data.get("tombstones") or []:
            if (
                isinstance(tomb, dict)
                and tomb.get("client_id")
                and isinstance(tomb.get("compliance"), dict)
            ):
                self.compliance_by_id[tomb["client_id"]] = tomb["compliance"]
        self.compliance_endpoint_enabled = data["compliance_endpoint_enabled"]

        self._fire_events(data)
        self._cleanup_orphans(data, truncated=truncated or areas_truncated)
        from .entity import async_sync_client_device_names  # noqa: PLC0415 - import cycle
        from .panel import async_remove_unwanted  # noqa: PLC0415 - import cycle

        async_sync_client_device_names(
            self.hass, self.config_entry.entry_id, data["clients"]
        )

        async_remove_unwanted(
            self.hass, self.config_entry.entry_id, data, truncated=truncated
        )
        self._check_addon_version(data)

        self.snapshot_started_at = started_at
        self.clients_truncated = truncated
        return data

    @staticmethod
    async def _json_or_raise(
        resp: aiohttp.ClientResponse, method: str, path: str
    ) -> dict[str, Any]:
        """Return the JSON object of a 200 answer, raise HubApiError otherwise."""
        if resp.status != 200:
            text = await resp.text()
            detail = _error_detail(text, resp.status)
            raise HubApiError(
                f"API {method} {path} failed: {detail}",
                status=resp.status,
                detail=detail,
                code=_error_code(text),
            )
        try:
            data = await resp.json(content_type=None)
        except ValueError as err:
            raise HubApiError(
                f"API {method} {path}: malformed response",
                status=resp.status,
                detail="malformed response",
            ) from err
        return data if isinstance(data, dict) else {}

    async def async_token_rejected(self) -> None:
        """The hub refused the integration token outside the status poll.

        Runs the poll at once, which takes over a renewed token or starts
        the reauthentication; when the poll still accepts the same token,
        the reauthentication is started here (only that fixes the token).
        """
        token = self._api_token
        await self.async_refresh()
        if self.last_update_success and self._api_token == token:
            self.config_entry.async_start_reauth(self.hass)

    def _hold_for_other_hub(self) -> None:
        """Nothing but the status poll goes to a hub the admin has not confirmed."""
        if self.other_hub is not None:
            raise OtherHubError(self._base_url)

    async def api_post(self, path: str) -> dict[str, Any]:
        """POST to a Halumar API endpoint. Used by buttons and switches."""
        self._hold_for_other_hub()
        async with self._session.post(
            f"{self._base_url}{path}",
            headers=await self.async_request_headers(),
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            return await self._json_or_raise(resp, "POST", path)

    async def api_post_json(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        """POST JSON to a Halumar API endpoint. Used by services."""
        self._hold_for_other_hub()
        async with self._session.post(
            f"{self._base_url}{path}",
            json=data,
            headers=await self.async_request_headers(),
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            return await self._json_or_raise(resp, "POST", path)

    async def api_get_json(self, path: str) -> dict[str, Any]:
        """GET a JSON object from a Halumar API endpoint."""
        self._hold_for_other_hub()
        async with self._session.get(
            f"{self._base_url}{path}",
            headers=await self.async_request_headers(),
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            return await self._json_or_raise(resp, "GET", path)

    async def api_get_image(
        self, path: str, extra_headers: Mapping[str, str] | None = None
    ) -> tuple[int, Mapping[str, str], bytes]:
        """GET a binary resource: (status, headers, body; empty unless 200).

        The headers stay case-insensitive.
        """
        self._hold_for_other_hub()
        async with self._session.get(
            f"{self._base_url}{path}",
            headers={**await self.async_request_headers(), **(extra_headers or {})},
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            body = await resp.read() if resp.status == 200 else b""
            return resp.status, CIMultiDict(resp.headers), body

    async def post_client_token(
        self,
        client_id: str,
        ha_user_id: str,
        refresh_token_id: str,
        token: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Deliver the token of a device's own HA user.

        POSTs to /api/integration/client-token. The bearer travels in the
        body only and never appears in the URL, the logs or a raised error
        (only the HTTP status is surfaced). Raises HubApiError with the
        status on a non-200 answer.
        """
        payload: dict[str, Any] = {
            "client_id": client_id,
            "ha_user_id": ha_user_id,
            "refresh_token_id": refresh_token_id,
            "token": token,
        }
        if request_id:
            payload["request_id"] = request_id
        self._hold_for_other_hub()
        async with self._session.post(
            f"{self._base_url}/api/integration/client-token",
            json=payload,
            headers=await self.async_request_headers(),
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                raise HubApiError(
                    f"client-token delivery for {client_id} failed (HTTP {resp.status})",
                    status=resp.status,
                )
            result = await resp.json(content_type=None)
            return result if isinstance(result, dict) else {}
