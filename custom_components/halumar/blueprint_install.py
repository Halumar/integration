"""Blueprints shipped with the integration.

Home Assistant only populates the blueprints of its own integrations. At the
first setup of a config entry this copies ours to
``<config>/blueprints/<domain>/<DOMAIN>/`` and records that in the entry's
data. Later setups (reloads, restarts) copy nothing, so a blueprint the user
deleted stays deleted. A file that already exists is never overwritten (the
user may have edited it).
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_BLUEPRINTS_INSTALLED, DOMAIN

_LOGGER = logging.getLogger(__name__)

SOURCE = Path(__file__).parent / "blueprints"


def _install(target_root: Path) -> list[str]:
    installed = []
    for src in sorted(SOURCE.glob("*/*.yaml")):
        dest = target_root / src.parent.name / DOMAIN / src.name
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        installed.append(f"{src.parent.name}/{DOMAIN}/{src.name}")
    return installed


async def async_install_blueprints(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Copy the blueprints at the entry's first setup (best effort).

    The entry's data records the copy. A failed copy is not recorded, so the
    next setup tries again. There is no update listener, so changing the data
    does not reload the entry.
    """
    if entry.data.get(CONF_BLUEPRINTS_INSTALLED):
        return
    try:
        installed = await hass.async_add_executor_job(
            _install, Path(hass.config.path("blueprints"))
        )
    except OSError as err:
        _LOGGER.warning("Could not install the %s blueprints: %s", DOMAIN, err)
        return
    if installed:
        _LOGGER.info("Installed blueprints: %s", ", ".join(installed))
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_BLUEPRINTS_INSTALLED: True}
    )
