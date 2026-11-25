# Halumar integration documentation

These pages describe the Halumar custom integration for Home Assistant (domain `halumar`) as it
is in this repository. Profiles, the editor and pairing screens belong to the
[Halumar Hub](https://github.com/Halumar/hub) and are documented there.

## For Home Assistant users

| Page | What it covers |
|---|---|
| [Installation](installation.md) | Marketplace or HACS (from the repository link), manual install, what the repository declares, updates, removal |
| [Configuration](configuration.md) | Discovery, manual setup, reauthentication, reconfigure, the "different hub" check, options |
| [Concepts](concepts.md) | Devices, profiles and rule snapshots, Home Assistant access (Hub only, Checked, Direct), guests, parental controls, access hours, remote access, location |
| [Entities](entities.md) | Every device and entity the integration creates, with entity ids and who may operate them |
| [Actions](actions.md) | Every `halumar.*` action with its fields, limits and who may call it |
| [Events and repairs](events.md) | Events on the Home Assistant event bus, repairs the integration raises |
| [Automations](automations.md) | Examples and the bundled blueprints |
| [Security model](security-model.md) | What is checked where, the Home Assistant users and tokens, and the limits |
| [Troubleshooting](troubleshooting.md) | Real messages from the integration and what to do |

## For contributors

| Page | What it covers |
|---|---|
| [Architecture](architecture.md) | Modules, runtime behavior, data flow to and from the hub |
| [Releases and versioning](releasing.md) | Version scheme, release workflow, changelog |
| [Contributing](../CONTRIBUTING.md) | Development setup and the checks CI runs |

## Conventions in these pages

- Display names are the English ones from `strings.json`; Home Assistant shows the German ones
  when its language is German.
- `<client>` in an entity id stands for the device name as Home Assistant turns it into an id
  (lower case, `_` instead of spaces). Guests have `guest_` in front of it.
- The examples use `homeassistant.local` and `192.0.2.10` as addresses.
