"""The Halumar integration.

Creates Home Assistant devices and entities from the Halumar Hub:
- Hub device: version, area count, connected clients, pairing buttons,
  area switches, pairing status camera
- Client devices (one per paired client): connection status, battery, GPS,
  current area, last seen timestamp
- Guest clients: editable datetime entity for session expiry

Fires events for automations (coordinator.py) and registers the actions in
services.py. Diagnostics support for debug/export via the HA UI.
"""

from __future__ import annotations

from homeassistant.const import CONF_HOST, CONF_PORT, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    instance_id,
    issue_registry as ir,
)
from homeassistant.helpers.typing import ConfigType

from .allowlist import Allowlist
from .compliance import async_register_compliance_view
from .const import (
    CLIENT_DEVICE_ID_PREFIX,
    CONF_API_TOKEN,
    CONF_USE_SSL,
    DEFAULT_USE_SSL,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import HubConfigEntry, HubCoordinator, addon_issue_id
from .device_users import DeviceUserManager
from .enforcement import DATA_ENFORCEMENT, EnforcementManager
from .family import async_register_family
from .enforcement_sync import async_setup_rule_sync
from .entity import async_register_hub_device
from .blueprint_install import async_install_blueprints
from .events import EventReporter, load_event_kinds
from .external_url import ExternalUrlReporter
from .offline_notifications import (
    DATA_OFFLINE_NOTIFICATIONS,
    async_setup_offline_notifications,
)
from .push import async_cancel_push_task, async_sync_push_task
from .services import async_setup_services

__all__ = ["HubConfigEntry"]

# Set up from the UI only (config flow); no YAML configuration.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register what exists once per HA instance: actions and the view."""
    # Enforcement: rule snapshots, device user groups and
    # halumar.control work without a loaded entry (and without the hub).
    events = EventReporter(hass, await hass.async_add_executor_job(load_event_kinds))
    manager = EnforcementManager(hass, await Allowlist.async_load(hass), events)
    events.coordinator_getter = manager.coordinator
    events.device_info = manager.device_info
    hass.data[DATA_ENFORCEMENT] = manager
    await manager.async_setup()

    @callback
    def _shutdown(_event: Event) -> None:
        manager.async_shutdown()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _shutdown)
    # Family manager away from home: HA WebSocket <ns>/family(/subscribe).
    async_register_family(hass)
    async_setup_services(hass)
    # Notifications written while the hub is unreachable, waiting for it.
    await async_setup_offline_notifications(hass)
    # Token-less compliance view: registered once per HA instance (views
    # cannot be deregistered); it resolves the loaded entry per request and
    # relays the hub's signed compliance blobs; the integration holds no
    # keys.
    async_register_compliance_view(hass)
    return True


# The entry functions return bool: HA 2025.11 (minimum supported release,
# hacs.json) still rejects None ("did not return boolean").
async def async_setup_entry(  # pylint: disable=home-assistant-return-type
    hass: HomeAssistant, entry: HubConfigEntry
) -> bool:
    """Set up Halumar from a config entry."""
    # The bundled blueprints, copied at the entry's first setup only.
    await async_install_blueprints(hass, entry)

    coordinator = HubCoordinator(
        hass,
        entry,
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        entry.data[CONF_API_TOKEN],
        use_ssl=entry.data.get(CONF_USE_SSL, DEFAULT_USE_SSL),
    )

    # First refresh: raises ConfigEntryNotReady (HA retries) or
    # ConfigEntryAuthFailed (reauth flow).
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    # Register the hub device before the platforms so client devices can
    # reference it by registry id (via_device_id).
    async_register_hub_device(hass, entry, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # One HA user per paired device: the integration creates each
    # device's user and token in HA and delivers the token to the hub.
    # Reconciled after every successful poll; push requests are dispatched
    # by the push client.
    device_users = DeviceUserManager(hass, entry, coordinator)
    await device_users.async_setup()
    coordinator.device_users = device_users
    entry.async_on_unload(
        coordinator.async_add_listener(device_users.async_schedule_reconcile)
    )
    device_users.async_schedule_reconcile()

    # Rule snapshots: fetched when the status poll reports other hashes
    # (backstop for a lost enforcement_rules push).
    async_setup_rule_sync(hass, entry, coordinator, device_users)

    # HA's external URL (external_url, else the Nabu Casa address) for the
    # devices with remote access; kept current while the entry is loaded. With
    # it HA's instance ID, loaded here once so the reports read it from
    # memory.
    await instance_id.async_get(hass)
    ExternalUrlReporter(hass, entry, coordinator).async_start()

    # Live push channel: when the status snapshot advertises hub.ws_push,
    # keep a WebSocket to the hub so events arrive within ~1s and the
    # REST poll can relax to a safety cadence. Synced on every successful
    # refresh, so a capability change is honored without a reload. The
    # background task is cancelled with the entry; async_unload_entry
    # additionally cancels it explicitly before the coordinator shuts down.
    @callback
    def _sync_push() -> None:
        async_sync_push_task(hass, entry, coordinator)

    entry.async_on_unload(coordinator.async_add_listener(_sync_push))
    _sync_push()

    # Notifications written while the hub was unreachable: remember the
    # devices of every snapshot and hand the queue over once it answers.
    offline = hass.data[DATA_OFFLINE_NOTIFICATIONS]

    @callback
    def _offline_notifications() -> None:
        if coordinator.last_update_success:
            offline.remember_clients(coordinator.data)
            offline.async_schedule_flush(coordinator)

    entry.async_on_unload(coordinator.async_add_listener(_offline_notifications))
    _offline_notifications()

    # No update listener: every path that changes the connection data
    # reloads the entry itself (reconfigure flow, reauth flow, Supervisor
    # re-discovery); the hub's instance id and the blueprint flag need no
    # reload, and the only option is read when the guest button is pressed.
    return True


async def async_unload_entry(  # pylint: disable=home-assistant-return-type
    hass: HomeAssistant, entry: HubConfigEntry
) -> bool:
    """Unload a config entry (the actions stay registered, see services.py)."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = entry.runtime_data
        # Stop the WS push task first: the entry's automatic background-task
        # cancellation only runs after async_unload_entry returns.
        await async_cancel_push_task(coordinator)
        await coordinator.async_shutdown()
    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: HubConfigEntry, device: dr.AnyDeviceEntry
) -> bool:
    """Allow deleting a client device in HA once the hub no longer knows it.

    The hub device and devices of clients the hub still lists stay: they
    would come back with the next snapshot (remove the client in the
    hub instead). Leftovers are removed automatically as well; this only
    lets the user clean up without waiting for the next poll.
    """
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is None or coordinator.clients_truncated:
        return False
    for domain, identifier in device.identifiers:
        if domain != DOMAIN:
            continue
        if not identifier.startswith(CLIENT_DEVICE_ID_PREFIX):
            return False
        client_id = identifier[len(CLIENT_DEVICE_ID_PREFIX) :]
        return client_id not in coordinator.client_by_id
    return True


async def async_remove_entry(hass: HomeAssistant, entry: HubConfigEntry) -> None:
    """Clean up what outlives the entry: device users and the repair issue.

    Without the integration nobody manages the per-device HA users any
    more, so they would linger as orphaned users with valid tokens. Only
    device users this integration owns are removed (see device_users.py).
    """
    ir.async_delete_issue(hass, DOMAIN, addon_issue_id(entry.entry_id))
    await DeviceUserManager(hass, entry, None).async_remove_all()
