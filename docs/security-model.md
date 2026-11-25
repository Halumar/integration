# Security model

This page describes what the Halumar integration enforces inside Home Assistant, how, and where
its limits are. It does not cover the hub's own checks or the apps in detail; see
[Halumar/hub](https://github.com/Halumar/hub) and [Halumar/contracts](https://github.com/Halumar/contracts).

## Who enforces what

| Layer | Enforces |
|---|---|
| **App** | What a device shows: hidden areas and entities, the views of a profile. On **Direct** the app also applies the switching rules. |
| **Hub** | Pairing, profiles and rules, what a device receives over the hub connection (for a device on **Hub only** everything it reads and switches), parental-controls counters and pauses, wall panel commands, the family relay. |
| **Integration** | The Home Assistant user and token of every device, its Home Assistant group, the checks of `halumar.control` and `halumar.report_location`, administrator checks on its own entities and actions, detection and reporting of what it cannot block. |
| **Home Assistant** | Sign-in, "local only" users, the *Read only* group (refuses entity actions of read-only users), administrator-only actions. |

Hiding is an app rule: Home Assistant lets every signed-in user read the state of every entity,
and an integration cannot change that. Only a device on **Hub only** without remote access holds no
Home Assistant key; it reads through the hub, which gives it only what its profile allows.

## One Home Assistant user per device

When the hub pairs a device, it asks the integration for a user. The integration creates it and
hands the token to the hub. The hub passes it to the app, except for a device on **Hub only**
without remote access: then the hub keeps the token and uses it only for the device's calls of
`halumar.control`. There is no shared user and no token to copy.

- **Name**: `Halumar · [Guest ][<owner> ]<device>`, so the logbook shows which device did what.
- **Never an administrator**: no owner, no administrator, no system user, no password or other
  login, active, and in exactly one group: *Read only* for **Hub only** and **Checked**, *Users*
  for **Direct**. The integration checks this before every token and refuses to issue one otherwise.
  No device user is created while Home Assistant has no owner yet.
- **Local only** unless the hub allows remote access for the device (guests included). Taking
  remote access away renews the token, so sessions from outside end.
- **Withdrawn keys**: when a device stops holding a key (it moves to **Hub only**, its remote
  access on Hub only ends or its access closes), the hub asks for a new token. The integration
  hands the new token to the hub and then removes the one the device held: from then on it no
  longer works. Until then it keeps working; while the integration cannot be reached this can take
  a while, and the hub keeps asking. The device's user stays, and only the hub holds the new token.
- **One token**: a long-lived access token named `halumar:<client_id>`. Lifetime 3650 days; for
  guests the time to the end of the access, rounded up to days, plus 7 days. A renewed token
  replaces the old one only after the hub confirmed it. Tokens on a device user that the
  integration did not create are removed and reported (repair **Foreign access tokens removed**).
- **Stored**: the integration stores only ids and timestamps; the token itself exists in memory only
  until it is handed to the hub.
- **Removed**: when the device is removed in the hub or a guest access ends, the integration
  removes its user and token; when the Halumar entry is deleted, it removes all device users. It
  only ever removes users it created and never owners, administrators, system users or users with a
  password.

Manage devices in the hub, not in the Home Assistant user list.

## Home Assistant access

Each device's profile sets its **Home Assistant access** (in the protocol: the control path). It has
three levels:

- **Hub only**: the device gets no Home Assistant key. It reads and switches only through the hub,
  which gives it only what its profile allows, and only where the hub is reachable (at home). The
  hub switches for it through `halumar.control` with the device's own user, so every check below
  applies. With remote access the device gets a read-only key (see below).
- **Checked**: the device holds a read-only key. Its user is in the *Read only* group, so Home
  Assistant refuses its entity actions. The device switches through `halumar.control`, which checks
  its rules first.
- **Direct**: the device user has the rights of a normal Home Assistant user. Its rules are applied
  by the app only. Choose this only for devices whose users you would also give a normal Home
  Assistant account.

In Home Assistant there are only two paths: the hub sends a device on Hub only as `checked`, so its
user is read-only like every checked device. The hub suggests Hub only for guests, babysitters,
house sitters and children. Guests and children are never on Direct.

| Level | Key on the device | Can read in Home Assistant | Can switch |
|---|---|---|---|
| **Hub only** | none | nothing directly; through the hub only what its profile allows | through the hub, checked by `halumar.control` |
| **Hub only** with remote access | read-only | every state | through `halumar.control` (from the hub or the app) |
| **Checked** | read-only | every state | through `halumar.control` |
| **Direct** | full | every state | everything, like a normal user |

- A device that holds a key (Checked, Direct, or Hub only with remote access) can read every state
  in Home Assistant, also what its profile hides. Only a device on Hub only without remote access is
  limited to what the hub gives it.
- Apple TV, Android TV and Apple Watch always hold a read-only key: their apps read and switch
  through Home Assistant. A profile on Hub only counts as Checked there.
- Parental controls need Halumar Pro in the app on the child's device. Without Pro the child's
  device shows a notice and controls nothing; the hub and the integration enforce the rules either
  way.

### What `halumar.control` checks

Every signed-in user may call `halumar.control`; Home Assistant gives no other way to let a
read-only user switch. The integration finds the caller's device from its own record of the users
it created (never from the call data or from token names) and then checks, per entity:

1. **Allow-list**: the domain and action must be on the list bundled from the protocol (31
   domains, for example `light.turn_on`, `lock.lock`, `cover.set_cover_position`), and every data
   field must be allowed for that action and within its range. Target fields are never accepted.
   The action then runs as the system, so this list is what keeps a device to entity actions.
2. **The entity exists** and is **not an entity of this integration** (no device may operate a
   panel, an area switch, a pairing button or another device's access switch).
3. **Access**: the access switch is on and the time is inside the access hours (or an early
   opening runs).
4. **Area and level**: the entity is in the device's snapshot; at home its home level applies,
   away its away level, and without remote access it is hidden away from home. `view` allows only
   safe actions such as locking or arming.
5. **Quick access**: calls from widgets, voice assistants and watches need the quick access level
   `control`.
6. **Allowed to run**: scenes, scripts and automations must be allowed to run on the device.
7. **Groups, scenes and scripts**: each member and each target is checked by the rules of its own
   domain, up to three levels deep.
8. **Parental controls**: blocked device types, bedtime, color lock and volume cap locally;
   counters and "pause everything" by asking the hub. No answer within 3 seconds means no.

The route is judged like Home Assistant's own "local only" check: a call over Home Assistant Cloud
is away, a local address or one of the device's further home networks (private ranges only) is at
home, anything unknown is away.

Allowed actions run as the system with the device's call as parent, so the logbook names the
device. Every switch and every refusal becomes an event (`control`, `kids_control`, `denied`,
`kids_bedtime_attempt`, `kids_pause`) for the hub's log and as `halumar_event`. A device user may
call it at most 120 times per minute and with at most 50 entities per call.

The rules are stored by the integration, so these checks also work while the hub is not reachable;
only parental-controls counters and pauses need the hub and are refused without it.

**Calls of the hub.** For a device on **Hub only** the hub calls `halumar.control` with the device's
own token from the internal network of Home Assistant apps. The call is judged as at home and gets
every check above, exactly like a call of the app. Only for a child's device does the hub add a
`grant`. The integration uses it for nothing but its question to the hub about counters and pauses,
which it asks only for a device with parental controls. A grant from any other device is ignored;
it never lifts a rule.

### Keeping the group right

- When a device user's group is changed in Home Assistant (for example by saving the user under
  **Settings > People > Users**), the integration sets it back at once, raises **Device user set
  back to read-only** and fires the alarm `access_group_restored`.
- At every start the integration brings all device users back to their group.
- Rules that cannot be read never loosen: a device keeps its earlier rules, a device without
  earlier rules is held read-only, and a device whose new rules cannot be read is held read-only
  when the hub reports its Home Assistant access as not **Direct**, even if its earlier rules said
  **Direct**.
- **While the integration is down**: nothing resets the groups of device users. A device user
  that an administrator saves with more rights during that time keeps them until the integration
  starts again. Devices on **Hub only** and **Checked** never fall back to direct switching on
  their own; while the integration is down they can view but not switch.
- **Outage detection**: the integration records a sign of life every 10 minutes. At start it
  compares it with the time Home Assistant last saved its states; a gap of more than 30 minutes
  raises the alarm `integration_outage` and, if a device uses **Hub only** or **Checked**, the
  repair **The Halumar integration was not running**.

## Other entry points

- **Administrator checks on entities**: the area switches, pairing buttons, **Guest access
  until**, the access switch and all wall panel controls check that the caller is an administrator
  or an automation. Home Assistant would otherwise let any user press a button or set a value.
- **Administrator actions**: all `halumar.*` actions that change something are registered as
  administrator actions. The exceptions are `control`, `report_location` (device users only, with
  their location rule), `ack_notifications` (the device's own user, administrators, automations)
  and `force_refresh` (only re-reads the hub status).
- **Family relay**: the Home Assistant WebSocket commands `halumar/family` and
  `halumar/family/subscribe` take the device from the calling user, relay to the hub and are
  rate-limited per user. The hub decides what each device may send.
- **Compliance endpoint**: `GET /api/halumar/compliance/<client_id>` works without sign-in, so an
  app whose token was revoked can learn why. It relays a status signed by the hub, which only the
  app can verify; the integration holds no key. Unknown devices, devices without a status and a
  disabled endpoint all get the same neutral answer. One request per second per address.
- **Connection to the hub**: the integration token opens only the hub's `/api/integration/*`
  paths, and the hub renews it at every start. The discovered hub is reached over the internal
  Supervisor network. A manual setup with HTTP to another host logs a warning. The integration
  remembers the hub's instance id and stops working with a different hub until an administrator
  confirms it.
- **Data minimization**: events carry no action data, codes, addresses or tokens. The integration
  never sends positions to the hub and never writes them to the log. The pairing code is never in
  an entity attribute, an event, the log or the diagnostics. Diagnostics contain no tokens, pairing
  codes, positions or rule contents.
- **Pairing QR code in the camera**: off by default. Only with the hub's app option
  `pairing_qr_camera` does `camera.halumar_hub_pairing_qr_code` show the QR code with the pairing
  code: `guests` only during a guest pairing window, `all` during every window. The integration
  shows such an image only when the hub's status reports a mode that allows its kind, and never
  caches it. See below for who can read it.

## What Home Assistant does not let an integration prevent

- **Reading**: every signed-in user, read-only ones included, can read the state of every entity.
  Halumar hides entities in the app; it cannot hide them from a user who uses the token outside the
  app. A device on **Hub only** without remote access has no token to use.
- **Camera images**: Home Assistant serves `/api/camera_proxy/<entity>` to every signed-in user
  without checking any entity permission, and the camera's image address (its `access_token`)
  opens the image without any sign-in; Home Assistant renews it every 5 minutes and accepts the last
  two, so an address works for 5 to 10 minutes. The camera is not told who asks. Halumar manages the
  group of its device users, not per-entity rights, and such rights would not cover this path. So
  while the hub's option `pairing_qr_camera` is on and a code is shown, these can read it: everyone
  who can sign in to this Home Assistant, every paired device that holds a key (guest and child
  devices too when they are on Checked or have remote access), and anyone with a current image
  address. Any of them can use the one-time code before the intended person. With `guests` such a
  code only adds a guest, with the profile and end chosen when the window opened. A guest has no
  parental controls, so a child's device could add one: keep the option `off` while children's
  devices are paired, or accept the risk. With `all` a member window's code adds a member device;
  if the window was opened without a profile, the device gets the default profile of the device
  type the pairing app reports. A live stream (`/api/camera_proxy_stream`)
  is checked by Home Assistant only when it opens; the integration therefore ends it when the user
  or address that opened it is no longer valid and when a pairing window opens, changes or ends.
  The window lasts 5 minutes, the code works once, only administrators can open a window, and a
  device paired with the camera's code is marked in the hub's log and notification.
- **Actions that are not tied to an entity** (for example `mqtt.publish`, `rest_command.*`,
  `shell_command.*`) run for every signed-in user, also for read-only users. Home Assistant gives an
  integration no way to refuse them.
  - What Halumar does: it watches every action call of its device users. When one calls an action
    that is not an entity action, not on the allow-list and not one of Halumar's own, the
    integration logs a warning, raises the repair **Device called an action Halumar cannot block**
    naming the device and the action, and fires the alarm `direct_access_attempt` (at most one
    alarm per device in 10 minutes). Steps of a script or scene that the device was allowed to
    start are not reported. It detects; it does not block.
- **Direct devices** have the rights of a normal user. The integration does not check their entity
  actions; the app does.
- **Refused direct attempts**: Home Assistant fires no event when it refuses an action. Halumar
  counts refused direct attempts of read-only device users on a best-effort basis and may miss
  some; repeated attempts raise `direct_access_attempt`.

### What an administrator can do

- Use **Hub only** for devices whose users should see only what their profile shows, for example
  guests and children. Keep other devices on **Checked** unless their users should have a normal
  Home Assistant account.
- Leave the hub's option `pairing_qr_camera` off unless a dashboard needs the QR code; then use
  `guests`, and keep it off when children's devices are paired.
- Grant remote access only to devices that need it; without it the device's user cannot sign in
  from outside. On **Hub only**, remote access gives the device a read-only key, and with it the
  device can read every state in Home Assistant from anywhere.
- Be aware of which installed integrations offer actions without an entity, since every signed-in
  user can call them.
- Watch the repairs and the alarms (`halumar_alarm`, for example in an automation that notifies
  you), and the hub's log.
- Remove a device in the hub when its access should end; its Home Assistant user and token are
  removed with it.

## Reporting a security problem

See [SECURITY.md](../SECURITY.md).
