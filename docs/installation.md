# Installation

## Before you start

- Home Assistant **2025.11.0** or later.
- The **Halumar Hub** app, version **2026.12.0** or later. The hub is a Home Assistant app, so it
  needs Home Assistant OS, or another installation with the Supervisor that can run apps. See
  [Halumar/hub](https://github.com/Halumar/hub).
- No Python packages are installed: the manifest lists no requirements.

## With the Marketplace or HACS

Home Assistant 2026.11 and newer install community integrations through the **Marketplace**
(**Settings > Marketplace**); older versions use [HACS](https://hacs.xyz/). Halumar is not in their
catalog yet, so you add this repository from its link. This button opens the repository in the
Marketplace, or in HACS before Home Assistant 2026.11:

[![Open your Home Assistant instance and show the Halumar repository in the Marketplace or in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Halumar&repository=integration&category=integration)

### Marketplace (Home Assistant 2026.11 and newer)

1. Go to **Settings > Marketplace**. The first time, the Marketplace shows a warning: everything
   in it is made and published by the community, not tested, audited or supported by the Home
   Assistant project. Until you accept the warning, Home Assistant cannot install anything from
   the Marketplace.
2. In the upper-right corner, select **Add from link**.
3. Under **GitHub link**, enter `https://github.com/Halumar/integration` (or `Halumar/integration`)
   and select **Add**. Adding a repository from a link needs a GitHub connection. If Home Assistant
   has none yet, the Marketplace asks you to connect GitHub: select **Continue**, open the link it
   shows, enter the code on GitHub and approve the connection. The connection has no access to
   private repositories and cannot change anything on GitHub.
4. Open **Halumar** in the list and select **Install**. The dialog shows the version and reminds
   you that the repository is not reviewed or supported by the Home Assistant project. Select
   **Install**.
5. No restart is needed: when the installation is done, the setup page of the integration opens.
   If the Halumar Hub app is running, the page asks you to confirm the connection to it; no token
   is needed. If you close the page, add the integration later under **Settings > Devices &
   services**.

### HACS (older Home Assistant versions)

You need [HACS](https://hacs.xyz/) installed.

1. In HACS, add `https://github.com/Halumar/integration` as a custom repository of the type
   **Integration**.
2. Open **Halumar** in HACS and download it.
3. Restart Home Assistant. After the restart Home Assistant offers the discovered **Halumar Hub**
   under **Settings > Devices & services**, if the Halumar Hub app is running.

The Marketplace and HACS show this repository's `README.md` as the description page. There is no
`info.md`.

### Updates

A new version is offered once its release is published on GitHub; a tag alone is not enough.

- **Marketplace**: the new version appears under **Settings > Updates**, with its release notes.
  Install it there, then restart Home Assistant; a repair under **Settings > System > Repairs**
  reminds you and can restart for you. For a repository added from a link, the Marketplace looks
  for new versions every 48 hours, and only with a GitHub connection.
- **HACS**: download the new version in HACS and restart Home Assistant.

Hub and integration are released together: install the hub version with the same number. The
integration needs at least the hub version under [Before you start](#before-you-start); with an
older hub Home Assistant shows the repair **Update the Halumar Hub app**.

### If no hub is found

If the setup page asks for host, port and token, Home Assistant found no running hub. Close the
page and start the Halumar Hub app. Then add the integration again under **Settings > Devices &
services**, or restart Home Assistant: after the restart it offers the discovered **Halumar Hub**
there. The form is meant for a hub that runs elsewhere; see
[configuration](configuration.md#manual-setup).

## Manually

1. Download `halumar.zip` from the
   [releases page](https://github.com/Halumar/integration/releases). The zip holds the contents of
   `custom_components/halumar/`.
2. Create the folder `custom_components/halumar/` inside your Home Assistant configuration folder
   (the folder that contains `configuration.yaml`) and unpack the zip into it. Alternatively copy
   the folder `custom_components/halumar/` of a checkout of this repository.
3. Restart Home Assistant.

For an update, replace the folder and restart again.

## What the repository declares

These values come from the files in the repository. They are listed here so you can check them;
change them only through a pull request.

### `hacs.json`

| Key | Value | Meaning |
|---|---|---|
| `name` | `Halumar` | Name shown in the Marketplace and in HACS |
| `homeassistant` | `2025.11.0` | Minimum Home Assistant version the Marketplace and HACS install this release on |

There is no `zip_release`: the Marketplace and HACS install the folder `custom_components/halumar/`
of the release's tag. The `halumar.zip` attached to each release holds the same files, for manual
installs.

### `custom_components/halumar/manifest.json`

| Key | Value |
|---|---|
| `domain` | `halumar` |
| `name` | `Halumar` |
| `version` | The version of the release; it equals the release tag |
| `integration_type` | `hub` |
| `iot_class` | `local_push` (live updates from the hub over a WebSocket, with polling as a fallback) |
| `config_flow` | `true` (set up in the UI; YAML configuration is not supported) |
| `dependencies` | `http`, `websocket_api` |
| `after_dependencies` | `cloud`, `hassio`, `script` |
| `requirements` | none |
| `documentation` | `https://github.com/Halumar/integration` |
| `issue_tracker` | `https://github.com/Halumar/integration/issues` |
| `codeowners` | `@Halumar` (the organization) |

### Brand images

The icon and logo ship inside the integration in `custom_components/halumar/brand/`. Home
Assistant 2026.3 and later shows them from there. Older releases (2025.11 to 2026.2) take brand
images only from the [home-assistant/brands](https://github.com/home-assistant/brands) repository;
the same files are prepared in [`brands/`](../brands/README.md) for that purpose.

### Checks that run in CI

- **hassfest** (`home-assistant/actions/hassfest`): manifest, translations, services and config
  flow follow the Home Assistant rules.
- **HACS action** (`hacs/action`, category `integration`): the checks the Marketplace catalog
  review runs too. It runs once the repository is public, because it reads `hacs.json` and the
  manifest through the GitHub API.

The full list of checks is in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Remove the integration

1. **Settings > Devices & services > Halumar**, open the menu of the entry and select **Delete**.
2. When the entry is deleted, the integration removes every Home Assistant user it created for a
   paired device, together with its token. The apps lose their access to Home Assistant at once.
   Users that are not device users of this integration (owners, administrators, users with a
   password) are never removed.
3. Uninstall the integration: in the Marketplace open the **Halumar** repository, open the menu
   (three dots, upper-right corner) and select **Uninstall**; in HACS remove the repository. For a
   manual install delete the folder `custom_components/halumar/`. Restart Home Assistant. The
   Marketplace does not uninstall an integration that is still set up: it offers **Delete and
   uninstall**, which also deletes the entry (step 2).
4. Uninstall the Halumar Hub app if you stop using Halumar.

Unloading or reloading the entry, or restarting Home Assistant, keeps the device users and their
tokens. Only deleting the entry removes them.
