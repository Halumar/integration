"""Button platform for Halumar integration.

Creates buttons for:
- Start Pairing: generates a regular pairing PIN
- Start Guest Pairing: generates a guest pairing PIN (24h, local only)
- Cancel Pairing: closes an open pairing window

The start buttons are unavailable while the hub's "allow pairing via
integration" toggle is off; the cancel button is available only while a
pairing window is open.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from .const import ALL_AREAS_TAG, CONF_GUEST_BUTTON_ALL_AREAS
from .coordinator import HubConfigEntry, HubCoordinator, hub_call
from .entity import HubEntity, async_require_admin
from .panel import PanelControlEntity, async_setup_panel_platform

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

_LOGGER = logging.getLogger(__name__)

# One pairing request at a time: the hub holds a single active PIN.
PARALLEL_UPDATES = 1

# HTTP 403 on start-pairing: the hub's "allow pairing via integration"
# toggle is off.
_PAIRING_STATUS_KEYS = {403: "pairing_disabled"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HubConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Halumar button entities."""
    coordinator = entry.runtime_data

    async_add_entities(
        [
            HubStartPairingButton(coordinator, entry),
            HubStartGuestPairingButton(coordinator, entry),
            HubCancelPairingButton(coordinator, entry),
        ]
    )
    # Wall panel buttons.
    async_setup_panel_platform(
        entry,
        "button",
        async_add_entities,
        {
            "panel_wake": PanelWakeButton,
            "panel_screensaver": PanelScreensaverButton,
            "panel_home": PanelHomeButton,
            "panel_reload": PanelReloadButton,
        },
    )


class HubStartPairingButton(HubEntity, ButtonEntity):
    """Button to start a regular pairing session (generates a PIN)."""

    _attr_translation_key = "start_pairing"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: HubCoordinator, entry: HubConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(coordinator, entry, "button", "start_pairing", "Start Pairing")

    @property
    def available(self) -> bool:
        """Unavailable while the hub's pairing-via-integration toggle is off."""
        data = self.coordinator.data or {}
        return super().available and bool(
            data.get("allow_pairing_via_integration", True)
        )

    async def async_press(self) -> None:
        """Handle the button press - start regular pairing."""
        await async_require_admin(self)
        await hub_call(
            "start_pairing",
            self.coordinator.api_post("/api/integration/start-pairing"),
            status_keys=_PAIRING_STATUS_KEYS,
        )
        # The pairing PIN is a secret and must never be logged.
        _LOGGER.debug("Regular pairing started")
        await self.coordinator.async_request_refresh()


class HubStartGuestPairingButton(HubEntity, ButtonEntity):
    """Button to start a guest pairing session (generates a time-limited PIN).

    Uses default guest settings: 24h expiry, no areas unless the option
    "share all areas" is on. For other guest settings use the
    halumar.start_guest_pairing action.
    """

    _attr_translation_key = "start_guest_pairing"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: HubCoordinator, entry: HubConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(
            coordinator, entry, "button", "start_guest_pairing", "Start Guest Pairing"
        )
        self._entry = entry

    @property
    def available(self) -> bool:
        """Unavailable while the hub's pairing-via-integration toggle is off."""
        data = self.coordinator.data or {}
        return super().available and bool(
            data.get("allow_pairing_via_integration", True)
        )

    async def async_press(self) -> None:
        """Handle the button press - start guest pairing with defaults."""
        await async_require_admin(self)
        # Without the option the guest profile of the hub decides the areas
        # (fail closed: only the device's own area); with it the guest
        # sees all areas. Duration: the hub's guest default.
        all_areas = bool(self._entry.options.get(CONF_GUEST_BUTTON_ALL_AREAS, False))
        body: dict[str, Any] = {"is_guest": True}
        if all_areas:
            body["guest_tags"] = [ALL_AREAS_TAG]
        # The integration token is scoped to /api/integration/*, so guest
        # pairing goes through start-pairing with guest parameters.
        await hub_call(
            "start_guest_pairing",
            self.coordinator.api_post_json("/api/integration/start-pairing", body),
            status_keys=_PAIRING_STATUS_KEYS,
        )
        # The pairing PIN is a secret and must never be logged.
        _LOGGER.debug("Guest pairing started")
        await self.coordinator.async_request_refresh()


class HubCancelPairingButton(HubEntity, ButtonEntity):
    """Button to cancel an open pairing window.

    Only available while a pairing window is open; cancelling stays possible
    even when the hub's pairing-via-integration toggle is off.
    """

    _attr_translation_key = "cancel_pairing"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: HubCoordinator, entry: HubConfigEntry) -> None:
        """Initialize the button."""
        super().__init__(
            coordinator, entry, "button", "cancel_pairing", "Cancel Pairing"
        )

    @property
    def available(self) -> bool:
        """Available only while a pairing window is open."""
        data = self.coordinator.data
        return super().available and bool(data and data.get("pairing_active"))

    async def async_press(self) -> None:
        """Handle the button press - cancel the open pairing window."""
        await async_require_admin(self)
        await hub_call(
            "cancel_pairing",
            self.coordinator.api_post("/api/integration/pairing/cancel"),
        )
        _LOGGER.debug("Pairing cancel requested")
        await self.coordinator.async_request_refresh()


# ── Wall panel: admins and automations only ───────────────────────────


class _PanelButton(PanelControlEntity, ButtonEntity):
    """A panel command as a button."""

    _platform = "button"
    _action: str

    async def async_press(self) -> None:
        """Send the command (admin or automation only)."""
        await self.async_command(self._action)


class PanelWakeButton(_PanelButton):
    """Wake the panel like a touch."""

    _key = "panel_wake"
    _object_name = "Panel Wake"
    _attr_translation_key = "panel_wake"
    _action = "wake"


class PanelScreensaverButton(_PanelButton):
    """Start the screensaver now."""

    _key = "panel_screensaver"
    _object_name = "Panel Screensaver"
    _attr_translation_key = "panel_screensaver"
    _action = "screensaver"


class PanelHomeButton(_PanelButton):
    """Back to the start area."""

    _key = "panel_home"
    _object_name = "Panel Home"
    _attr_translation_key = "panel_home"
    _action = "home"


class PanelReloadButton(_PanelButton):
    """Reload areas and states."""

    _key = "panel_reload"
    _object_name = "Panel Reload"
    _attr_translation_key = "panel_reload"
    _attr_entity_registry_enabled_default = False
    _action = "reload"
