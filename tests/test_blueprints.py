"""Bundled blueprints.

Every blueprint is a valid automation. The doorbell blueprints open the
camera on the TV: Apple TV through media_player.play_media (url), Google TV
through remote.turn_on (activity), with the link https://<website>/open/…
(preferred) or halumar://open/…. The blueprints are copied at the first
setup of a config entry only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aioresponses import aioresponses

from custom_components.halumar import blueprint_install
from custom_components.halumar.brand import BRAND_NAME
from custom_components.halumar.const import CONF_BLUEPRINTS_INSTALLED, DOMAIN
from homeassistant.components.blueprint import models, schemas
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.template import Template
from homeassistant.util.yaml import load_yaml_dict

from ._ha import MockConfigEntry
from .common import setup_integration

APPLE = "doorbell_camera_apple_tv.yaml"
GOOGLE = "doorbell_camera_google_tv.yaml"
BATTERY = "panel_battery_protection.yaml"


def _blueprint(name: str) -> models.Blueprint:
    data = load_yaml_dict(str(blueprint_install.SOURCE / "automation" / name))
    return models.Blueprint(
        data, expected_domain="automation", schema=schemas.BLUEPRINT_SCHEMA
    )


def _substituted(name: str, inputs: dict[str, Any]) -> dict[str, Any]:
    bp = _blueprint(name)
    bp_inputs = models.BlueprintInputs(
        bp, {"use_blueprint": {"path": name, "input": inputs}}
    )
    bp_inputs.validate()
    return bp_inputs.async_substitute()


def _link(hass: HomeAssistant, config: dict[str, Any]) -> str:
    variables = dict(config["variables"])
    variables.pop("link")
    tpl = Template(config["variables"]["link"], hass)
    return str(tpl.async_render(variables)).strip()


@pytest.mark.parametrize(
    ("name", "inputs", "action"),
    [
        (
            APPLE,
            {
                "doorbell": "event.front_door_bell",
                "camera": "camera.front_door",
                "apple_tv": "media_player.living_room",
            },
            "media_player.play_media",
        ),
        (
            GOOGLE,
            {
                "doorbell": "binary_sensor.doorbell",
                "camera": "camera.front_door",
                "tv_remote": "remote.living_room_tv",
            },
            "remote.turn_on",
        ),
    ],
)
@pytest.mark.parametrize(
    ("form", "expected"),
    [
        ("website", "https://halumar.com/open/camera/camera.front_door"),
        ("app", "halumar://open/camera/camera.front_door"),
    ],
)
async def test_doorbell_blueprint_opens_the_camera(
    hass: HomeAssistant,
    name: str,
    inputs: dict[str, Any],
    action: str,
    form: str,
    expected: str,
) -> None:
    config = _substituted(name, {**inputs, "link_form": form})
    assert config["mode"] == "single"
    assert [a.get("action") for a in config["actions"] if "action" in a] == [action]
    assert _link(hass, config) == expected


async def test_doorbell_blueprint_own_website(hass: HomeAssistant) -> None:
    config = _substituted(
        APPLE,
        {
            "doorbell": "event.bell",
            "camera": "camera.door",
            "apple_tv": "media_player.tv",
            "website": "https://example.org/",
        },
    )
    assert _link(hass, config) == "https://example.org/open/camera/camera.door"


def test_doorbell_blueprints_only_open_a_view() -> None:
    """The link never switches anything: no halumar.* or switching actions."""
    for name in (APPLE, GOOGLE):
        text = (blueprint_install.SOURCE / "automation" / name).read_text("utf-8")
        assert "~ '/camera/' ~ camera" in text
        assert "action: halumar." not in text
        assert _blueprint(name).metadata["homeassistant"]["min_version"] == "2025.11.0"


BUNDLED = sorted(
    p.name for p in (blueprint_install.SOURCE / "automation").glob("*.yaml")
)
INPUTS = {
    APPLE: {
        "doorbell": "event.front_door_bell",
        "camera": "camera.front_door",
        "apple_tv": "media_player.living_room",
    },
    GOOGLE: {
        "doorbell": "binary_sensor.doorbell",
        "camera": "camera.front_door",
        "tv_remote": "remote.living_room_tv",
    },
    BATTERY: {
        "battery_sensor": "sensor.halumar_wall_tablet_battery",
        "charger": "switch.wall_tablet_charger",
    },
}


def test_every_bundled_blueprint_has_inputs_here() -> None:
    assert set(BUNDLED) == set(INPUTS)


@pytest.mark.parametrize("name", BUNDLED)
async def test_bundled_blueprint_is_a_valid_automation(
    hass: HomeAssistant, name: str
) -> None:
    """Blueprint schema, inputs and the resulting automation config are valid."""
    from homeassistant.components.automation.config import async_validate_config_item

    metadata = _blueprint(name).metadata
    assert metadata["author"] == BRAND_NAME
    assert "source_url" not in metadata  # Home Assistant sets it on import
    assert metadata["homeassistant"]["min_version"] == "2025.11.0"
    config = _substituted(name, INPUTS[name])
    validated = await async_validate_config_item(
        hass, "test", {"id": "test", "alias": "test", **config}
    )
    assert validated is not None
    assert validated.validation_status.value == "ok", validated.validation_error


# ── installation at the entry's first setup ────────────────────────────────


def _installed(target: Path) -> set[str]:
    return {p.name for p in (target / "automation" / DOMAIN).glob("*.yaml")}


async def test_first_setup_copies_the_blueprints(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    blueprints_in_tmp: Path,
) -> None:
    assert CONF_BLUEPRINTS_INSTALLED not in config_entry.data
    await setup_integration(hass, mock_api, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert _installed(blueprints_in_tmp) == set(BUNDLED)
    assert config_entry.data[CONF_BLUEPRINTS_INSTALLED] is True
    text = (blueprints_in_tmp / "automation" / DOMAIN / BATTERY).read_text("utf-8")
    assert "integration: halumar" in text


async def test_reload_and_restart_never_copy_again(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    blueprints_in_tmp: Path,
) -> None:
    """A deleted blueprint stays deleted, an edited one stays edited."""
    await setup_integration(hass, mock_api, config_entry)
    folder = blueprints_in_tmp / "automation" / DOMAIN
    (folder / BATTERY).unlink()
    (folder / APPLE).write_text("# edited\n", encoding="utf-8")

    # Reload
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert not (folder / BATTERY).exists()

    # Restart: a fresh setup of the stored entry
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED
    assert not (folder / BATTERY).exists()
    assert (folder / APPLE).read_text(encoding="utf-8") == "# edited\n"


async def test_existing_file_is_never_overwritten(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    blueprints_in_tmp: Path,
) -> None:
    folder = blueprints_in_tmp / "automation" / DOMAIN
    folder.mkdir(parents=True)
    (folder / BATTERY).write_text("# own version\n", encoding="utf-8")
    await setup_integration(hass, mock_api, config_entry)
    assert (folder / BATTERY).read_text(encoding="utf-8") == "# own version\n"
    assert _installed(blueprints_in_tmp) == set(BUNDLED)


async def test_failed_copy_never_breaks_setup_and_is_retried(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    blueprints_in_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    working = blueprint_install._install  # noqa: SLF001

    def _fail(_root: Path) -> list[str]:
        raise OSError("read-only file system")

    monkeypatch.setattr(blueprint_install, "_install", _fail)
    await setup_integration(hass, mock_api, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert "Could not install" in caplog.text
    assert CONF_BLUEPRINTS_INSTALLED not in config_entry.data

    monkeypatch.setattr(blueprint_install, "_install", working)
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert _installed(blueprints_in_tmp) == set(BUNDLED)
    assert config_entry.data[CONF_BLUEPRINTS_INSTALLED] is True


async def test_new_entry_copies_missing_blueprints(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    blueprints_in_tmp: Path,
) -> None:
    """Only one Halumar entry exists at a time; a new entry after the old one
    was deleted copies the missing blueprints again."""
    await setup_integration(hass, mock_api, config_entry)
    folder = blueprints_in_tmp / "automation" / DOMAIN
    (folder / BATTERY).unlink()
    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()

    new_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=config_entry.unique_id,
        data={
            k: v for k, v in config_entry.data.items() if k != CONF_BLUEPRINTS_INSTALLED
        },
    )
    new_entry.add_to_hass(hass)
    await setup_integration(hass, mock_api, new_entry)
    assert (folder / BATTERY).exists()
