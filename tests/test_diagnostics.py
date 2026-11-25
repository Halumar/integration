"""Tests for the diagnostics export: redaction and field allowlists."""

from __future__ import annotations

import json

from aioresponses import aioresponses

from custom_components.halumar.diagnostics import (
    async_get_config_entry_diagnostics,
)
from homeassistant.core import HomeAssistant

from ._ha import MockConfigEntry
from .common import ALL_SECRETS, CLIENT_ID, TEST_TOKEN, setup_integration


async def test_diagnostics_redacts_and_allowlists(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """The export redacts the token and drops all non-allowlisted fields."""
    await setup_integration(hass, mock_api, config_entry)

    diag = await async_get_config_entry_diagnostics(hass, config_entry)

    assert diag["config"]["api_token"] == "**REDACTED**"

    blob = json.dumps(diag, default=str)
    for needle in ALL_SECRETS:
        assert needle not in blob, f"{needle!r} leaked into diagnostics"
    assert "pairing_pin" not in blob

    client = next(
        c for c in diag["coordinator_data"]["clients"] if c["id"] == CLIENT_ID
    )
    # GPS and raw notification content are deliberately not allowlisted
    assert "latitude" not in client
    assert "longitude" not in client
    assert "notifications" not in client
    assert "ha_token" not in client
    assert client["has_ha_token"] is True
    assert client["notification_ids"] == ["n-1", "n-2"]
    assert client["notification_count"] == 2

    # The area's control mode as the hub sends it.
    assert [a["control_mode"] for a in diag["coordinator_data"]["areas"]] == [
        None,
        "basic",
    ]

    hub = diag["coordinator_data"]["hub"]
    assert hub == {"version": "2026.12.0", "connected_clients": 1, "ws_push": None}
    assert TEST_TOKEN not in blob
    assert diag["options"] == {}
    assert diag["device_users"] == []
    assert diag["push_connected"] is False
