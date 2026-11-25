# Concepts

This page explains the ideas behind Halumar as far as the integration is involved. Profiles,
areas and the editor live in the [Halumar Hub](https://github.com/Halumar/hub); the integration
receives their result and acts on it inside Home Assistant.

## Hub, integration and devices

- **Halumar Hub**: a Home Assistant app. It pairs devices, holds areas, profiles and rules, and
  talks to the apps on the home network.
- **Integration** (this repository): runs inside Home Assistant. It connects to the hub, creates a
  Home Assistant user per device, checks rules inside Home Assistant and offers entities, actions
  and events.
- **Device** (in the code: client): one installed Halumar app that was paired with the hub. The
  apps run on iPhone, iPad, Apple TV, Apple Watch, Android phone and tablet (wall panel) and
  Android TV. The protocol also provides for Wear OS watches and a web app; apps for them are
  planned and do not exist yet. Each paired device becomes a Home Assistant device named
  `Halumar: <device>` (guests: `Halumar Guest: <device>`). When the hub knows an owner, the name
  is `Halumar: <owner> <device>`.
- **Apple Watch** is set up through its iPhone and has no connection of its own to the hub. A
  notification for a watch goes to the phone that set it up, and notifications to `all` leave
  watches out.

## Profiles and rule snapshots

Every device has a profile in the hub, with adjustments for the single device. The hub resolves
the profile, the adjustments, the areas, the rules for "when away", quick access and parental
controls into one **rule snapshot** per device and sends it to the integration. A snapshot holds,
among others:

| Part | Meaning in the integration |
|---|---|
| Control path | `checked` or `direct`; a device on Hub only is `checked` here, see below |
| Remote access | Whether the device may reach Home Assistant from outside the home network |
| Entities | Per entity a level at home and away: `hidden`, `view` or `control`; a quick access level (`off`, `status`, `control`) for widgets, voice assistants and watches; and whether a scene, script or automation may be run |
| Parental controls | Bedtime, color lock, volume cap, blocked device types, counters (see below) |
| Access | Access switch, access hours and an early opening |
| Home networks | Up to 10 further private networks that count as "at home" |
| Location | `off`, `foreground` or `background` |

The integration only accepts a snapshot that is valid, whose hash matches and whose Home Assistant
user is the user this integration created for that device. It stores the snapshots, so its checks
work while the hub is not reachable.

A snapshot that cannot be read never loosens anything: the device keeps its earlier rules, and a
device without earlier rules is held read-only. The repair **Device rules could not be read** names
these devices.

## Home Assistant access

The profile rule **Home Assistant access** in the hub (in the protocol: the control path) has three
levels:

| Level | Key on the device | Home Assistant user | How the device reads and switches | Who applies the rules |
|---|---|---|---|---|
| **Hub only** | None; a read-only key with remote access | Group *Read only* (`system-read-only`); the hub holds its token | Through the hub, which calls `halumar.control` for it | The hub and the integration |
| **Checked** | Read-only | Group *Read only* (`system-read-only`) | Reads Home Assistant, switches through the action `halumar.control` | The integration, inside Home Assistant |
| **Direct** | Full | Group *Users* (`system-users`) | Directly, like any normal user | The app |

- **What a device can read**: a device that holds a key (Checked, Direct, or Hub only with remote
  access) can read every state in Home Assistant, also what its profile hides. Only a device on Hub
  only without remote access is limited to what the hub gives it.
- **What a device can switch**: on Hub only and Checked only what its profile allows, checked by
  `halumar.control`; on Direct everything a normal user can.
- The integration knows two paths: the hub sends a device on Hub only as `checked`, so its user is
  read-only like every checked device. A call of the hub for such a device gets every check of
  `halumar.control` (see the [security model](security-model.md)).
- Apple TV, Android TV and Apple Watch always hold a read-only key; a profile on Hub only gives
  them Checked.
- The hub suggests Hub only for guests, babysitters, house sitters and children. Guests and
  children are never on Direct.

See the [security model](security-model.md) for what this does and does not protect.

## Areas and tags

Halumar areas are the rooms and views the apps show; they are not Home Assistant areas. Each area
appears in Home Assistant as a sensor (number of entities) and a switch that turns the area on or
off for all devices. Tags adjust which areas a single device sees; the reserved tag `*` means all
areas.

## Guests

A guest is a device with time-limited access. Pair a guest with **Start guest pairing** or the
action `halumar.start_guest_pairing`.

- The guest gets its own device in Home Assistant with the entity **Guest access until**, which an
  administrator can change to shorten or extend the access.
- The guest's Home Assistant token lives until the end of the access plus 7 days at most; the hub
  removes the guest at the end of the access, and the integration then removes its user and token.
  On Hub only without remote access the hub keeps the token; the guest's app never gets it.
- The event `halumar_guest_expired` fires when a guest disappears.
- Guests never get the wall panel entities. Like any device, a guest is local only unless its
  profile allows remote access.

## Parental controls

Devices with parental controls are on **Hub only** or **Checked**, never on **Direct**. Parental
controls need Halumar Pro in the app on the child's device: without Pro the child's device shows a
notice and controls nothing. The hub and the integration enforce the rules either way. In
`halumar.control` the integration checks:

- locally: blocked device types (lifted by an approval from a family manager), bedtime (switching
  on is refused, switching off or darker is allowed), color lock for lights, and a volume cap for
  media players (a volume above the cap is lowered to the cap; a step up at the cap is refused
  with "The volume of … is at the allowed maximum.");
- by asking the hub: counters and "pause everything" after too many actions. If the hub does not
  answer within 3 seconds, the action is refused ("The Halumar Hub does not answer, so parental
  controls cannot check … right now.").

Groups, scenes and scripts are checked member by member and target by target, so a group cannot be
used to switch a blocked member. Away from home, family managers and child devices that hold a
Home Assistant key talk to the hub through the integration (Home Assistant WebSocket commands
`halumar/family` and `halumar/family/subscribe`). The hub decides what each device may send; the
integration only relays and rate-limits.

## Access hours and the access switch

A device can have access hours and an access switch. Outside its hours, or while the switch is off,
`halumar.control` refuses with "This device has no access right now." A device can ask for early
access; when someone says yes in the hub, the device has access until the time given.

Devices with access hours, and guests, house sitters and babysitters, get the switch
`switch.halumar_<client>_access` (display name **Access**). The entity id does not change with the
language.

## Remote access

- The hub decides per device whether it may reach Home Assistant from outside the home network.
  Without remote access the device's Home Assistant user is **local only**, so Home Assistant
  refuses its sign-in from outside. Taking remote access away renews the token, so open sessions
  from outside end at once.
- On Hub only, remote access gives the device a read-only key. With it the device can read every
  state in Home Assistant from anywhere, also the areas and devices its profile hides; it can still
  switch only what its profile allows. When remote access ends, the hub asks for a new token. The
  key the device held stops working once the integration has removed it; until then it keeps
  working, and the hub keeps asking while the integration cannot be reached.
- `halumar.control` judges each call like Home Assistant's own "local only" check: a call over Home
  Assistant Cloud is away, a call from a local address or one of the device's further home networks
  is at home, anything else is away. Away, the entity's away level applies, and without remote
  access everything is hidden.
- For devices with remote access, the integration reports Home Assistant's external URL to the hub
  (the configured external URL or, without one, the Home Assistant Cloud address), together with the
  Home Assistant instance id. A URL entered in the hub takes precedence.

## Location

The device tracker **Location** of a device is filled in two ways:

- Devices with a Home Assistant token call `halumar.report_location`. The integration checks the
  device's location rule (`off`, `foreground`, `background`), that the position is not older than
  15 minutes or more than 5 minutes in the future, and accepts at most one report per 5 seconds.
- Devices on Hub only that have no token send their position through the hub, which passes it
  on; the integration checks the rule again.

The integration never sends positions to the hub and never writes them to the log; the hub gets
only metadata (for example whether location sharing is on). When a device's rule becomes `off`, its
last position is cleared.

## Notifications while the hub is down

`halumar.display_notification`, `halumar.clear_notifications` and `halumar.ack_notifications`
normally go to the hub. When the hub does not answer, the integration writes the change into the
device's notification sensor (which the hub maintains in Home Assistant) and keeps it in a queue
that survives restarts. When the hub is back, the queue is handed over in order. Limits: 50 queued
notifications per device, none older than 72 hours.

## Wall panels and TVs

A tablet in wall panel mode or a TV can report values and, if its profile allows it, be controlled
from Home Assistant (brightness, wake, screensaver, start area, reload). Which entities a device
gets is decided by the hub from its profile; guests never get them. See
[entities](entities.md#values-and-controls-of-wall-panels-tvs-and-other-devices).
