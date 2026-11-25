"""Number platform for Halumar: brightness of a wall panel."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant

from .coordinator import HubConfigEntry
from .panel import PanelControlEntity, async_setup_panel_platform

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

# Commands wait for the panel's answer (up to 5 s in the hub): one at a time.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HubConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the panel brightness numbers the hub wants."""
    async_setup_panel_platform(
        entry,
        "number",
        async_add_entities,
        {"panel_brightness_control": PanelBrightnessNumber},
    )


class PanelBrightnessNumber(PanelControlEntity, NumberEntity):
    """Backlight of the panel, 5–100 % (admins and automations only)."""

    _platform = "number"
    _key = "panel_brightness_control"
    _object_name = "Panel Brightness"
    _attr_translation_key = "panel_brightness"
    _attr_native_min_value = 5
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    @property
    def native_value(self) -> float | None:
        """Actual backlight in percent."""
        value = (self.panel or {}).get("brightness")
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(round(value * 100))

    async def async_set_native_value(self, value: float) -> None:
        """Set the backlight (manual until the next phase change)."""
        await self.async_command("set_brightness", level=round(value / 100, 3))
