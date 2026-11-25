# Configuration

The integration is set up in the Home Assistant UI only. YAML configuration is not supported.
There is exactly one Halumar entry per Home Assistant; a second setup aborts with "The Halumar Hub
is already set up. Only one Halumar entry is supported."

Every way of setting up or changing the connection tests it first with an authenticated request to
the hub (`GET /api/integration/status`). The entry is only saved when the hub answers.

## Automatic discovery (recommended)

The Halumar Hub app announces itself through the Supervisor with its host, port, integration token
and version.

1. Start the Halumar Hub app.
2. Open **Settings > Devices & services**. Home Assistant offers the discovered **Halumar Hub**.
3. Add it. The dialog asks: "Do you want to connect Home Assistant to *&lt;hub&gt;* (version
   *&lt;version&gt;*)? The connection details and the token come from the hub and stay up to date
   by themselves."
4. Select **Submit**. The entry is created.

The discovered hub is reached over the internal Supervisor network with plain HTTP; there is no
HTTPS option in this step.

Home Assistant turns the hub's announcement into a discovery when Home Assistant starts and when
the hub announces itself. After an install without a restart (Marketplace), start the setup
yourself: when the Supervisor holds exactly one announcement of the hub, the setup shows the same
dialog instead of the form for host, port and token.

The hub creates a new integration token at every start. Each time, the Supervisor announces it
again and the integration takes over the new host, port and token and reloads itself. This also
works when the entry had failed to set up, for example when Home Assistant and the hub restarted
at the same time. If Home Assistant started with the old token before the announcement arrived,
the integration looks for the token the hub announced for the same host and port and retries once
with it. Only when no renewed token works does Home Assistant ask you to reconnect.

## Manual setup

Use this when discovery does not work or the hub runs elsewhere.

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=halumar)

Or add the integration **Halumar** under **Settings > Devices & services**. If the Supervisor
holds the announcement of a running hub, this shows the confirmation of the discovery instead of
the form below.

| Field | Default | Meaning |
|---|---|---|
| Host | `localhost` | Host name, IPv4 or IPv6 address of the hub, for example `192.0.2.10` or `homeassistant.local`. Leading and trailing spaces are removed. |
| Port | `8099` | Port of the hub API, 1 to 65535. |
| Integration token | (required) | The hub's integration token. It is shown as a password field. |
| Use HTTPS | off | Connect with HTTPS instead of HTTP. Leave it off for the hub on the same Home Assistant system. |

The entry is named `Halumar (<host>:<port>)`.

If you connect with plain HTTP to a host that is not the local machine, the integration logs a
warning, because the token then travels unencrypted over your network:

> The Halumar Hub is configured without SSL for remote host … : the integration token is transmitted
> in cleartext and can be intercepted on the network; enable SSL or use the local Hub

### Form errors

| Message | Cause |
|---|---|
| Invalid host. Use a host name, an IPv4 or an IPv6 address. | The host does not look like a host name or address. |
| The port must be between 1 and 65535. | Port out of range. |
| The Halumar Hub rejected the integration token. | The hub answered HTTP 401. |
| Cannot connect to the Halumar Hub. Check host and port, and that the Halumar Hub app is running. | No connection, a timeout (10 seconds) or any answer other than 200 or 401. |
| Unexpected error. | Anything else; the details are in the Home Assistant log. |

## Reconnect (reauthentication)

When the hub rejects the stored token and no renewed token is available, Home Assistant shows
**Reconnect to the Halumar Hub**: "The Halumar Hub at *&lt;host&gt;* rejected the stored integration
token. Restart the Halumar Hub app (it then hands Home Assistant a new token by itself), or enter
the current token here."

Restarting the Halumar Hub app is usually enough. Or enter the current token; it is tested against
the stored host and port.

## Change the connection (reconfigure)

Open the Halumar entry, open its menu and select **Reconfigure**. You can change host, port, token
and **Use HTTPS**. The current host, port and HTTPS setting are filled in. Leave the token empty to
keep the stored one. The settings are tested before they are saved; the entry then reloads.

## A different hub

The integration remembers which hub it belongs to (the hub's instance id). When the address
suddenly answers as another hub, for example after the hub app was reinstalled without a backup,
the integration changes nothing:

- No device, device user or token is removed, and nothing is sent to that hub except the status
  request. Actions such as **Start pairing** fail with "Not sent: … is a different Halumar Hub than
  the one this entry belongs to …".
- The repair **This is a different Halumar Hub** is shown.

If the new hub is the right one, open **Reconfigure** (or enter the token when Home Assistant asks
to reconnect). The flow then shows **This is a different Halumar Hub**: "If you continue, Home
Assistant takes this hub: the devices and device users of the previous hub that this hub does not
know are removed. Cancel to keep everything as it is." Submit to take the new hub, or point the
integration back at the previous hub.

## Options

Open the Halumar entry and select **Configure**. Host, port and token are not options; use
**Reconfigure** for them.

| Option | Default | Meaning |
|---|---|---|
| Guest pairing button: share all areas (`guest_button_all_areas`) | off | On: guests paired with the **Start guest pairing** button see all areas. Off: the guest profile of the hub decides, and the guest sees no area until you share areas in the hub. |

The option is read when the button is pressed, so saving it needs no reload. The action
`halumar.start_guest_pairing` has its own fields and does not use this option.

## Data updates

- The integration asks the hub for its status every 30 seconds.
- When the hub offers live updates, the integration also keeps a WebSocket connection to it.
  Changes then arrive within about a second, and the regular request slows down to every 5 minutes
  as a safety net. A connection that stays silent for 45 seconds is closed and opened again;
  meanwhile the 30-second interval applies.
- After a pairing window opens, one extra update runs just after its advertised end, so
  **Pairing active** turns off on time.
- When the hub is not reachable, all Halumar entities become unavailable. The log notes the outage
  once and the recovery once.
