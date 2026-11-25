# Devices and entities

The integration creates one device for the hub and one device per paired device. New devices and
areas appear without a reload. Devices and areas that are removed in the hub are removed from Home
Assistant as well (not while the hub's list was cut off at 500 entries).

## Entity ids

Entity ids do not depend on the language or on renames in the hub:

- hub entities: `<platform>.halumar_hub_<name>`
- device entities: `<platform>.halumar_<client>_<name>`, guests `<platform>.halumar_guest_<client>_<name>`

`<client>` is the device name from the time it was paired, as Home Assistant turns it into an id.
When an id is taken, Home Assistant adds a number. Most device entities carry the attribute
`client_id`, which the actions need.

## Who may operate what

Home Assistant lets every signed-in user read every entity. For operating:

- **Administrators and automations only**: every entity of this integration that does something
  (the area switches, the pairing buttons, **Guest access until**, the access switch and every wall
  panel control). Any other user gets *Unauthorized*. This includes the Home Assistant users of
  Halumar devices on **Direct**.
- `halumar.control` refuses every entity of this integration with "… belongs to Halumar itself
  (wall panel or hub) – only Home Assistant administrators and automations operate it." So no
  device, not even a child's wall panel that sees all areas, can operate a panel, an area or a
  pairing button.

## Halumar Hub device

Name **Halumar Hub**, model *Halumar Area Manager*, software version = hub version. The
configuration URL is the hub address.

| Entity | Entity id | Shows |
|---|---|---|
| Version (diagnostic) | `sensor.halumar_hub_version` | Hub version |
| Areas | `sensor.halumar_hub_areas` | Number of Halumar areas |
| Connected devices (diagnostic) | `sensor.halumar_hub_connected_clients` | Devices connected to the hub right now |
| Guests | `sensor.halumar_hub_guests` | Number of active guests. Attribute `guests`: per guest only `client_id`, `is_connected`, `expires_at` (not recorded) |
| Area: *name* (sensor) | `sensor.halumar_hub_area_<area>` | Number of entities in the area. Attributes: `area_id`, `name`, `slug`, `is_active`, `grid_cols`, `tags`, `color`, `description`, `control_mode` |
| Area: *name* (switch) | `switch.halumar_hub_area_<area>` | On while the area is active; turning it on or off activates or deactivates the area for all devices. Administrators and automations only |
| Pairing active | `binary_sensor.halumar_hub_pairing_active` | On while a pairing window is open. Attributes `pairing_remaining`, `is_guest_pairing` (not recorded) |
| Start pairing | `button.halumar_hub_start_pairing` | Opens a pairing window. Unavailable while pairing through Home Assistant is turned off in the hub |
| Start guest pairing | `button.halumar_hub_start_guest_pairing` | Opens a guest pairing window with the hub's default duration; areas as set in the [option](configuration.md#options). Unavailable like the button above |
| Cancel pairing | `button.halumar_hub_cancel_pairing` | Closes an open pairing window |
| Pairing status | `camera.halumar_hub_pairing_qr_code` | A status image from the hub, or the pairing QR code when the hub's option allows it (below); state `streaming` while a pairing window is open. Attributes `pairing_active`, `pairing_remaining`, `is_guest_pairing`, `qr_mode` (`off`, `guests`, `all`) |

The pairing buttons and the camera have the entity category *configuration*; the buttons check
for an administrator.

The pairing code is never in an attribute, an event, the log or the diagnostics. By default it is
not in the camera image either: the image says whether a pairing window is open and that the QR
code is off. Every Home Assistant user can read a camera, so by default the code is shown only in
the hub's panel.

**QR code in the camera (optional).** The option `pairing_qr_camera` in the configuration of the
Halumar Hub app shows the pairing QR code in this camera, for example on a dashboard for a guest to
scan:

| `qr_mode` | The camera shows the QR code |
|---|---|
| `off` (default) | never; the image says "QR code off" |
| `guests` | only while a guest pairing window is open. A scanned code can only add a guest, with the profile and end chosen when the window opened. During any other window the image says "QR code only for guests" |
| `all` | during every pairing window |

While the QR code is shown, everyone who can sign in to this Home Assistant can read it, including
every paired device that holds a Home Assistant key (guest and child devices too, when they hold
one), and for 5 to 10 minutes anyone who has the camera's image address, without signing in (Home
Assistant renews that address every 5 minutes and accepts the last two). Such a person can use the
one-time code before the intended person. A child's device could therefore add a guest device,
which has no parental controls: keep the option `off` while children's devices are paired, or
accept the risk. Prefer `guests` over `all` (see the
[security model](security-model.md#what-home-assistant-does-not-let-an-integration-prevent)).

The camera shows a QR code only when the hub reports a mode that allows that kind of window; it
never keeps a QR code after its window, and the code works once. A live stream of the camera ends
when a pairing window opens, changes or ends and when the user or address that opened it is no
longer valid, so it never carries a later QR code. The hub notes in its audit log when the QR code
was shown (once per window), and a device paired with that code is marked in the hub's log and
notification as "paired with the code shown on the Home Assistant camera". If the hub sends an
image that might contain the code without that permission, the camera shows nothing and logs a
warning.

Area entities keep the name the area had when it was added; rename them in the entity settings if
needed.

## Paired devices

Each paired device: name `Halumar: [<owner> ]<device>` or `Halumar Guest: …`, model = the
device model the app reports, software version = app version. It is linked to the hub device.
Renames in the hub update the device name; a name you gave in Home Assistant stays.

| Entity | Entity id suffix | Shows | Notes |
|---|---|---|---|
| Connected | `binary_sensor …_connected` | Whether the app is connected to the hub | Attributes `client_id`, `client_name`, `last_seen`, `is_active`, `is_guest`, `expires_at` (guests), `pending_deletion` while the hub waits for a removed device to reset |
| Current area | `sensor …_current_area` | Name of the area the device shows; `home` (shown as "Home") on the start screen | Attribute `area_id` |
| Battery | `sensor …_battery` | Battery level in % | Every device without wall panel values; for devices with wall panel values only when the profile lists it |
| Notifications | `sensor …_notifications` | Number of active notifications | Attribute `notification_ids` (ids only, never content; not recorded) |
| Location | `device_tracker …_location` | GPS position, accuracy, and if sent `altitude`, `vertical_accuracy`, `speed`, `course`; attribute `battery_level` | See [Location](concepts.md#location). The last position survives a restart while the rule allows sharing |
| Guest access until | `datetime …_guest_expiry` | End of the guest access | Guests only. Change it to extend or shorten the access. Administrators and automations only |
| Access | `switch …_access` | The device's access switch. Attributes `open`, `reason`, `until`, `opens_at`, `schedule`, `early_until` | Devices with access hours, and guests, house sitters and babysitters. Access = switch on and inside the access hours. Administrators and automations only |
| Device name | `sensor …_device_name` | Device name the app reports | Diagnostic, disabled by default |
| App version | `sensor …_app_version` | App version | Diagnostic, disabled by default |
| Connection type | `sensor …_connection_type` | Connection the app reports | Diagnostic, disabled by default |
| Last seen | `sensor …_last_seen` | Last contact with the hub | Diagnostic, disabled by default |

Text the app reports (device name, app version, connection type, area name) is cleaned of control
characters and cut to 255 characters.

## Values and controls of wall panels, TVs and other devices

The hub decides per device from its profile which of these entities exist (rules for values in
Home Assistant and for control from Home Assistant, and the platform). Guests and watches get none
of them; they keep only the **Battery** sensor. The entities are added and removed as the profile
changes. Live values are unavailable while the app is not connected. Halumar never registers a device as a Home Assistant
mobile app; the values arrive while the app is connected to the hub.

| Entity | Entity id suffix | Shows or does |
|---|---|---|
| App active | `binary_sensor …_app_active` | Devices outside the wall panel mode: on while the app is in the foreground. Attribute `panel_mode` |
| Panel screen | `sensor …_panel_screen` | `active`, `dimmed`, `screensaver`, `night` or `off` (also `off` while not connected). Which of these a device reports depends on the app. Attribute `panel_mode` |
| Charging | `binary_sensor …_charging` | On while the device is on power |
| Screen brightness | `sensor …_panel_brightness` | Backlight in % (diagnostic) |
| Panel illuminance | `sensor …_panel_illuminance` | Light sensor in lx (Android devices with a light sensor) |
| Battery temperature | `sensor …_battery_temperature` | Battery temperature in °C, attribute `battery_health` (Android) |
| Battery | `sensor …_battery` | See above |
| Panel brightness | `number …_panel_brightness` | Sets the backlight, 5–100 % |
| Panel brightness mode | `select …_panel_brightness_mode` | `auto` (profile rule) or `manual`; disabled by default |
| Wake panel | `button …_panel_wake` | Wakes the panel like a touch |
| Panel screensaver | `button …_panel_screensaver` | Starts the screensaver |
| Panel start area | `button …_panel_home` | Shows the start area |
| Reload panel | `button …_panel_reload` | Reloads the panel; disabled by default |

The number, select and buttons are operated only by administrators and automations. A command
fails with a message when the device is not connected, does not answer within 5 seconds, does not
support the command, needs Halumar Pro, is not showing the wall panel, or when its profile does not
allow control from Home Assistant (messages in
[troubleshooting](troubleshooting.md#actions-and-entities)).

## Entities written by the hub

The hub app itself also writes a few states into Home Assistant, for example the per-device
notification sensors the apps read (`sensor.halumar_client_…_notifications`), `sensor.halumar_guests`
and `sensor.halumar_area_…`. They belong to the hub, not to this integration, and their ids never
collide with the ids above (`halumar_hub_…`, `halumar_<client>_…`).

## Diagnostics

**Settings > Devices & services > Halumar >** menu **> Download diagnostics** gives a file with the
connection settings (token replaced by `**REDACTED**`), the options, hub version, areas, devices
(an allow-list of fields: ids, names, connection, versions, battery, tags, notification ids, guest
expiry, remote access, pending deletion), which devices have a Home Assistant user (ids only),
push and poll state, and per device the rules hash, control path, remote access, parental controls
flag, number of entities and Home Assistant group. It contains no tokens, no pairing code, no
positions and no rule contents.

A device on Hub only appears there with the control path `checked` and the group `read_only`, like
every checked device: the hub sends it that way. Its entry in the device list shows
`has_ha_token: false` while the app holds no Home Assistant key (Hub only without remote access, or
its access is closed); the hub then keeps the device's token.
