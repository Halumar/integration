"""Diagnostics support for Halumar integration.

Provides a diagnostic dump for debugging and support purposes. Secrets
(the integration API token, HA tokens, the pairing PIN) and location data
never appear: the entry data is redacted, and the snapshot goes through an
allowlist of known-safe fields.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_API_TOKEN
from .coordinator import HubConfigEntry
from .enforcement import DATA_ENFORCEMENT

TO_REDACT = {CONF_API_TOKEN}

# Allowlist approach: only known-safe client fields (security hardening).
# The status payload itself is content-free by contract: notifications
# appear as ids+counts only, so no notification title or message can ever
# end up in a diagnostics export. GPS coordinates are deliberately NOT
# allowlisted.
SAFE_CLIENT_FIELDS = frozenset(
    {
        "id",
        "name",
        "slug",
        "is_connected",
        "is_guest",
        "is_active",
        "device_name",
        "app_version",
        "current_area_id",
        "current_area_name",
        "connection_type",
        "battery_level",
        "last_seen",
        "tags",
        "notification_ids",
        "notification_count",
        "expires_at",
        "remote_allowed",
        "pending_deletion",
        "pending_deletion_at",
    }
)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: HubConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    raw_data = coordinator.data or {}

    clients = []
    for client in raw_data.get("clients", []):
        safe_client = {k: v for k, v in client.items() if k in SAFE_CLIENT_FIELDS}
        safe_client["has_ha_token"] = client.get("has_ha_token", False)
        clients.append(safe_client)

    # Allowlist the hub block too (consistent with clients/areas) so no future
    # sensitive field can leak into a downloadable diagnostics export.
    hub = raw_data.get("hub") or {}
    safe_hub = {
        "version": hub.get("version"),
        "connected_clients": hub.get("connected_clients"),
        "ws_push": hub.get("ws_push"),
    }

    device_users = coordinator.device_users
    return {
        "config": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "coordinator_data": {
            "hub": safe_hub,
            "area_count": raw_data.get("area_count", 0),
            "client_count": raw_data.get("client_count", 0),
            "pairing_active": raw_data.get("pairing_active", False),
            "pairing_remaining": raw_data.get("pairing_remaining", 0),
            "allow_pairing_via_integration": raw_data.get(
                "allow_pairing_via_integration"
            ),
            "areas": [
                {
                    "id": a.get("id"),
                    "name": a.get("name"),
                    "slug": a.get("slug"),
                    "is_active": a.get("is_active"),
                    "entity_count": a.get("entity_count"),
                    "grid_cols": a.get("grid_cols"),
                    "control_mode": a.get("control_mode"),
                    "tags": a.get("tags", []),
                }
                for a in raw_data.get("areas", [])
            ],
            "clients": clients,
        },
        # Ids only: which devices have an HA user (never a token).
        "device_users": sorted(device_users.records) if device_users else [],
        "push_connected": coordinator.push_task is not None
        and not coordinator.push_task.done(),
        "update_interval": coordinator.update_interval.total_seconds()
        if coordinator.update_interval
        else None,
        "last_update_success": coordinator.last_update_success,
        # Enforcement in HA: hashes, control paths, counts; no rules
        # content, no names.
        "enforcement": hass.data[DATA_ENFORCEMENT].diagnostics()
        if DATA_ENFORCEMENT in hass.data
        else None,
    }
