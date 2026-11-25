# Actions

All actions are in the domain `halumar`. They exist as soon as the integration is installed; most
of them fail with "The Halumar integration is not set up or not loaded." until the entry is loaded.
A failed action always fails with a message; nothing is only logged.

## Who may call which action

| Callers | Actions |
|---|---|
| Home Assistant administrators, automations and scripts | `navigate_to_area`, `show_entity`, `display_notification`, `clear_notifications`, `refresh_client`, `kick_client`, `start_pairing`, `start_guest_pairing`, `cancel_pairing`, `manage_client_tags`, `extend_guest`, `panel_announce`, `panel_show_camera`, `apply_rules` |
| The device's own Home Assistant user, administrators, automations | `ack_notifications` |
| Every signed-in user | `force_refresh` (only reads the hub status again) |
| Every signed-in user; executes only for Halumar device users within their rules, and for administrators and automations | `control` |
| Halumar device users only | `report_location` |

Home Assistant rejects a call to an administrator action by any other user with *Unauthorized*.

`start_pairing`, `start_guest_pairing`, `manage_client_tags` and `extend_guest` run one at a time
and at most once every 3 seconds each. A faster call fails with "Action … was called too often.
Wait … seconds and try again."; a call while one runs fails with "Action … is already running.
Wait until it has finished."

`client_id` is the id of a paired device. Read it from the attribute `client_id` of one of the
device's entities, for example
`{{ state_attr('binary_sensor.halumar_kitchen_ipad_connected', 'client_id') }}`.

## Devices

### `halumar.navigate_to_area`

Opens an area, or the start screen, on a device.

| Field | Required | Values |
|---|---|---|
| `client_id` | yes | Device id |
| `area_id` | no | Id of a Halumar area (attribute `area_id` of an area sensor) |
| `home` | no, default `false` | `true` opens the start screen and wins over `area_id` |

Without `area_id` and without `home: true` the call fails: "Select an area, or turn on Home."

### `halumar.show_entity`

Shows the controls of one entity on a device.

| Field | Required | Values |
|---|---|---|
| `client_id` | yes | Device id |
| `entity_id` | yes | Any entity |
| `control_mode` | no | `basic` (on/off only), `advanced` (common controls), `full` (all controls) |

The device's profile still decides whether it may see the entity.

### `halumar.refresh_client`

Makes a device load everything again. Field: `client_id`.

### `halumar.kick_client`

Ends the hub connection of a device. The device stays paired and may connect again. Field:
`client_id`.

### `halumar.manage_client_tags`

Changes the tags of a device. Tags adjust which areas a device sees; `*` means all areas.

| Field | Required | Values |
|---|---|---|
| `client_id` | yes | Device id |
| `action` | yes | `add`, `remove` or `set` (replace all) |
| `tags` | yes | List of tags |

## Notifications

### `halumar.display_notification`

Sends a notification to one device, or with `client_id: all` to every active device (watches are
left out; a notification for a watch goes to the phone that set it up).

| Field | Required | Values |
|---|---|---|
| `client_id` | yes | Device id or `all` |
| `title` | one of title or message | Up to 500 characters |
| `message` | one of title or message | Up to 5000 characters |
| `notification_type` | no, default `default` | `default` (disappears by itself) or `sticky` (stays until dismissed) |
| `icon` | no | For example `mdi:door-open`, up to 200 characters |
| `icon_svg` | no | SVG source, up to 10000 characters |
| `popup_mode` | no | `none` (banner), `popup`, `both`, `direct` (open the action directly) |
| `scheduled_at` | no | ISO 8601 date and time |
| `priority` | no | 0 to 100 |
| `color` | no | `none`, `red`, `blue`, `green`, `yellow`, `orange` |
| `action_type` | no | `none`, `area`, `entity`, `url` |
| `action_target` | no | Area id, entity id or URL, up to 2000 characters |
| `show_time` | no | `true` or `false` |

Without title and message the call fails: "A notification needs a title or a message." Every
notification gets an id, so a retry is never shown twice. If the hub does not answer, the
notification goes straight into the device's notification sensor and is handed to the hub later
(see [concepts](concepts.md#notifications-while-the-hub-is-down)).

### `halumar.clear_notifications`

Removes all notifications from a device, also while the hub is not reachable. Field: `client_id`.

### `halumar.ack_notifications`

Confirms the delivery of notifications; the apps use it away from home.

| Field | Required | Values |
|---|---|---|
| `client_id` | yes | Device id |
| `notification_ids` | yes | 1 to 50 notification ids |

Only the device's own Home Assistant user, an administrator or an automation may call it. The hub
checks every id against the device.

## Pairing and guests

### `halumar.start_pairing`

Opens a pairing window for a new device. The pairing code is shown in the hub's panel. Home
Assistant shows it only when the hub's option `pairing_qr_camera` is `all`, in the camera
**Pairing status** (see [entities](entities.md#halumar-hub-device)).

| Field | Required | Values |
|---|---|---|
| `profile` | no | Name of a hub profile, 1 to 40 characters. Without it the default profile of the device type applies; an unknown name fails in the hub |

If pairing through Home Assistant is turned off in the hub, the call fails: "Pairing through Home
Assistant is turned off in the Halumar Hub."

### `halumar.start_guest_pairing`

Opens a pairing window for a guest. The pairing code is shown in the hub's panel. Home Assistant
shows it only when the hub's option `pairing_qr_camera` is `guests` or `all`, in the camera
**Pairing status**.

| Field | Required | Values |
|---|---|---|
| `expires_hours` | no | Whole hours, 1 to 2160 (90 days). Without it the hub's default guest duration applies |
| `tags` | no | Areas by tag. Cannot be combined with `all_areas`; `*` is not allowed as a tag |
| `all_areas` | no, default `false` | Share all areas |
| `profile` | no | Name of a hub profile, 1 to 40 characters; without it the guest default profile |

Without `tags` and `all_areas` the areas of the guest's profile apply; if it gives none, the guest
sees no area.

### `halumar.cancel_pairing`

Closes an open pairing window. Calling it without an open window is not an error.

### `halumar.extend_guest`

Changes the end of a guest access.

| Field | Required | Values |
|---|---|---|
| `client_id` | yes | Device id of the guest |
| `hours` | one of the two | 0.5 to 720 hours from now |
| `expires_at` | one of the two | ISO 8601 date and time |

Without either the call fails: "Enter hours or an expiry date and time."

## Wall panels and TVs

### `halumar.panel_announce`

Speaks and shows a text on a wall panel, with a chime.

| Field | Required | Values |
|---|---|---|
| `client_id` | yes | Device id |
| `text` | yes | 1 to 300 characters; never written to the log |
| `volume` | no | 0 to 100 % |
| `chime` | no | `true` or `false` |

The hub refuses it when announcements are off in the device's profile.

### `halumar.panel_show_camera`

Opens a camera on a wall panel or TV.

| Field | Required | Values |
|---|---|---|
| `client_id` | yes | Device id |
| `entity_id` | yes | A `camera.` entity |

The hub refuses it when the device cannot see this camera.

## Rules and checks

### `halumar.control`

Switches on behalf of a Halumar device after checking its rules. Devices on **Checked** use it for
every switch, and the hub uses it for devices on **Hub only**, with the device's own token and the
same checks; see the [security model](security-model.md).

| Field | Required | Values |
|---|---|---|
| `entity_id` | one of the two | One entity |
| `entity_ids` | one of the two | 1 to 50 entities |
| `action` | yes | Action of the entity's domain, for example `turn_on` |
| `data` | no | Action data. Only fields the allow-list knows for that action, within their ranges; target keys (`entity_id`, `device_id`, `area_id`, `floor_id`, `label_id`, `target`) are never allowed |
| `origin` | no, default `app` | `app`, or `quick_access` for widgets, voice assistants and watches |
| `check_only` | no, default `false` | Only check, do not switch and do not report |
| `grant` | no | Only for the Halumar Hub, on calls for a child's device; passed on to the hub's check of counters and pauses. Ignored for devices without parental controls |

What happens:

- **Device user**: the call is checked against the device's rule snapshot (see the
  [security model](security-model.md#what-halumarcontrol-checks)). Allowed actions run as the
  system with the device's call as parent, so the logbook names the device. Every switch and every
  refusal is reported as an event.
- **Administrator or automation**: only the allow-list applies; the action runs with the caller's
  own context.
- **Any other user**: "Only Halumar devices and administrators can use this action."
- A device user may call it at most 120 times per minute.

With one entity, a refusal fails the call with the reason, for example "… is not allowed for this
device." With several entities, the call fails with "… of … devices were not switched because the
device may not operate them." unless a response is requested. With a response it returns
`route` (`local`, `external`, `unknown`), `rules_hash` and per entity `entity_id`, `ok`, `executed`,
`reason`, `until` and `adjusted` (for example a lowered volume).

### `halumar.report_location`

Position of a Halumar device that has a Home Assistant token. Only device users may call it.

| Field | Required | Values |
|---|---|---|
| `latitude` | yes | -90 to 90 |
| `longitude` | yes | -180 to 180 |
| `accuracy` | yes | 0 to 100000 m |
| `altitude` | no | -1000 to 100000 m |
| `vertical_accuracy` | no | 0 to 100000 m |
| `speed` | no | 0 to 1000 m/s |
| `course` | no | 0 to 360° |
| `battery_level` | no | 0 to 100 % |
| `background` | yes | Whether the app was in the background |
| `ts` | yes | Time of the position (ISO 8601) |
| `location_shared` | no | `none`, `foreground`, `background` |

The device's location rule, the age of the position and one report per 5 seconds are checked; then
the device tracker is updated. Returns `applied` (`false` for a position older than the last one).

### `halumar.apply_rules`

Applies rule snapshots by hand. The hub sends them by itself; this action exists for checks and
tests. Snapshots are validated exactly like the ones the hub sends. Fields: `snapshots` (up to
500), `removed` (up to 500 device ids), `complete` (whether this is the full set),
`control_paths` (device id to `checked` or `direct`). Returns `applied` and `rules_hashes`; fails
with "… rule snapshot(s) were rejected: invalid, wrong hash, or not for a device user of this
integration." when one is rejected.

### `halumar.force_refresh`

Reads the status from the hub now. No fields.

## Entity actions

The usual Home Assistant actions work on the integration's entities (`switch.turn_on` on an area
switch, `button.press` on a pairing button, `datetime.set_value` on **Guest access until**,
`number.set_value`, `select.select_option`). They are limited to administrators and automations;
see [entities](entities.md#who-may-operate-what).
