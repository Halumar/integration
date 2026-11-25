# Events and repairs

## Device and pairing events

The integration compares each hub status with the previous one. The first status after a Home
Assistant start or a reload only sets the starting point: devices that are already connected and a
pairing window that is already open fire nothing.

| Event | Data | Fired when |
|---|---|---|
| `halumar_client_connected` | `client_id`, `client_name`, `device_name` | A device connects to the hub. |
| `halumar_client_disconnected` | `client_id`, `client_name` | A device disconnects, or is removed while connected. |
| `halumar_client_area_changed` | `client_id`, `client_name`, `old_area_id`, `new_area_id`, `new_area_name` | A connected device shows another area. |
| `halumar_pairing_started` | `is_guest` | A pairing window opens. Never with the pairing code. |
| `halumar_pairing_stopped` | – | The pairing window closes. |
| `halumar_guest_expired` | `client_id`, `client_name` | A guest disappears from the hub (access ended or removed). |

## `halumar_event`: everything Halumar reports

Every event of the Halumar event list is fired as `halumar_event`. Events that start in the
integration are also sent to the hub's log; events that start in the hub arrive over the live
connection and are fired here. An event never carries action data, codes, IP addresses or tokens.

Data of events from the integration: `kind`, `category`, `at`, `client_id`, `device_name`,
`owner_label`, `entity_id`, `action`, `route`, `reason`, and where they apply `count`, `since`,
`until`. Events from the hub carry `kind`, `category`, `at` and those of `client_id`,
`device_name`, `owner_label`, `profile_id`, `entity_id`, `action`, `route`, `reason`,
`access_reason`, `count`, `since`, `until`, `tag_id`, `tag_name`, `result` that apply. A test sent
from the hub ("Send test" of a notification rule) has `test: true`.

| `kind` | `category` | From | Meaning |
|---|---|---|---|
| `device_paired` | devices | hub | Device paired |
| `device_set_up` | devices | hub | Device set up |
| `device_removed` | devices | hub | Device removed |
| `access_opened` | devices | hub | Access opened |
| `access_closed` | devices | hub | Access closed |
| `access_requested` | devices | hub | Early access requested |
| `access_request_answered` | devices | hub | Early access request answered |
| `access_request_expired` | devices | hub | Early access request unanswered |
| `profile_changed` | devices | hub | Profile of a device changed |
| `adjustment_expiring` | devices | hub | Temporary adjustment ends soon |
| `adjustment_expired` | devices | hub | Temporary adjustment ended |
| `guest_invited` | guests | hub | Guest invited |
| `guest_paired` | guests | hub | Guest paired |
| `guest_expiring` | guests | hub | Guest access ends soon |
| `guest_expired` | guests | hub | Guest access ended |
| `kids_control` | kids | integration | A child's device switched something |
| `kids_bedtime_attempt` | kids | integration | Attempt during bedtime |
| `kids_pause` | kids | integration | Pause after too many actions |
| `kids_paused_all` | kids | hub | Everything paused (too many devices or actions) |
| `kids_release_requested` | kids | hub | Parent approval requested |
| `kids_request_answered` | kids | hub | Child's request answered |
| `kids_request_expired` | kids | hub | Child's request unanswered |
| `family_change` | kids | hub | Kids rule changed by a family manager |
| `control` | activity | integration | A device switched something |
| `panel_command` | activity | hub | Wall panel controlled |
| `panel_command_rejected` | activity | hub | Wall panel command rejected |
| `tag_scanned` | activity | hub | Tag scanned |
| `denied` | alarms | integration | Control attempt rejected |
| `access_group_restored` | alarms | integration | Access group restored: the group of a device user was changed in Home Assistant and set back |
| `direct_access_attempt` | alarms | integration | Direct access attempt: repeated refused direct attempts, or an action Halumar cannot block |
| `integration_outage` | alarms | integration | The integration was not running |
| `integration_started` | system | integration | The integration started |

The list is part of the protocol (`event_kinds.json`, copied from
[Halumar/contracts](https://github.com/Halumar/contracts)).

## `halumar_alarm`

The kinds of the category *alarms* (`denied`, `access_group_restored`, `direct_access_attempt`,
`integration_outage`) are fired a second time as `halumar_alarm`, with `kind`, `at`, `client_id`,
`device_name`, `entity_id`, `reason`, `route` and, where they apply, `count` and `since`.
`direct_access_attempt` and `integration_outage` are detected on a best-effort basis; see the
[security model](security-model.md#what-home-assistant-does-not-let-an-integration-prevent).

## `halumar_tag_scanned`

When a device resolved an NFC or QR tag, the hub's `tag_scanned` is also fired as
`halumar_tag_scanned` with `tag_id`, `tag_name`, `result`, `client_id`, `device_name`, `at` (and
`test: true` for a test).

## Repairs

The integration raises these repairs under **Settings > System > Repairs**. None of them has a
"fix" button, and none is kept across a restart of Home Assistant (it is raised again if the
cause is still there). **Update the Halumar Hub app**, **This is a different Halumar Hub**,
**Device rules could not be read** and **Foreign access tokens removed** also disappear as soon as
the integration sees that the cause is gone. **Device user set back to read-only** and **Device
called an action Halumar cannot block** disappear when the device is removed; **The Halumar
integration was not running** stays until the next restart.

| Title | Shown when | What to do |
|---|---|---|
| **Update the Halumar Hub app** | The hub is older than this integration needs (2026.12.0). | Update the Halumar Hub app under **Settings > Apps** in Home Assistant. |
| **This is a different Halumar Hub** | The configured address answers as another hub than the one the entry was set up with. Nothing is removed or sent meanwhile. | If the new hub is right, confirm it under **Reconfigure** (or when asked to reconnect). Otherwise point the integration back at the previous hub. See [configuration](configuration.md#a-different-hub). |
| **Device user set back to read-only** | The Home Assistant user of a device had other rights than its profile allows, for example because it was saved under **Settings > People > Users**. Halumar set it back. | To give the device full rights, set its Home Assistant access to **Direct** in the hub. |
| **The Halumar integration was not running** | After a start, Halumar found that Home Assistant ran for more than 30 minutes without the integration, and at least one device is on **Hub only** or **Checked**. Those devices could not switch anything; devices on **Checked** could still see everything. | Check the Home Assistant log for why the integration did not start. |
| **Device rules could not be read** | The hub sent rules that this version cannot read, or none for a device it still lists. Those devices keep their earlier rules; a device without earlier rules can only view. A device that is still being set up gets up to 3 minutes before it is listed. | Usually the integration is older than the hub: update it and restart Home Assistant. Otherwise the hub's log says why. |
| **Foreign access tokens removed** | The Home Assistant user of a device carried access tokens Halumar did not create. Halumar removed them. | If nobody did this on purpose, check who uses this device. |
| **Device called an action Halumar cannot block** | The Home Assistant user of a device called an action that is not tied to an entity. It was detected, not blocked. | If the device should not do this, check who uses its access and remove the device in the hub if needed. See the [security model](security-model.md#what-home-assistant-does-not-let-an-integration-prevent). |

The exact texts are in [`strings.json`](../custom_components/halumar/strings.json) under `issues`.
