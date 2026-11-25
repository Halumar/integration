"""Report Home Assistant's external URL to the hub.

Apps with remote access reach Home Assistant from outside the home network
through its external URL. Home Assistant knows it best: its configured
``external_url`` or, without one, the Nabu Casa remote UI address
(``get_url`` without internal URLs). The hub cannot see the cloud address
itself, so the integration reports the result to
``POST /api/integration/ha-url`` (null = none). The hub hands it only to
devices with remote access; a URL the admin entered in the hub
takes precedence there.

Every report also carries Home Assistant's instance ID
(``ha_instance_id``, the ``uuid`` of its Bonjour announcement). The hub hands
it to the apps, which recognise their Home Assistant by it when the hub does
not answer (second proof of the home network for an unencrypted address).

Reported once at setup and again whenever it changes: on a change of HA's
core configuration, of the cloud connection, and checked every
``CHECK_INTERVAL`` (e.g. remote access switched on or off in the cloud
settings, which fires no event). Nothing is sent while the value is
unchanged. A failed report is retried with the next check.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.const import EVENT_CORE_CONFIG_UPDATE
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import instance_id
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import DOMAIN
from .coordinator import HubApiError

if TYPE_CHECKING:
    from .coordinator import HubConfigEntry, HubCoordinator

_LOGGER = logging.getLogger(__name__)

HA_URL_PATH = "/api/integration/ha-url"
CHECK_INTERVAL = timedelta(seconds=60)

_UNSET: Any = object()


def external_url(hass: HomeAssistant) -> str | None:
    """HA's external URL: ``external_url``, else the Nabu Casa address."""
    try:
        url = get_url(
            hass,
            allow_internal=False,
            allow_ip=True,
            prefer_external=True,
            prefer_cloud=False,
        )
    except NoURLAvailableError:
        return None
    return url.rstrip("/") or None


class ExternalUrlReporter:
    """Keeps the hub's view of HA's external URL current."""

    def __init__(
        self, hass: HomeAssistant, entry: HubConfigEntry, coordinator: HubCoordinator
    ) -> None:
        """Store collaborators; no side effects."""
        self.hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._reported: Any = _UNSET
        # A URL the hub refused (422) is not offered again until it changes.
        self._refused: Any = _UNSET
        self._running = False
        self._again = False

    @property
    def reported(self) -> str | None:
        """Last URL the hub accepted (tests/diagnostics)."""
        return None if self._reported is _UNSET else self._reported[0]

    @callback
    def async_start(self) -> None:
        """Report now and follow every change while the entry is loaded."""
        entry = self._entry
        entry.async_on_unload(
            self.hass.bus.async_listen(EVENT_CORE_CONFIG_UPDATE, self._on_event)
        )
        entry.async_on_unload(
            async_track_time_interval(self.hass, self._on_event, CHECK_INTERVAL)
        )
        if "cloud" in self.hass.config.components:
            from homeassistant.components.cloud import (
                async_listen_connection_change,
            )

            entry.async_on_unload(
                async_listen_connection_change(self.hass, self._on_cloud_change)
            )
        self.async_schedule()

    @callback
    def _on_event(self, _event: Event | Any = None) -> None:
        self.async_schedule()

    @callback
    def _on_cloud_change(self, _state: Any) -> None:
        self.async_schedule()

    @callback
    def async_schedule(self) -> None:
        """Check (and report on change) in the background."""
        if self._running:
            self._again = True
            return
        self._entry.async_create_background_task(
            self.hass, self.async_report(), name=f"{DOMAIN}-external-url"
        )

    async def async_report(self) -> bool:
        """Send the URL if it changed. True when the hub has the current one."""
        if self._running:
            self._again = True
            return False
        self._running = True
        try:
            while True:
                self._again = False
                ok = await self._async_report_once()
                if not self._again:
                    return ok
        finally:
            self._running = False

    async def _async_report_once(self) -> bool:
        url = external_url(self.hass)
        ha_id = await instance_id.async_get(self.hass)
        report = (url, ha_id)
        if report == self._reported:
            return True
        if report == self._refused:
            return False
        try:
            await self._coordinator.api_post_json(
                HA_URL_PATH, {"external_url": url, "ha_instance_id": ha_id}
            )
        except HubApiError as err:
            if err.status == 422:
                # The hub keeps the instance ID even when it refuses the URL.
                self._refused = report
                _LOGGER.warning(
                    "The hub did not accept Home Assistant's external URL: %s",
                    err.detail,
                )
            else:
                _LOGGER.debug("Reporting the external URL failed, will retry: %s", err)
            return False
        except Exception as err:  # noqa: BLE001 - retried with the next check
            _LOGGER.debug("Reporting the external URL failed, will retry: %s", err)
            return False
        self._reported = report
        _LOGGER.debug(
            "Reported Home Assistant's external URL to the hub (%s)",
            "set" if url else "none",
        )
        return True
