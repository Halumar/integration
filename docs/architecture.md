# Architecture

This page describes how the integration (`custom_components/halumar`) is built and how it behaves
at runtime. It is written for contributors and reviewers. User documentation is in the other pages
of [docs/](README.md).

## Overview

Halumar is a `hub` integration with `iot_class: local_push`. It talks to the Halumar Hub app over
the hub's REST API (`/api/integration/*`) and, when the hub offers it, a WebSocket (`/api/ws`).
Everything goes through Home Assistant's shared `aiohttp` session, with the integration token as
bearer and the header `X-Hub-Integration-Version`.

Two parts work independently of each other:

- **Per config entry**: a `DataUpdateCoordinator` (`HubCoordinator`) polls
  `GET /api/integration/status`. Entities, device users, the push client, the external URL report
  and the offline notification queue hang off it.
- **Per Home Assistant instance** (set up in `async_setup`, also without a loaded entry): the
  `EnforcementManager` with the stored rule snapshots and the device user groups, the actions, the
  event reporter, the compliance view and the family relay. `halumar.control` works
  with the stored snapshots while the hub or the entry is down.

```text
                         Halumar Hub app
     GET status   POST /api/integration/*   GET pairing-qr   WS /api/ws
     (30 s/300 s) (actions, tokens, events) (camera)         (push messages)
          |                 ^                    |                |
          v                 |                    v                v
   +---------------+   +-------------------+  +--------+   +---------------------+
   | HubCoordinator|-->| entities, actions |  | camera |   | push client         |
   | (.data)       |   | device users      |  +--------+   | refresh triggers,   |
   +---------------+   | external URL      |               | provisioning, rules,|
          |            | offline queue     |               | hub events, family, |
          v            +-------------------+               | location            |
   platforms: sensor, binary_sensor, switch, button,       +---------------------+
   camera, datetime, device_tracker, number, select

   per instance: EnforcementManager (snapshots, groups, watchers) + EventReporter
                 halumar.control, halumar.report_location, family relay, compliance view
```

## File map

| File | Responsibility |
|---|---|
| `__init__.py` | `async_setup` (per instance: enforcement, actions, offline queue, compliance view), entry setup (including the blueprints) and unload, device removal, `async_remove_entry` (removes device users) |
| `coordinator.py` | `HubCoordinator`: status poll, shape check, hub identity check, events, orphan cleanup, version repair, `hub_call` error mapping, HTTP helpers |
| `config_flow.py` | User, Supervisor discovery, reauth, reconfigure and "other hub" steps; options flow |
| `token_renewal.py` | Takes over a token the hub announced after a 401, before reauth |
| `push.py` | WebSocket client: refresh triggers, dispatch of push messages, backoff, silence timeout, pairing-expiry timer |
| `device_users.py` | `DeviceUserManager`: one Home Assistant user and token per device, delivery to the hub, reconcile, safety checks |
| `enforcement.py` | `EnforcementManager`: snapshots, groups, `user_updated` watcher, outage detection, direct-attempt and entity-less action detection, repairs |
| `enforcement_sync.py` | Fetches the snapshots when the status reports other hashes; handles the push messages `enforcement_rules` and `ha_event` |
| `snapshot.py` | Validation of rule snapshots and time-window helpers (bedtime, access hours) |
| `allowlist.py`, `control_allowlist.json` | Allow-list of domains, actions and fields for `halumar.control` (copy of the protocol file) |
| `control.py` | `halumar.control` |
| `run_targets.py` | Targets of scripts and scenes for the checks of `halumar.control` |
| `location.py` | `halumar.report_location` and the `client_location` push message |
| `family.py` | Home Assistant WebSocket commands `halumar/family` and `halumar/family/subscribe` |
| `events.py`, `event_kinds.json` | Event reporter: fires `halumar_event`, `halumar_alarm`, `halumar_tag_scanned`, queues events for the hub (copy of the protocol event list) |
| `services.py`, `services.yaml` | The other actions |
| `offline_notifications.py` | Notification queue while the hub is not reachable |
| `external_url.py` | Reports Home Assistant's external URL and instance id to the hub |
| `compliance.py` | Unauthenticated compliance relay view |
| `panel.py` | Wall panel entities and commands |
| `entity.py` | Base entities, device info, entity id scheme, administrator check |
| `sensor.py`, `binary_sensor.py`, `switch.py`, `button.py`, `camera.py`, `datetime.py`, `device_tracker.py`, `number.py`, `select.py` | Platforms |
| `diagnostics.py` | Redacted diagnostics |
| `blueprint_install.py`, `blueprints/` | Copies the bundled blueprints into the configuration folder at the entry's first setup |
| `brand.py`, `brand/` | Display brand (generated from the protocol's brand file) and brand images |
| `const.py` | Domain, identifiers, intervals, minimum hub version |
| `strings.json`, `translations/`, `icons.json`, `quality_scale.yaml` | UI texts, icons, quality-scale self-assessment |

## Setup and teardown

`async_setup` (once per instance) creates the `EventReporter` and the `EnforcementManager`, loads
the stored snapshots, detects an outage, starts the watchers, registers `halumar.control`,
`halumar.report_location`, the family commands and the other actions, loads the offline
notification queue and registers the compliance view.

`async_setup_entry`:

1. At the entry's first setup, copies the bundled blueprints and sets `blueprints_installed` in
   the entry's data; later setups copy nothing.
2. Creates the `HubCoordinator` and runs the first refresh. A failure raises
   `ConfigEntryNotReady` (retry) or `ConfigEntryAuthFailed` (reauth).
3. Registers the hub device, then forwards the platforms.
4. Creates the `DeviceUserManager`, loads its store and reconciles after every successful poll.
5. Sets up the rule sync, the external URL reporter, the push task (started when the status
   advertises `hub.ws_push`) and the offline queue hand-over.

There is no update listener: every path that changes the connection data reloads the entry itself.
The hub's instance id and `blueprints_installed` are written without a reload.
`async_unload_entry` cancels the push task and shuts the coordinator down; device users and tokens
stay. `async_remove_entry` deletes the version repair and removes all device users of the entry.

## The status poll

`HubCoordinator` polls `GET /api/integration/status` every 30 seconds (`UPDATE_INTERVAL`) and
every 300 seconds (`SLOW_UPDATE_INTERVAL`) while the push channel is connected. Each request has a
15-second timeout.

- `401`: the coordinator first tries a renewed token (`token_renewal.py`): one written to the
  entry meanwhile, or the token the hub announced through the Supervisor for the same host and
  port. If the hub rejects the token it announced itself, it is switching right now
  (`token_switching`, retried). Otherwise `ConfigEntryAuthFailed` starts reauth.
- Any other non-200, connection error or timeout: a translated `UpdateFailed`; entities become
  unavailable.
- **Shape check**: `hub` must be an object with a UUID `instance_id`; `areas`, `clients`,
  `guests`, `provisioning`, `ha_user_holds` must be lists whose items name their id. A status
  without this shape is a failed poll, never an empty hub, because these lists decide what is
  removed.
- **Hub identity**: the first status stores `hub.instance_id` in the entry. A status with another
  id raises the repair `other_hub` and fails the poll before anything is reconciled; only
  reconfigure or reauth store the new id.
- Lists are capped at 500 items (`MAX_ENTITIES_PER_POLL`); a cut list removes nothing.

On success the coordinator rebuilds its client index and compliance cache, fires the device and
pairing events (the first status after setup is only the baseline), removes devices and area
entities that left the status, and raises or clears the version repair
(`MIN_ADDON_VERSION = 2026.12.0`).

## Push channel

When the status advertises `hub.ws_push`, a background task keeps a WebSocket to `/api/ws` with the
same headers. Hub events only trigger a refresh; the status stays the single source of truth.
Other messages are dispatched: device user requests (`provision_client`,
`update_client_identity`, `deprovision_client`), rule snapshots (`enforcement_rules`), hub events
(`ha_event`), positions (`client_location`) and family relay pushes. A JSON `ping` is answered with
`pong`. Reconnects back off from 1 to 60 seconds with jitter. A session silent for 45 seconds is
closed. A rejected handshake never starts reauth; that stays with the poll.

## Device users

`DeviceUserManager` creates one Home Assistant user and one long-lived token per device when the
hub asks for it (push message, repeated in the status list `provisioning`). It checks the user
before every token (`assert_safe_device_user`), delivers the token with
`POST /api/integration/client-token` and removes older tokens only after the hub confirmed the new
one. The reconcile after every poll applies names, owner, remote access and guest expiry, and
removes users of devices that are gone, never while the client list was cut off, never users
created after the status was requested, and never users the hub still holds (`ha_user_holds`).
Only ids are stored (`halumar.device_users.<entry_id>`). Details for users:
[security model](security-model.md#one-home-assistant-user-per-device).

## Enforcement

`EnforcementManager` stores the snapshots (`.storage/halumar.enforcement`), sets each device user's
group (*Read only* for `checked`, *Users* otherwise), restores it on `user_updated`, holds devices
read-only whose rules cannot be read, detects outages from its 10-minute heartbeat, counts refused
direct actions from Home Assistant's WebSocket log, and reports entity-less actions of device users
from `call_service` events. `halumar.control` (`control.py`) checks every call against the
allow-list and the snapshot, asks the hub only for parental-controls counters
(`POST /api/integration/kids-check`, 3-second timeout, no answer = refused) and executes as the
system with the caller's context as parent.

Snapshots arrive by push; the status carries their hashes, and `enforcement_sync.py` fetches the
full set (`GET /api/integration/enforcement-rules`) once per distinct set of hashes when they
differ.

## Events

`EventReporter` fires every integration event at once as `halumar_event` (alarm kinds also as
`halumar_alarm`) and queues it for the hub (`POST /api/integration/events`, batches of 100, every
10 seconds or at 20 queued, at most 500 kept while the hub is down, oldest dropped and counted).
Hub events arrive as `ha_event` and are only fired. The device and pairing events of the
coordinator (`halumar_client_connected` and others) are fired from the status comparison.

## Actions and errors

Actions are registered in `async_setup`, so they exist without a loaded entry and fail with
`not_loaded` until it is loaded. Administrator actions use `async_register_admin_service`.
Every hub call goes through `hub_call`: HTTP 400 and 404 become `ServiceValidationError`, other
failures `HomeAssistantError` with a translated message; nothing is only logged. The privileged
actions (`start_pairing`, `start_guest_pairing`, `manage_client_tags`, `extend_guest`) hold a
per-action lock and a 3-second cooldown. Entity actions that change something call
`async_require_admin`.

## Offline notifications

`offline_notifications.py` keeps a persistent queue (`.storage/halumar.offline_notifications`) of
notifications, clears and acknowledgements made while the hub did not answer, writes them into the
device's notification sensor and hands them over in order when the hub is back. Rejected-token and
other-hub answers pause the hand-over; an item the hub keeps refusing is set aside after five
attempts over ten minutes. Bounds: 50 per device, 72 hours.

## Compliance view

`GET /api/halumar/compliance/{client_id}` (`requires_auth = False`) relays the hub's signed
compliance status to apps that can no longer sign in. Unknown, compliant, unsigned and disabled
all give the same neutral answer (`status: unknown`, HTTP 200); one request per second per remote
address.

## Entities

Entity ids are built from fixed English object names, not from translated names:
`<platform>.halumar_hub_<name>` and `<platform>.halumar_[guest_]<client>_<name>`. Unique ids are
`<entry_id>_hub_<key>` and `<entry_id>_client_<client_id>_<key>`. Per-area and per-client
platforms add entities when ids first appear in the status and forget ids that left it. Wall
panel entities follow the hub's per-device list `panel_ha` and are removed when it no longer
lists them. The list of entities is in [entities.md](entities.md).

## Config flow

All flows share the unique id `halumar`; a single instance is enforced in the flow (not with the
manifest's `single_config_entry`, which would stop Supervisor re-discovery from rotating the token).
Steps: `user`, `hassio` / `hassio_confirm`, `reauth_confirm`, `reconfigure`, `other_hub`; options
step `init`. Details for users: [configuration.md](configuration.md).

## Diagnostics

`diagnostics.py` redacts the token, copies only an allow-list of client fields, and adds device
user ids, push and poll state and the enforcement summary (hashes, control paths, counts, groups).
