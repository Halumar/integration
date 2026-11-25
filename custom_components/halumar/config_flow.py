"""Config flow for Halumar integration.

Flows provided:
1. Manual setup (user step): host, port, integration API token, optional SSL
2. Supervisor discovery (hassio step): consumes the hub's discovery
   payload {host, port, api_token, version}; re-discovery of a configured
   instance rotates host/port/token on the existing entry and reloads it
   (self-healing token rotation), also when its setup had failed
3. Reauthentication (reauth step): triggered by 401s from the coordinator
   when no renewed token is available (token_renewal.py), asks for a
   replacement token
4. Reconfiguration (reconfigure step): change host, port, token and SSL of
   the existing entry
5. Options flow: settings that are not needed to connect (guest button)

All flows validate credentials against the authenticated
GET /api/integration/status endpoint (200 = valid, 401 = invalid token,
anything else = cannot connect).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

import aiohttp

from homeassistant.config_entries import (
    SOURCE_HASSIO,
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from homeassistant.loader import async_get_integration

from .brand import BRAND_NAME
from .const import (
    CONF_API_TOKEN,
    CONF_GUEST_BUTTON_ALL_AREAS,
    CONF_HUB_INSTANCE_ID,
    CONF_USE_SSL,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_USE_SSL,
    DOMAIN,
    ENTRY_UNIQUE_ID,
    HUB_DEVICE_NAME,
)
from .coordinator import hub_instance_id
from .token_renewal import async_announced_configs

try:
    import probatio as vol
except ImportError:  # pragma: no cover - HA < 2026.9 validates with voluptuous
    import voluptuous as vol  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

# Host validation: must be a valid hostname, IPv4, or IPv6 address
_HOST_PATTERN = re.compile(
    r"^("
    r"localhost|"
    r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|"  # IPv4
    r"\[?[a-f0-9:]+\]?|"  # IPv6
    r"[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*"  # hostname
    r")$",
    re.IGNORECASE,
)


def _validate_host_port(user_input: dict[str, Any]) -> dict[str, str]:
    """Normalize host/port in place and return form errors.

    Runs in the step handlers rather than in the data_schema: the frontend
    serializes the schema, and plain functions are not serializable
    (voluptuous_serialize / probatio raise, the flow view returns HTTP 500).
    """
    errors: dict[str, str] = {}
    host = str(user_input[CONF_HOST]).strip()
    if not _HOST_PATTERN.match(host):
        errors[CONF_HOST] = "invalid_host"
    port = user_input[CONF_PORT]
    if not 1 <= port <= 65535:
        errors[CONF_PORT] = "invalid_port"
    user_input[CONF_HOST] = host
    return errors


# Loopback hosts where plain-HTTP traffic never leaves the machine, so the
# cleartext-token warning does not apply.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_local_host(host: str) -> bool:
    """Return True if host is a loopback address (no off-box exposure)."""
    normalized = str(host).strip().strip("[]").lower()
    return normalized in _LOCAL_HOSTS


# The token is a secret: always render it as a password field.
TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.Coerce(int),
        vol.Required(CONF_API_TOKEN): TOKEN_SELECTOR,
        vol.Optional(CONF_USE_SSL, default=DEFAULT_USE_SSL): bool,
    }
)

STEP_REAUTH_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_TOKEN): TOKEN_SELECTOR,
    }
)

# Reconfigure: the token field may stay empty (keeps the stored token); the
# current host, port and SSL setting are suggested.
STEP_RECONFIGURE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT): vol.Coerce(int),
        vol.Optional(CONF_API_TOKEN): TOKEN_SELECTOR,
        vol.Optional(CONF_USE_SSL): bool,
    }
)

# Discovery confirmation has no fields: host, port and token come from the
# Supervisor payload and are not editable here.
STEP_HASSIO_CONFIRM_DATA_SCHEMA = vol.Schema({})


async def _async_validate_connection(
    hass: HomeAssistant,
    host: str,
    port: int,
    token: str,
    use_ssl: bool,
    *,
    supervisor_network: bool = False,
) -> str | None:
    """Validate credentials against the authenticated integration status API.

    GET /api/integration/status with the supplied token: 200 means the token
    is valid, 401 raises InvalidAuth, anything else raises CannotConnect.
    Returns the hub's ``hub.instance_id`` of that answer (None when it names
    none; the first poll then stores it).
    ``supervisor_network`` marks the hub host announced by Supervisor
    discovery: it is only reachable over the internal Supervisor network.
    """
    scheme = "https" if use_ssl else "http"
    url = f"{scheme}://{host}:{port}/api/integration/status"

    # Warn (non-blocking) when the integration token would be sent in
    # cleartext to a host on the network. We deliberately do not change the
    # SSL default so plain-HTTP LAN installs keep working unchanged. Loopback
    # and the internal Supervisor network (the discovered hub host, e.g.
    # "local-<slug>") never leave the machine.
    if not use_ssl and not supervisor_network and not _is_local_host(host):
        _LOGGER.warning(
            "The %s Hub is configured without SSL for remote host %s: "
            "the integration token is transmitted in cleartext and can be "
            "intercepted on the network; enable SSL or use the local Hub",
            BRAND_NAME,
            host,
        )

    integration = await async_get_integration(hass, DOMAIN)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Hub-Integration-Version": str(integration.version),
    }

    session = async_get_clientsession(hass)
    async with session.get(
        url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
    ) as resp:
        if resp.status == 401:
            raise InvalidAuth("Invalid API token")
        if resp.status != 200:
            raise CannotConnect(f"HTTP {resp.status}")
        try:
            data = await resp.json(content_type=None)
        except ValueError:
            return None
    return hub_instance_id(data)


async def _async_validate_to_errors(
    hass: HomeAssistant,
    host: str,
    port: int,
    token: str,
    use_ssl: bool,
    *,
    supervisor_network: bool = False,
) -> tuple[dict[str, str], str | None]:
    """Validate a connection and map failures to form errors.

    Returns (errors, instance id): errors is empty when the credentials are
    valid, otherwise a dict suitable for async_show_form(errors=...).
    """
    try:
        iid = await _async_validate_connection(
            hass,
            host,
            port,
            token,
            use_ssl,
            supervisor_network=supervisor_network,
        )
    except InvalidAuth:
        return {"base": "invalid_auth"}, None
    except (CannotConnect, aiohttp.ClientError, TimeoutError):
        return {"base": "cannot_connect"}, None
    except Exception:
        _LOGGER.exception("Unexpected error validating the hub connection")
        return {"base": "unknown"}, None
    return {}, iid


def _discovery_data(config: Mapping[str, Any]) -> dict[str, Any] | None:
    """Entry data from the hub's announcement {host, port, api_token, version}.

    None when host, port or token is missing or the port is not a number.
    The hub is reached over the internal Supervisor network, so plain HTTP
    (use_ssl False) is correct.
    """
    host = config.get("host")
    port = config.get("port")
    token = config.get("api_token")
    if not host or not token or port is None:
        return None
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    return {
        CONF_HOST: str(host),
        CONF_PORT: port,
        CONF_API_TOKEN: str(token),
        CONF_USE_SSL: False,
    }


def _with_hub_id(data: dict[str, Any], iid: str | None) -> dict[str, Any]:
    """Entry data with the hub's instance id when the answer named one."""
    return {**data, CONF_HUB_INSTANCE_ID: iid} if iid is not None else dict(data)


class HubConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Halumar."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery: dict[str, Any] | None = None
        self._discovery_title: str = BRAND_NAME
        self._discovery_version: str = "unknown"
        # Entry data waiting for the admin's "this is a different hub" answer.
        self._other_hub_updates: dict[str, Any] | None = None
        self._other_hub_host: str = ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HubOptionsFlow:
        """Create the options flow."""
        return HubOptionsFlow()

    # ── Manual setup ─────────────────────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle setup started by the user.

        When the Supervisor already holds exactly one announcement of the
        hub, the flow continues like a discovery (confirmation, no token to
        enter). Home Assistant only turns announcements into discovery flows
        at its start or when they arrive, so after an install without a
        restart the announcement of a running hub would otherwise go unused.
        Without a usable announcement the manual form follows.
        """
        # Single-instance guard: exactly one Halumar entry per installation.
        # Enforced here (not via manifest single_config_entry) because HA
        # aborts discovery flows for single_config_entry integrations before
        # async_step_hassio runs, which would break re-discovery token
        # rotation.
        if self._async_current_entries(include_ignore=False):
            return self.async_abort(reason="already_configured")

        if user_input is None:
            announced = await async_announced_configs(self.hass)
            staged = _discovery_data(announced[0]) if len(announced) == 1 else None
            if staged is not None:
                await self.async_set_unique_id(ENTRY_UNIQUE_ID)
                self._abort_if_unique_id_configured()
                self._discovery = staged
                self._discovery_title = HUB_DEVICE_NAME
                self._discovery_version = str(announced[0].get("version") or "unknown")
                return await self.async_step_hassio_confirm()

        errors: dict[str, str] = {}

        if user_input is not None and not (errors := _validate_host_port(user_input)):
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            token = user_input[CONF_API_TOKEN]
            use_ssl = user_input.get(CONF_USE_SSL, DEFAULT_USE_SSL)

            # Fixed unique id shared with the hassio flow (guards races too)
            await self.async_set_unique_id(ENTRY_UNIQUE_ID)
            self._abort_if_unique_id_configured()

            errors, iid = await _async_validate_to_errors(
                self.hass, host, port, token, use_ssl
            )
            if not errors:
                return self.async_create_entry(
                    title=f"{BRAND_NAME} ({host}:{port})",
                    data=_with_hub_id(
                        {
                            CONF_HOST: host,
                            CONF_PORT: port,
                            CONF_API_TOKEN: token,
                            CONF_USE_SSL: use_ssl,
                        },
                        iid,
                    ),
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    # ── Supervisor discovery ─────────────────────────────────────────────

    async def async_step_hassio(
        self, discovery_info: HassioServiceInfo
    ) -> ConfigFlowResult:
        """Handle Supervisor discovery of the hub.

        The hub announces service "halumar" with the payload
        {host, port, api_token, version}. On re-discovery of an existing
        entry, host/port/token are updated in place (the hub rotates the
        integration token on every startup) and the entry reloads
        automatically.
        """
        config = discovery_info.config
        staged = _discovery_data(config)
        if staged is None:
            return self.async_abort(reason="invalid_discovery_info")

        entry = await self.async_set_unique_id(ENTRY_UNIQUE_ID)
        updates = {
            CONF_HOST: staged[CONF_HOST],
            CONF_PORT: staged[CONF_PORT],
            CONF_API_TOKEN: staged[CONF_API_TOKEN],
        }
        if (
            entry is not None
            and entry.state is ConfigEntryState.SETUP_ERROR
            and entry.disabled_by is None
        ):
            # HA reloads a loaded or retrying entry on changed data, but not
            # one that failed (e.g. the hub rejected the old token while
            # Core and the hub restarted together): reload it as well, so
            # the announced token is used without a manual step.
            return self.async_update_reload_and_abort(
                entry, data_updates=updates, reason="already_configured"
            )
        self._abort_if_unique_id_configured(updates=updates)

        # First-time discovery: stage the data and ask the user to confirm.
        # Re-discovery of an existing entry (above) only rotates
        # host/port/token.
        self._discovery = staged
        self._discovery_title = discovery_info.name or BRAND_NAME
        self._discovery_version = str(config.get("version") or "unknown")
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm setup of a newly discovered Halumar Hub."""
        if self._discovery is None:
            return self.async_abort(reason="invalid_discovery_info")

        errors: dict[str, str] = {}

        if user_input is not None:
            errors, iid = await _async_validate_to_errors(
                self.hass,
                self._discovery[CONF_HOST],
                self._discovery[CONF_PORT],
                self._discovery[CONF_API_TOKEN],
                self._discovery[CONF_USE_SSL],
                supervisor_network=True,
            )
            if not errors:
                return self.async_create_entry(
                    title=self._discovery_title,
                    data=_with_hub_id(self._discovery, iid),
                )

        return self.async_show_form(
            step_id="hassio_confirm",
            data_schema=STEP_HASSIO_CONFIRM_DATA_SCHEMA,
            description_placeholders={
                "addon": self._discovery_title,
                "version": self._discovery_version,
            },
            errors=errors,
        )

    # ── Reauthentication ─────────────────────────────────────────────────

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauth after the hub rejected the stored token."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for and validate a replacement API token."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            token = user_input[CONF_API_TOKEN]
            errors, iid = await _async_validate_to_errors(
                self.hass,
                entry.data[CONF_HOST],
                entry.data[CONF_PORT],
                token,
                entry.data.get(CONF_USE_SSL, DEFAULT_USE_SSL),
                supervisor_network=entry.source == SOURCE_HASSIO,
            )
            if not errors:
                return await self._async_take(
                    entry, {CONF_API_TOKEN: token}, iid, entry.data[CONF_HOST]
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_DATA_SCHEMA,
            description_placeholders={"host": str(entry.data.get(CONF_HOST, ""))},
            errors=errors,
        )

    # ── Another hub ──────────────────────────────────────────────────────

    async def _async_take(
        self,
        entry: ConfigEntry,
        updates: dict[str, Any],
        iid: str | None,
        host: str,
    ) -> ConfigFlowResult:
        """Save validated settings; another hub than the entry's needs a Yes.

        The entry keeps the instance id of its hub. When the validated
        answer names another one, the admin first confirms that this is the
        hub to take (its devices and users replace the previous hub's);
        only then is the new id stored.
        """
        stored = entry.data.get(CONF_HUB_INSTANCE_ID)
        if iid is not None and stored is not None and iid != stored:
            self._other_hub_updates = {**updates, CONF_HUB_INSTANCE_ID: iid}
            self._other_hub_host = host
            return await self.async_step_other_hub()
        return self.async_update_reload_and_abort(
            entry, data_updates=_with_hub_id(updates, iid)
        )

    async def async_step_other_hub(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm that a different hub replaces the entry's hub."""
        assert self._other_hub_updates is not None  # set by _async_take
        if user_input is not None:
            entry = (
                self._get_reauth_entry()
                if self.source == SOURCE_REAUTH
                else self._get_reconfigure_entry()
            )
            return self.async_update_reload_and_abort(
                entry,
                data_updates=self._other_hub_updates,
                reason="other_hub_confirmed",
            )
        return self.async_show_form(
            step_id="other_hub",
            data_schema=vol.Schema({}),
            description_placeholders={"host": self._other_hub_host},
        )

    # ── Reconfiguration ──────────────────────────────────────────────────

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the connection settings of the existing entry.

        An empty token field keeps the stored token. The new settings are
        validated against the hub before the entry is updated and
        reloaded.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input = dict(user_input)
            if not user_input.get(CONF_API_TOKEN):
                user_input[CONF_API_TOKEN] = entry.data[CONF_API_TOKEN]
            user_input.setdefault(CONF_USE_SSL, DEFAULT_USE_SSL)
            errors = _validate_host_port(user_input)
            if not errors:
                errors, iid = await _async_validate_to_errors(
                    self.hass,
                    user_input[CONF_HOST],
                    user_input[CONF_PORT],
                    user_input[CONF_API_TOKEN],
                    user_input[CONF_USE_SSL],
                )
            if not errors:
                return await self._async_take(
                    entry, user_input, iid, user_input[CONF_HOST]
                )

        suggested = {
            CONF_HOST: entry.data.get(CONF_HOST),
            CONF_PORT: entry.data.get(CONF_PORT),
            CONF_USE_SSL: entry.data.get(CONF_USE_SSL, DEFAULT_USE_SSL),
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_RECONFIGURE_DATA_SCHEMA, suggested
            ),
            description_placeholders={"host": str(entry.data.get(CONF_HOST, ""))},
            errors=errors,
        )


class HubOptionsFlow(OptionsFlow):
    """Handle the Halumar options (settings not needed to connect).

    Connection settings are changed in the reconfigure flow. The only
    option is read when the guest pairing button is pressed, so saving it
    needs no reload.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_GUEST_BUTTON_ALL_AREAS: bool(
                        user_input.get(CONF_GUEST_BUTTON_ALL_AREAS, False)
                    )
                }
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_GUEST_BUTTON_ALL_AREAS,
                        default=bool(
                            self.config_entry.options.get(
                                CONF_GUEST_BUTTON_ALL_AREAS, False
                            )
                        ),
                    ): bool,
                }
            ),
        )


class CannotConnect(Exception):
    """Error to indicate we cannot connect."""


class InvalidAuth(Exception):
    """Error to indicate there is invalid auth."""
