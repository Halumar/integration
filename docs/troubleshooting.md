# Troubleshooting

The messages below are the texts the integration shows or logs. Log lines come from the logger
`custom_components.halumar`. To see more, turn on debug logging for the Halumar entry
(**Settings > Devices & services > Halumar >** menu **> Enable debug logging**).

Before you open an issue, download the diagnostics (menu of the entry **> Download diagnostics**).
The file contains no tokens, no pairing codes and no positions.

## Setup and connection

**All Halumar entities are unavailable.**
The hub is not running or not reachable. The entry shows "Cannot connect to the Halumar Hub at …"
or "The Halumar Hub is not reachable: …". Check that the Halumar Hub app is started. With a manual
setup, check host, port and HTTPS with **Reconfigure**.

**"The Halumar Hub answered with HTTP …" / "The Halumar Hub sent an invalid answer: …"**
The hub answered, but not as expected. Update hub and integration to the same version.

**Home Assistant asks to reconnect: "The Halumar Hub at … rejected the stored integration token."**
Restart the Halumar Hub app; it hands Home Assistant a new token. Or enter the current token in the
dialog.

**"The Halumar Hub is switching to a new integration token; retrying."**
Home Assistant and the hub started at the same time. The integration retries by itself.

**Repair "Update the Halumar Hub app"** (The Halumar Hub app has version …. This integration needs
version 2026.12.0 or newer.)
Update the Halumar Hub app under **Settings > Apps** in Home Assistant. The repair disappears with
the next successful status request after the update.

**Repair "This is a different Halumar Hub"**, or an action fails with "Not sent: … is a different
Halumar Hub than the one this entry belongs to …"
See [configuration](configuration.md#a-different-hub).

**"The Halumar Hub is already set up. Only one Halumar entry is supported."**
There is already an entry. Use **Reconfigure** on it instead of adding a second one.

**"The discovery data of the Halumar Hub was incomplete or invalid."**
The hub's announcement lacked host, port or token. Restart the Halumar Hub app; if it persists,
add the integration by hand.

**Log warning "The Halumar Hub is configured without SSL for remote host …"**
The token goes over the network unencrypted. Turn on **Use HTTPS** (if the hub offers it) or use
the hub on the same Home Assistant system.

## Devices and users

**A paired device does not show up.**
Pair it in the hub first. Then call `halumar.force_refresh` or wait 30 seconds.

**A device's app cannot sign in to Home Assistant.**
Look for these log lines:

- "No token for device …: …" – the device user failed the safety check (for example it was made an
  administrator, given a password or put into another group). Remove the device in the hub and
  pair it again.
- "Home Assistant has no owner yet; refusing to create a device user …" – finish the Home
  Assistant onboarding first.
- "Several Home Assistant users claim device …; adopting none (a new user is created, check
  Settings > People > Users)" – remove the extra `Halumar · …` users that do not belong to a paired device.

**Log warning "Home Assistant user of device … was removed outside the integration; recreating
it"**
Someone deleted a device user in Home Assistant. The integration creates a new one. Remove devices
in the hub instead.

**Repair "Device user set back to read-only"**
The device's Home Assistant user was changed, for example saved under **Settings > People >
Users**. To give the device full rights, set its Home Assistant access to **Direct** in the hub.

**Repair "Foreign access tokens removed"**
The device's user carried tokens Halumar did not create. They were removed. If nobody did this on
purpose, check who uses the device.

**Repair "Device rules could not be read"**
Usually the integration is older than the hub: update it and restart Home Assistant. The log says
which snapshot was ignored, for example "Ignoring rule snapshot of …: …" or "The hub sent no rules
for device … but still lists it; keeping its earlier rules". The devices named in the repair keep
their earlier rules or can only view.

**Repair "The Halumar integration was not running"**
Home Assistant ran without the integration (log: "The integration was not running from … until at
least … while Home Assistant ran: devices with the checked control path could not switch"). Devices
on Hub only count as checked here. Check the Home Assistant log of that time for why the
integration did not start.

**Repair "Device called an action Halumar cannot block"**
See the [security model](security-model.md#what-home-assistant-does-not-let-an-integration-prevent).

## Actions and entities

**"Only Halumar devices and administrators can use this action."**
`halumar.control` or `halumar.report_location` was called by a user that is not a Halumar device
user (or, for `report_location`, by an administrator).

**"The rules of this device have not arrived from the Halumar Hub yet."**
The device is new or the hub could not build its rules. Wait a moment; check the hub log if it
stays.

**Refusals of `halumar.control`**

| Message | Meaning |
|---|---|
| … is not allowed for this device. | The entity is not in the device's areas, or a scene, script or automation is not allowed to run on this device |
| … cannot be operated from away on this device. | Away level or missing remote access |
| This device may only view … (locking and arming stay possible). | Level `view` at home |
| … cannot be switched from widgets, voice assistants or a watch on this device. | Quick access level is not `control` |
| … switches devices that this device may not switch (outside its areas or not allowed). | A member of a group or a target of a scene or script is not allowed |
| … belongs to Halumar itself (wall panel or hub) – only Home Assistant administrators and automations operate it. | Entities of this integration are never operated by devices |
| This device has no access right now. | Access switch off or outside the access hours |
| … is blocked by parental controls. / Bedtime: … / Short break: … / Break: everything … / The colors of … are locked … / The volume of … is at the allowed maximum. | Parental controls |
| The Halumar Hub does not answer, so parental controls cannot check … right now. | The hub did not answer within 3 seconds |
| This action is not allowed for … / The action contains a field that is not allowed. / A value of the action is not allowed. | Not on the allow-list, or a field out of range |
| … does not exist. | Unknown entity |
| Home Assistant could not run the action for …. | The check passed, but the action itself failed |
| Action halumar.control was called too often. … | More than 120 calls in a minute |

**Wall panel controls fail**

| Message | Meaning |
|---|---|
| … is not connected to the Halumar Hub. | The app is not connected |
| … did not answer within 5 seconds. | No answer from the device |
| … is not a wall panel right now. / The wall panel is not shown on … right now. | Not in wall panel mode |
| Control from Home Assistant is off for … (profile rule). | The profile does not allow it |
| The wall panel needs Halumar Pro on …. | Pro feature |
| … does not support this command. | The app does not offer this command |
| Guests have no wall panel control. | Guest device |
| Announcements are off for … (profile rule). | `panel_announce` not allowed by the profile |
| … cannot see this camera. | `panel_show_camera` with a camera outside the device's areas |

**Pairing buttons are unavailable, or "Pairing through Home Assistant is turned off in the Halumar
Hub."**
Pairing through Home Assistant is turned off in the hub's settings.

**Location refused**: "This device may not share its location." / "This device may share its
location only while the app is in use." / "The position is too old or in the future." – the
device's location rule, or the clock of the device is off.

## Notifications while the hub is down

| Log line | Meaning |
|---|---|
| … for …: the hub is not reachable (…); writing to the device's notification sensor, the hub takes it over when it is back | Normal fallback |
| More than 50 notifications queued for device … while the hub is unreachable; dropping the … oldest | Queue limit per device |
| Dropping … queued notification change(s) older than 72 hours: … | Age limit |
| The hub answered HTTP … to the queued … for device … times since …; it is no longer sent … | The hub keeps refusing one item; the rest is handed over |
| The Halumar Hub rejected the token of the integration (HTTP …). The change is kept and handed over once the integration is signed in again … | Reconnect the integration (see above) |

## Other log lines

| Log line | Meaning |
|---|---|
| The hub returned … clients; processing only the first 500 | More than 500 devices or areas; nothing is removed while the list is cut off |
| The hub did not accept Home Assistant's external URL: … | Check **Settings > System > Network**, or set the URL in the hub |
| Could not install the halumar blueprints: … | The blueprint folder is not writable |
| The hub sent a pairing image that may contain the pairing code; not shown (update the hub) | Update the hub |
