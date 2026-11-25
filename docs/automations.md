# Automations

Automations run without a user, so they may use every Halumar action and operate every Halumar
entity. Replace the entity ids below with yours.

## Examples

Light the hallway when a device connects:

```yaml
automation:
  - alias: "Welcome home"
    triggers:
      - trigger: event
        event_type: halumar_client_connected
        event_data:
          client_name: "Kitchen iPad"
    actions:
      - action: light.turn_on
        target:
          entity_id: light.hallway
```

Show the front door camera on a TV when the doorbell rings:

```yaml
automation:
  - alias: "Doorbell on the TV"
    triggers:
      - trigger: state
        entity_id: binary_sensor.doorbell
        to: "on"
    actions:
      - action: halumar.show_entity
        data:
          client_id: "{{ state_attr('binary_sensor.halumar_living_room_tv_connected', 'client_id') }}"
          entity_id: camera.front_door
          control_mode: full
```

A notification on all devices:

```yaml
automation:
  - alias: "Washing machine done"
    triggers:
      - trigger: state
        entity_id: sensor.washing_machine
        to: "done"
    actions:
      - action: halumar.display_notification
        data:
          client_id: all
          title: "Laundry"
          message: "The washing machine is done."
          icon: mdi:washing-machine
```

Tell an administrator about every Halumar alarm:

```yaml
automation:
  - alias: "Halumar alarm"
    triggers:
      - trigger: event
        event_type: halumar_alarm
    actions:
      - action: notify.notify
        data:
          title: "Halumar"
          message: "{{ trigger.event.data.kind }} on {{ trigger.event.data.device_name }}"
```

Guest pairing for one evening, with the guest areas tagged `guest`:

```yaml
script:
  guest_evening:
    sequence:
      - action: halumar.start_guest_pairing
        data:
          expires_hours: 8
          tags:
            - guest
```

### Dashboard: a guest scans the pairing QR code

Needs the hub's app option `pairing_qr_camera` set to `guests` (or `all`), see
[entities](entities.md#halumar-hub-device). The button opens a guest pairing window with a profile and a duration;
the card below shows the QR code until a guest has scanned it or the window ends (5 minutes). The
action is for administrators only: a person signed in without administrator rights gets an error.

```yaml
type: vertical-stack
cards:
  - type: button
    name: Guest QR code
    icon: mdi:qrcode
    tap_action:
      action: perform-action
      perform_action: halumar.start_guest_pairing
      data:
        profile: Guest
        expires_hours: 24
  - type: picture-entity
    entity: camera.halumar_hub_pairing_qr_code
    camera_view: auto
    show_name: false
    show_state: false
```

`profile` is the name of a guest profile in the hub (without parental controls). With
`camera_view: auto` the card loads a new still image every 10 seconds, so the QR code appears up to
10 seconds after the button is pressed and disappears up to 10 seconds after the window ends or the
code is used. Do not use `camera_view: live` for this card: the live stream ends whenever a pairing
window opens or ends (so it never carries a code it was not opened for), and the card then keeps
showing the last image until Home Assistant renews the camera's address (up to 5 minutes) or the
page is reloaded.

## Bundled blueprints

When the Halumar entry is set up for the first time, the integration copies three blueprints to
`<config>/blueprints/automation/halumar/`. A file that already exists there is never overwritten,
so your own changes stay. The blueprints are not copied again after a restart or reload, so a
blueprint you delete stays deleted; only a newly added Halumar entry copies the missing ones again.
Bundled copies are not updated with the integration; import a blueprint from GitHub (below) to get
new versions. Create an automation from them under **Settings > Automations & scenes >
Blueprints**.

### Wall panel: keep the battery between two limits

For tablets on permanent power (iPad or Android tablet). Choose the panel's **Battery** sensor and
the smart plug of its charger: the plug turns on at the lower limit (default 20 %) and off at the
upper limit (default 80 %).

### Doorbell → camera on a TV

Two blueprints open Halumar on a TV directly on a camera when the doorbell rings. They use the open
link `https://halumar.com/open/camera/<entity_id>` (input *Link* under *Advanced* = *Website*, the
default) or the app link `halumar://open/camera/<entity_id>` (*App*). The link only opens the view
and switches nothing; the TV's profile decides whether the camera is visible and live. Choose a
doorbell event entity or a binary sensor that turns on, the camera (in an area the TV can see) and
the TV. Ringing again within the pause (default 30 seconds) does nothing.

| Blueprint | For | Needs | How it opens the app |
|---|---|---|---|
| *Doorbell: show a camera on an Apple TV* | Apple TV | Home Assistant's **Apple TV** integration | `media_player.play_media` with `media_content_type: url`; the option *Turn the Apple TV on* wakes it first |
| *Doorbell: show a camera on an Android TV or Google TV* | Android TV / Google TV | Home Assistant's **Android TV Remote** integration | `remote.turn_on` with `activity`; the option *Only when the TV is on* skips a TV in standby |

The Android TV app is not available yet; the blueprint is ready for it.

### Import a blueprint from GitHub

You can also import each blueprint from its file on GitHub. Home Assistant remembers that address,
so **Re-import blueprint** fetches the current version later. Use the link, or go to
**Settings > Automations & scenes > Blueprints**, select **Import blueprint** and enter the address.

- *Wall panel: keep the battery between two limits*: [Import in Home Assistant](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FHalumar%2Fintegration%2Fblob%2Fmain%2Fcustom_components%2Fhalumar%2Fblueprints%2Fautomation%2Fpanel_battery_protection.yaml)  
  `https://github.com/Halumar/integration/blob/main/custom_components/halumar/blueprints/automation/panel_battery_protection.yaml`
- *Doorbell: show a camera on an Apple TV*: [Import in Home Assistant](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FHalumar%2Fintegration%2Fblob%2Fmain%2Fcustom_components%2Fhalumar%2Fblueprints%2Fautomation%2Fdoorbell_camera_apple_tv.yaml)  
  `https://github.com/Halumar/integration/blob/main/custom_components/halumar/blueprints/automation/doorbell_camera_apple_tv.yaml`
- *Doorbell: show a camera on an Android TV or Google TV*: [Import in Home Assistant](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FHalumar%2Fintegration%2Fblob%2Fmain%2Fcustom_components%2Fhalumar%2Fblueprints%2Fautomation%2Fdoorbell_camera_google_tv.yaml)  
  `https://github.com/Halumar/integration/blob/main/custom_components/halumar/blueprints/automation/doorbell_camera_google_tv.yaml`

Home Assistant saves an imported blueprint under `<config>/blueprints/automation/Halumar/` (the
folder is named after the GitHub owner), not in the `halumar/` folder the integration copies to. So
an import next to a bundled copy gives two blueprints with the same name. Import a blueprint only
if you deleted the bundled copy or want to update it with **Re-import blueprint**; then move your
automations to the imported one and delete the bundled copy. On a file system that ignores case,
`Halumar/` and `halumar/` are the same folder, and the import meets the bundled file of the same
name.
