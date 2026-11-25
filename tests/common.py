"""Shared constants and helpers for the Halumar test suite.

The status payload built here deliberately contains fields the hub must
never send by contract (a pairing PIN, notification content, an HA token,
GPS coordinates on guests), so the tests prove the integration never
surfaces them even if a faulty hub sent them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aioresponses import aioresponses

from homeassistant.core import HomeAssistant

from ._ha import MockConfigEntry

TEST_HOST = "127.0.0.1"
TEST_PORT = 8099
TEST_TOKEN = "test-integration-token"
BASE_URL = f"http://{TEST_HOST}:{TEST_PORT}"
STATUS_URL = f"{BASE_URL}/api/integration/status"
CLIENT_TOKEN_URL = f"{BASE_URL}/api/integration/client-token"

# Read the version from the manifest so a version bump cannot silently
# break the X-Hub-Integration-Version header contract tests.
MANIFEST_VERSION: str = json.loads(
    (
        Path(__file__).parent.parent / "custom_components" / "halumar" / "manifest.json"
    ).read_text()
)["version"]

CLIENT_ID = "11111111-1111-1111-1111-111111111111"
GUEST_ID = "22222222-2222-2222-2222-222222222222"
GUEST_EXPIRES_AT = "2026-07-04T10:00:00+00:00"

# Secrets planted in the adversarial payload fields; they must never appear
# in any entity state, attribute, or diagnostics export.
SECRET_PIN = "424242"
SECRET_NOTIFICATION_TITLE = "SECRET-NOTIFICATION-TITLE"
SECRET_NOTIFICATION_BODY = "SECRET-NOTIFICATION-BODY"
SECRET_HA_TOKEN = "SECRET-HA-TOKEN"
ALL_SECRETS = (
    SECRET_PIN,
    SECRET_NOTIFICATION_TITLE,
    SECRET_NOTIFICATION_BODY,
    SECRET_HA_TOKEN,
)


# hub.instance_id of the hub in the status payloads (contract $defs/hub_instance_id).
HUB_INSTANCE_ID = "6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b"


def make_status_payload() -> dict[str, Any]:
    """Return a full /api/integration/status snapshot.

    Mirrors the hub contract (hub, areas, clients, guests, pairing_*)
    plus adversarial extras that must never leak (see module docstring).
    """
    return {
        "hub": {
            "version": "2026.12.0",
            "instance_id": HUB_INSTANCE_ID,
            "connected_clients": 1,
        },
        "area_count": 2,
        "client_count": 2,
        "guest_count": 1,
        "areas": [
            {
                "id": "area-kitchen",
                "name": "Kitchen",
                "slug": "kitchen",
                "is_active": True,
                "entity_count": 5,
                "grid_cols": 2,
                "tags": ["downstairs"],
                "color": "blue",
                "description": None,
                "control_mode": None,
                "icon": None,
            },
            {
                "id": "area-office",
                "name": "Office",
                "slug": "office",
                "is_active": False,
                "entity_count": 3,
                "grid_cols": 1,
                "tags": [],
                "color": None,
                "description": None,
                "control_mode": "basic",
                "icon": None,
            },
        ],
        "clients": [
            {
                "id": CLIENT_ID,
                "name": "Wall Tablet",
                "slug": "wall-tablet",
                "is_connected": True,
                "is_guest": False,
                "device_name": "iPad Pro",
                "app_version": "1.2.3",
                "current_area_id": "area-kitchen",
                "current_area_name": "Kitchen",
                "connection_type": "lan",
                "battery_level": 81,
                "last_seen": "2026-07-03T09:59:00+00:00",
                "tags": ["tablet"],
                "notification_ids": ["n-1", "n-2"],
                "notification_count": 2,
                "has_ha_token": True,
                # Adversarial extras (never sent by contract):
                "notifications": [
                    {
                        "id": "n-1",
                        "title": SECRET_NOTIFICATION_TITLE,
                        "message": SECRET_NOTIFICATION_BODY,
                    }
                ],
                "ha_token": SECRET_HA_TOKEN,
            },
            {
                "id": GUEST_ID,
                "name": "Guest Phone",
                "slug": "guest-phone",
                "is_connected": False,
                "is_guest": True,
                "expires_at": GUEST_EXPIRES_AT,
                "device_name": "Pixel 9",
                "app_version": "1.2.3",
                "current_area_id": None,
                "current_area_name": None,
                "connection_type": "disconnected",
                "battery_level": None,
                "last_seen": "2026-07-03T08:00:00+00:00",
                "tags": ["guest"],
                "notification_ids": [],
                "notification_count": 0,
            },
        ],
        "guests": [
            {
                "client_id": GUEST_ID,
                "is_connected": False,
                "expires_at": GUEST_EXPIRES_AT,
                # Adversarial extras: the guests sensor must project these away.
                "name": "Guest Phone",
                "device_name": "Pixel 9",
                "tags": ["guest"],
            }
        ],
        "provisioning": [],
        "ha_user_holds": [],
        "pairing_active": False,
        "pairing_remaining": 0,
        "pairing_is_guest": False,
        # Adversarial extra (never sent by contract):
        "pairing_pin": SECRET_PIN,
    }


async def setup_integration(
    hass: HomeAssistant,
    mock_api: aioresponses,
    entry: MockConfigEntry,
    payload: dict[str, Any] | None = None,
    *,
    repeat: bool = True,
) -> None:
    """Set up a config entry against a mocked status endpoint.

    With repeat=False the mocked 200 is consumed by the first refresh only,
    so tests can register a different response for the next poll.
    """
    mock_api.get(
        STATUS_URL,
        status=200,
        payload=payload if payload is not None else make_status_payload(),
        repeat=repeat,
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
