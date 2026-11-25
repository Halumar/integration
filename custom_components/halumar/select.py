"""Select platform for Halumar: brightness mode of a wall panel."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant

from .coordinator import HubConfigEntry
from .panel import BRIGHTNESS_MODES, PanelControlEntity, async_setup_panel_platform

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

# Commands wait for the panel's answer (up to 5 s in the hub): one at a time.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HubConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the panel brightness mode selects the hub wants."""
    async_setup_panel_platform(
        entry,
        "select",
        async_add_entities,
        {"panel_brightness_mode": PanelBrightnessModeSelect},
    )


class PanelBrightnessModeSelect(PanelControlEntity, SelectEntity):
    """auto = profile rule panel_brightness, manual = set from HA (admins and automations only)."""

    _platform = "select"
    _key = "panel_brightness_mode"
    _object_name = "Panel Brightness Mode"
    _attr_translation_key = "panel_brightness_mode"
    _attr_options = BRIGHTNESS_MODES
    _attr_entity_registry_enabled_default = False

    @property
    def current_option(self) -> str | None:
        """Mode reported by the panel."""
        mode = (self.panel or {}).get("brightness_mode")
        return mode if mode in BRIGHTNESS_MODES else None

    async def async_select_option(self, option: str) -> None:
        """Switch between auto and manual."""
        await self.async_command("set_brightness_mode", mode=option)
