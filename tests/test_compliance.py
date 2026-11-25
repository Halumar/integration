"""Tests for the token-less compliance view (GET /api/halumar/compliance).

The view relays the hub's HMAC-signed compliance blobs verbatim and
collapses everything else (unknown id, compliant client, missing blob,
disabled toggle, malformed blob) to one identical neutral body, so the
endpoint is not an existence oracle. The known-answer vector below is the
shared crypto-contract vector the hub and the iOS app must reproduce;
the integration never verifies it, it only relays.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

import pytest
from aioresponses import aioresponses

from custom_components.halumar import compliance
from custom_components.halumar.brand import BRAND_NAME
from custom_components.halumar.compliance import HubComplianceView
from custom_components.halumar.const import (
    CONF_API_TOKEN,
    CONF_USE_SSL,
    DOMAIN,
    ENTRY_UNIQUE_ID,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from ._ha import MockConfigEntry
from .common import TEST_TOKEN, make_status_payload

# The hub is mocked on a non-loopback host here so aioresponses can pass
# loopback traffic through to the aiohttp test server hosting the view.
ADDON_HOST = "hub-addon.invalid"
ADDON_PORT = 8099
STATUS_URL = f"http://{ADDON_HOST}:{ADDON_PORT}/api/integration/status"

# ── Shared crypto-contract known-answer vector ──────────────────────────
# digest32 = 00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff
VECTOR_CLIENT_ID = "11111111-2222-3333-4444-555555555555"
VECTOR_TS = 1783130000
VECTOR_SIG_NEED_TOKEN_UPDATE = (
    "043c94b1ed4b90d21468c396891df7b88566067a47207436e9236d271abcadaf"
)
VECTOR_SIG_DELETED = "6ffc6ab04928d81add5fa8d0d39581d560163b81fbfeed6fd8adad61d97ed685"

COMPLIANT_CLIENT_ID = "11111111-1111-1111-1111-111111111111"
UNKNOWN_CLIENT_ID = "99999999-9999-9999-9999-999999999999"
TOMBSTONE_CLIENT_ID = "44444444-4444-4444-4444-444444444444"


@pytest.fixture
def mock_api() -> Generator[aioresponses, None, None]:
    """Intercept aiohttp requests, passing loopback through.

    Overrides the conftest fixture: the view lives on the aiohttp test
    server (loopback), which must NOT be mocked, while the hub host
    (hub-addon.invalid) stays fully mocked.
    """
    with aioresponses(passthrough=["http://127.0.0.1", "http://localhost"]) as mocked:
        yield mocked


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return a Halumar config entry pointing at the mocked hub host."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ENTRY_UNIQUE_ID,
        title=f"{BRAND_NAME} ({ADDON_HOST}:{ADDON_PORT})",
        data={
            CONF_HOST: ADDON_HOST,
            CONF_PORT: ADDON_PORT,
            CONF_API_TOKEN: TEST_TOKEN,
            CONF_USE_SSL: False,
        },
    )
    entry.add_to_hass(hass)
    return entry


def _clear_rate_limit(hass: HomeAssistant) -> None:
    """Forget the test client's IP so back-to-back requests are allowed."""
    compliance.last_requests(hass).clear()


def make_compliance_payload(**overrides: Any) -> dict[str, Any]:
    """Status payload with compliance blobs on the clients.

    Client 0 (Wall Tablet) is compliant (unsigned blob); a third client
    carries the signed known-answer need_token_update blob; a fourth
    carries a signed deleted blob.
    """
    payload = make_status_payload()
    payload["clients"][0]["compliance"] = {
        "status": "compliant",
        "ts": VECTOR_TS,
    }
    payload["clients"].append(
        {
            "id": VECTOR_CLIENT_ID,
            "name": "Revoked Phone",
            "slug": "revoked-phone",
            "is_connected": False,
            "is_guest": False,
            "connection_type": "disconnected",
            "tags": [],
            "notification_ids": [],
            "notification_count": 0,
            "compliance": {
                "status": "need_token_update",
                "ts": VECTOR_TS,
                "sig": VECTOR_SIG_NEED_TOKEN_UPDATE,
            },
        }
    )
    payload["clients"].append(
        {
            "id": "33333333-3333-3333-3333-333333333333",
            "name": "Deleted Phone",
            "slug": "deleted-phone",
            "is_connected": False,
            "is_guest": False,
            "connection_type": "disconnected",
            "tags": [],
            "notification_ids": [],
            "notification_count": 0,
            "pending_deletion": True,
            "compliance": {
                "status": "deleted",
                "ts": VECTOR_TS,
                "sig": VECTOR_SIG_DELETED,
            },
        }
    )
    payload["compliance_endpoint_enabled"] = True
    payload.update(overrides)
    return payload


async def setup_compliance(
    hass: HomeAssistant,
    mock_api: aioresponses,
    entry: MockConfigEntry,
    payload: dict[str, Any],
) -> None:
    """Set up the config entry against the mocked hub."""
    mock_api.get(STATUS_URL, status=200, payload=payload, repeat=True)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def test_view_is_unauthenticated() -> None:
    """The view must opt out of HA auth: dead-token clients are the callers."""
    assert HubComplianceView.requires_auth is False
    assert HubComplianceView.url == "/api/halumar/compliance/{client_id}"


async def test_unknown_client_returns_neutral_body(
    hass: HomeAssistant,
    mock_api: aioresponses,
    config_entry: MockConfigEntry,
    hass_client_no_auth,
) -> None:
    """An unknown client id gets HTTP 200 with the neutral body, not a 404."""
    await setup_compliance(hass, mock_api, config_entry, make_compliance_payload())
    client = await hass_client_no_auth()

    resp = await client.get(f"/api/halumar/compliance/{UNKNOWN_CLIENT_ID}")
    assert resp.status == 200
    body = await resp.json()
    assert body["v"] == 1
    assert body["status"] == "unknown"
    assert isinstance(body["ts"], int)
    assert "sig" not in body
    assert set(body) == {"v", "status", "ts"}


async def test_compliant_client_indistinguishable_from_unknown(
    hass: HomeAssistant,
    mock_api: aioresponses,
    config_entry: MockConfigEntry,
    hass_client_no_auth,
) -> None:
    """A compliant client and an unknown id produce the identical body."""
    await setup_compliance(hass, mock_api, config_entry, make_compliance_payload())
    client = await hass_client_no_auth()

    resp_compliant = await client.get(f"/api/halumar/compliance/{COMPLIANT_CLIENT_ID}")
    _clear_rate_limit(hass)
    resp_unknown = await client.get(f"/api/halumar/compliance/{UNKNOWN_CLIENT_ID}")
    assert resp_compliant.status == 200
    assert resp_unknown.status == 200

    body_compliant = await resp_compliant.json()
    body_unknown = await resp_unknown.json()
    # ts is the serve time in both bodies; normalize it (within a small
    # tolerance) and the two bodies must be byte-identically shaped.
    assert abs(body_compliant["ts"] - body_unknown["ts"]) <= 5
    body_compliant["ts"] = body_unknown["ts"] = 0
    assert body_compliant == body_unknown == {"v": 1, "status": "unknown", "ts": 0}


async def test_signed_blob_relayed_verbatim(
    hass: HomeAssistant,
    mock_api: aioresponses,
    config_entry: MockConfigEntry,
    hass_client_no_auth,
) -> None:
    """Signed statuses pass through untouched: sig and ts are byte-exact."""
    await setup_compliance(hass, mock_api, config_entry, make_compliance_payload())
    client = await hass_client_no_auth()

    resp = await client.get(f"/api/halumar/compliance/{VECTOR_CLIENT_ID}")
    assert resp.status == 200
    assert await resp.json() == {
        "v": 1,
        "status": "need_token_update",
        "ts": VECTOR_TS,
        "sig": VECTOR_SIG_NEED_TOKEN_UPDATE,
    }

    _clear_rate_limit(hass)
    resp = await client.get(
        "/api/halumar/compliance/33333333-3333-3333-3333-333333333333"
    )
    assert resp.status == 200
    assert await resp.json() == {
        "v": 1,
        "status": "deleted",
        "ts": VECTOR_TS,
        "sig": VECTOR_SIG_DELETED,
    }


async def test_tombstone_only_client_gets_signed_deleted(
    hass: HomeAssistant,
    mock_api: aioresponses,
    config_entry: MockConfigEntry,
    hass_client_no_auth,
) -> None:
    """A hidden/dismissed pending-deletion client absent from `clients` but
    present in the top-level `tombstones` array still gets its signed
    `deleted` blob relayed verbatim, not the neutral body."""
    payload = make_compliance_payload()
    assert all(c["id"] != TOMBSTONE_CLIENT_ID for c in payload["clients"])
    payload["tombstones"] = [
        {
            "client_id": TOMBSTONE_CLIENT_ID,
            "compliance": {
                "status": "deleted",
                "ts": VECTOR_TS,
                "sig": VECTOR_SIG_DELETED,
            },
        }
    ]
    await setup_compliance(hass, mock_api, config_entry, payload)
    client = await hass_client_no_auth()

    resp = await client.get(f"/api/halumar/compliance/{TOMBSTONE_CLIENT_ID}")
    assert resp.status == 200
    assert await resp.json() == {
        "v": 1,
        "status": "deleted",
        "ts": VECTOR_TS,
        "sig": VECTOR_SIG_DELETED,
    }


async def test_restricting_status_without_sig_is_neutral(
    hass: HomeAssistant,
    mock_api: aioresponses,
    config_entry: MockConfigEntry,
    hass_client_no_auth,
) -> None:
    """A restricting status missing its signature collapses to neutral."""
    payload = make_compliance_payload()
    payload["clients"][2]["compliance"] = {
        "status": "need_token_update",
        "ts": VECTOR_TS,
    }
    await setup_compliance(hass, mock_api, config_entry, payload)
    client = await hass_client_no_auth()

    resp = await client.get(f"/api/halumar/compliance/{VECTOR_CLIENT_ID}")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "unknown"
    assert "sig" not in body


async def test_toggle_disabled_serves_neutral_for_all(
    hass: HomeAssistant,
    mock_api: aioresponses,
    config_entry: MockConfigEntry,
    hass_client_no_auth,
) -> None:
    """With the hub toggle off, even signed clients get the neutral body."""
    payload = make_compliance_payload(compliance_endpoint_enabled=False)
    await setup_compliance(hass, mock_api, config_entry, payload)
    client = await hass_client_no_auth()

    resp = await client.get(f"/api/halumar/compliance/{VECTOR_CLIENT_ID}")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "unknown"
    assert "sig" not in body


async def test_rate_limit_trips(
    hass: HomeAssistant,
    mock_api: aioresponses,
    config_entry: MockConfigEntry,
    hass_client_no_auth,
) -> None:
    """A second request from the same IP inside the cooldown gets a 429."""
    await setup_compliance(hass, mock_api, config_entry, make_compliance_payload())
    client = await hass_client_no_auth()

    resp = await client.get(f"/api/halumar/compliance/{VECTOR_CLIENT_ID}")
    assert resp.status == 200

    resp = await client.get(f"/api/halumar/compliance/{VECTOR_CLIENT_ID}")
    assert resp.status == 429

    # Outside the cooldown (simulated by forgetting the IP) it serves again.
    _clear_rate_limit(hass)
    resp = await client.get(f"/api/halumar/compliance/{VECTOR_CLIENT_ID}")
    assert resp.status == 200
