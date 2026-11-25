# Changelog

Changes to the Halumar integration for Home Assistant. The integration uses calendar versions
(`YYYY.M.PATCH`, for example 2026.12.0) and is released together with the Halumar Hub of the
same version.

## 2026.12.0

Released on 2026-11-26. First public release of the Halumar integration for Home Assistant.

### Before you install

- Needs Home Assistant 2025.11 or later on Home Assistant OS, or another installation with the
  Supervisor that can run apps, and the Halumar Hub app 2026.12.0 or later. An older hub raises a
  repair.
- Install through the Marketplace (Home Assistant 2026.11 and newer) or HACS (older versions) by
  adding the repository `https://github.com/Halumar/integration`, or copy
  `custom_components/halumar/` by hand. After a Marketplace install the setup opens without a
  restart; after a HACS or manual install, restart Home Assistant. If the setup asks for a token,
  no running hub was found: start the Halumar Hub app and add the integration again, or restart
  Home Assistant. See the README.
- Only one Halumar entry per Home Assistant is supported.
- The Halumar apps for iPhone, iPad, Apple TV and Apple Watch are in beta and available through
  TestFlight: <https://testflight.apple.com/join/fHX7YT8v>. The apps for Android phone, tablet
  (wall panel) and Android TV are not available yet.

### Setup

- The Halumar Hub is discovered through the Supervisor. The hub's integration token is renewed at
  every hub start and taken over without user action.
- A setup started by hand uses the hub's announcement when the Supervisor holds one, so no token
  has to be entered. Manual setup, reconfigure and reauthentication.
- The entry remembers its hub. When the address answers as a different hub, the integration
  changes nothing until an administrator confirms the new hub.
- Option: guests paired with the **Start guest pairing** button see all areas (default off).

### Devices and users

- One Home Assistant user and one token per paired device (iPhone, iPad, Apple TV, Apple Watch,
  Android phone and tablet (wall panel), Android TV). The user is never an administrator, has no
  password and is local only unless the hub allows remote access. It is created, renewed and
  removed with the device; deleting the entry removes all of them.
- A Home Assistant device for the hub and for every paired device, added and removed without a
  reload.
- A child's Apple Watch switches through `halumar.control` with the parental controls of its
  iPhone.

### Rules in Home Assistant

- Three levels of Home Assistant access: **Hub only**, **Checked** and **Direct**. Devices on
  **Checked** have read-only users and switch through the action `halumar.control`, which checks
  the device's rules (areas, at home and away, quick access, which scenes and scripts may run,
  groups, access hours, parental controls) and works while the hub is not reachable.
- Devices on **Hub only** get no Home Assistant key: they read and switch only through the hub,
  which calls `halumar.control` for them with the device's own read-only user and the same checks.
  With remote access they get a read-only key. Guests, babysitters, house sitters and children
  start on Hub only; guests and children are never on Direct.
- A device that holds a key (Checked, Direct, or Hub only with remote access) can read every state
  in Home Assistant. Only a device on Hub only without remote access is limited to what the hub
  gives it. Apple TV, Android TV and Apple Watch always hold a read-only key.
- When the hub withdraws a device's key, the token the device held stops working once the
  integration has removed it. Until then it keeps working, and the hub keeps asking while the
  integration cannot be reached. The device's user stays.
- Parental controls need Halumar Pro in the app on the child's device; the integration enforces
  the rules either way.
- Rules that cannot be read never loosen a device's rights.
- Device user groups are set back when they are changed in Home Assistant.
- Actions that Home Assistant runs for every user and that the integration cannot refuse are
  detected and reported with a repair and an alarm.

### Entities

- Hub: version, areas, connected devices, guests, a sensor and a switch per area, pairing state,
  pairing buttons and a pairing status image. The image shows the pairing QR code only when the
  hub's option `pairing_qr_camera` allows it (`guests`: guest pairing only, `all`: every pairing;
  off by default).
- Devices: connection, current area, battery, notifications, location, end of guest access and
  the access switch `switch.halumar_<device>_access`; diagnostic sensors are disabled by default.
- Wall panels and TVs: screen state, charging, brightness, illuminance, battery temperature and
  app state; brightness, brightness mode, wake, screensaver, start area and reload controls for
  administrators and automations.

### Actions and events

- Actions to navigate, show an entity, send, clear and acknowledge notifications (kept while the
  hub is not reachable), refresh or disconnect a device, start and cancel pairing and guest
  pairing, manage tags, extend guest access, make wall panel announcements and show cameras,
  report locations, and apply rule snapshots by hand.
- Events for connections, area changes, pairing and guests; every Halumar event as
  `halumar_event`, alarms as `halumar_alarm`, tag scans as `halumar_tag_scanned`.
- Live updates over a WebSocket connection to the hub, with polling as a fallback.
- Three blueprints (wall panel battery protection, doorbell camera on an Apple TV, doorbell camera
  on an Android TV or Google TV), copied at the first setup of the entry and importable from
  GitHub.

### Other

- Repairs for an outdated hub, a different hub, restored device users, an integration outage,
  unreadable rules, foreign tokens and actions the integration cannot block.
- Diagnostics without tokens, pairing codes, positions or rule contents.
- English and German translations.
