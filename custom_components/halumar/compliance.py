"""Token-less compliance view for Halumar client apps.

GET /api/halumar/compliance/<client_id> serves the hub's opaque,
HMAC-signed per-client compliance blob to client apps that can no longer
authenticate to HA (revoked token, pending deletion). The view is
unauthenticated by design: a device whose HA token is dead has no other
way to learn WHY, and Nabu Casa forwards unauthenticated views exactly
like webhooks.

Security model:
- The integration holds NO key material. Signing happens in the hub
  (which holds the client's secret hash); the app verifies the signature
  with its pairing secret. This view only relays the cached blob verbatim.
- No existence oracle: an unknown client id, a compliant client, a client
  without a blob, and a disabled endpoint toggle all produce the IDENTICAL
  neutral body (HTTP 200). Only the two signed restricting statuses
  (need_token_update, deleted) are ever distinguishable, and those are
  useless without the per-client pairing secret.
- Per-IP rate limit mirroring the privileged-service cooldown in
  services.py (_enforce_rate_limit), so the endpoint cannot be hammered.
"""

from __future__ import annotations

import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.util.hass_dict import HassKey

from .const import COMPLIANCE_VIEW_NAME, COMPLIANCE_VIEW_URL, DOMAIN

if TYPE_CHECKING:
    from .coordinator import HubCoordinator

# hass.data flag guarding double registration: HA has no view
# deregistration, so the view is registered once per HA instance and
# survives entry reloads (it resolves the coordinator per request).
DATA_COMPLIANCE_VIEW_REGISTERED = f"{DOMAIN}_compliance_view_registered"

# Statuses the hub signs. Anything else (compliant, unknown, or a
# malformed/unsigned blob) collapses to the neutral body.
SIGNED_STATUSES = frozenset({"need_token_update", "deleted"})

# ── Per-IP rate limiting ──────────────────────────────────────────────────
# Mirrors _enforce_rate_limit in services.py: a minimum interval between
# two successive requests from the same remote address. 1 second is far
# above the app's dormant polling cadence but still allows two devices
# behind the same NAT to poll without permanently starving each other.
RATE_LIMIT_SECONDS = 1.0

# Upper bound on tracked addresses, so requests from many addresses cannot
# grow the table without bound; expired entries are pruned at the cap.
_RATE_LIMIT_MAX_TRACKED = 4096

# Remote address -> time of its last allowed request, per HA instance.
DATA_LAST_REQUESTS: HassKey[dict[str, float]] = HassKey(f"{DOMAIN}_compliance_requests")


def last_requests(hass: HomeAssistant) -> dict[str, float]:
    """The per-address request times of this HA instance."""
    return hass.data.setdefault(DATA_LAST_REQUESTS, {})


def _rate_limited(hass: HomeAssistant, remote: str) -> bool:
    """Return True when this remote must wait before the next request.

    Like _enforce_rate_limit, the timestamp is only recorded for allowed
    requests: a rejected burst does not extend its own cooldown window.
    """
    seen_at = last_requests(hass)
    now = time.monotonic()
    if len(seen_at) >= _RATE_LIMIT_MAX_TRACKED:
        cutoff = now - RATE_LIMIT_SECONDS
        for ip, seen in list(seen_at.items()):
            if seen < cutoff:
                del seen_at[ip]
    last = seen_at.get(remote)
    if last is not None and (now - last) < RATE_LIMIT_SECONDS:
        return True
    seen_at[remote] = now
    return False


def _neutral_body() -> dict[str, Any]:
    """Return the neutral response body.

    Served identically for unknown ids, compliant clients, missing blobs,
    and a disabled endpoint toggle, so the response never reveals whether
    a client id exists. ts is the serve time (unsigned statuses carry no
    signature, so the timestamp is informational only).
    """
    return {"v": 1, "status": "unknown", "ts": int(time.time())}


def _async_get_coordinator(hass: HomeAssistant) -> HubCoordinator | None:
    """Return the coordinator of the loaded entry, or None before/after setup."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            coordinator: HubCoordinator = entry.runtime_data
            return coordinator
    return None


class HubComplianceView(HomeAssistantView):
    """Unauthenticated relay for the hub's signed compliance blobs."""

    url = COMPLIANCE_VIEW_URL
    name = COMPLIANCE_VIEW_NAME
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Bind the view to this HA instance."""
        self._hass = hass

    async def get(self, request: web.Request, client_id: str) -> web.Response:
        """Serve the client's compliance blob, or the neutral body."""
        if _rate_limited(self._hass, request.remote or ""):
            return self.json(
                {"error": "rate_limited"},
                status_code=HTTPStatus.TOO_MANY_REQUESTS,
            )

        coordinator = _async_get_coordinator(self._hass)
        if coordinator is None or not coordinator.compliance_endpoint_enabled:
            return self.json(_neutral_body())

        blob = coordinator.compliance_by_id.get(client_id)
        if not isinstance(blob, dict):
            return self.json(_neutral_body())

        status = blob.get("status")
        ts = blob.get("ts")
        sig = blob.get("sig")
        if status not in SIGNED_STATUSES or ts is None or not sig:
            # Unsigned statuses (compliant/unknown) and malformed blobs are
            # indistinguishable from an unknown client by design.
            return self.json(_neutral_body())

        # Signed restricting status: relay the hub blob VERBATIM. ts and
        # sig must pass through untouched because the app recomputes the
        # HMAC over "v1|client_id|status|ts" and compares byte-for-byte.
        return self.json({"v": 1, "status": status, "ts": ts, "sig": sig})


@callback
def async_register_compliance_view(hass: HomeAssistant) -> None:
    """Register the compliance view exactly once per HA instance."""
    if hass.data.get(DATA_COMPLIANCE_VIEW_REGISTERED):
        return
    hass.http.register_view(HubComplianceView(hass))
    hass.data[DATA_COMPLIANCE_VIEW_REGISTERED] = True
