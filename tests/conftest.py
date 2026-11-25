"""Fixtures for the Halumar integration test suite."""

from __future__ import annotations

from collections.abc import Generator

from pathlib import Path

import pytest
from aioresponses import aioresponses

from custom_components.halumar.brand import BRAND_NAME
from custom_components.halumar.const import (
    CONF_API_TOKEN,
    CONF_USE_SSL,
    DOMAIN,
    ENTRY_UNIQUE_ID,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from ._ha import MockConfigEntry
from .common import TEST_HOST, TEST_PORT, TEST_TOKEN


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom integrations from custom_components/ in every test."""


@pytest.fixture(autouse=True)
def blueprints_in_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[Path, None, None]:
    """Install the bundled blueprints into a temp dir, never into the repo."""
    from custom_components.halumar import blueprint_install

    target = tmp_path / "blueprints"
    real = blueprint_install._install  # noqa: SLF001

    monkeypatch.setattr(blueprint_install, "_install", lambda _root: real(target))
    yield target


@pytest.fixture
def mock_api() -> Generator[aioresponses, None, None]:
    """Intercept aiohttp requests.

    aioresponses patches ClientSession._request, so it covers both HA's
    shared client session (config flow validation) and the coordinator's
    own session (polling + service calls).
    """
    with aioresponses() as mocked:
        yield mocked


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return a Halumar config entry registered with hass."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ENTRY_UNIQUE_ID,
        title=f"{BRAND_NAME} ({TEST_HOST}:{TEST_PORT})",
        data={
            CONF_HOST: TEST_HOST,
            CONF_PORT: TEST_PORT,
            CONF_API_TOKEN: TEST_TOKEN,
            CONF_USE_SSL: False,
        },
    )
    entry.add_to_hass(hass)
    return entry
