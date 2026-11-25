"""Camera platform for Halumar integration.

Provides a camera entity that shows whether a pairing window is open. The
image is fetched from the hub's GET /api/integration/pairing-qr endpoint.
A camera image is readable by every HA user and every device token (Home
Assistant gives the entity no way to know who asks). The image therefore
carries the pairing QR code (with the PIN) only when the admin switched it on
with the hub option pairing_qr_camera, reported in the status snapshot:
``guests`` only during a guest pairing window, ``all`` during every window.
The hub marks such an image ``X-Pairing-Pin: included`` with
``X-Pairing-Kind``; the camera shows it only when the reported mode allows
that kind and never keeps it in its cache. Otherwise the QR code is shown
only in the Halumar panel (admins).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import aiohttp

from aiohttp import web

from homeassistant.components.camera import Camera, async_get_still_stream
from homeassistant.components.http import KEY_HASS_REFRESH_TOKEN_ID
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback

from .const import (
    QR_ACCEPT_HEADER,
    QR_ACCEPT_VALUE,
    QR_MODE_ALL,
    QR_MODE_GUESTS,
    QR_MODE_OFF,
)
from .coordinator import HubConfigEntry, HubCoordinator
from .entity import HubEntity

if TYPE_CHECKING:
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

_LOGGER = logging.getLogger(__name__)

# The image is fetched on demand; one camera, no limit needed.
PARALLEL_UPDATES = 0

# X-Pairing-Kind values a mode allows to show (X-Pairing-Pin: included).
_KINDS_SHOWN = {
    QR_MODE_OFF: frozenset(),
    QR_MODE_GUESTS: frozenset({"guest"}),
    QR_MODE_ALL: frozenset({"guest", "device"}),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HubConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Halumar camera entity."""
    async_add_entities([HubPairingQRCamera(entry.runtime_data, entry)])


class HubPairingQRCamera(HubEntity, Camera):
    """Camera entity that shows the pairing status image."""

    _attr_translation_key = "pairing_qr_code"
    _attr_entity_category = EntityCategory.CONFIG
    # Short frame interval so HA frontend polls the image frequently
    # while pairing is active (default is 10s which is too slow)
    _attr_frame_interval = 2
    # Keep the transient pairing-status attributes out of the recorder/history
    # DB. They are live-only signals about an in-progress pairing session and
    # have no analytic value once pairing ends. (The pairing PIN is never
    # exposed as an attribute at all -- see extra_state_attributes.)
    _unrecorded_attributes = frozenset(
        {"pairing_active", "is_guest_pairing", "pairing_remaining"}
    )

    def __init__(self, coordinator: HubCoordinator, entry: HubConfigEntry) -> None:
        """Initialize the camera."""
        HubEntity.__init__(
            self, coordinator, entry, "camera", "pairing_qr", "Pairing QR Code"
        )
        Camera.__init__(self)
        # Last image WITHOUT a PIN (shown again on errors); a QR code with the
        # PIN is never kept, so it cannot outlive its pairing window.
        self._cached_image: bytes | None = None
        self._prev_pairing_active: bool = False
        self._prev_qr_mode: str = QR_MODE_OFF

    @property
    def is_on(self) -> bool:
        """Always on: without a pairing window it shows the standby image.

        HA's camera proxy answers 503 for a camera that is off, so tying
        ``is_on`` to the pairing window made every image request outside a
        pairing fail. The pairing state is ``is_streaming``.
        """
        return True

    @property
    def is_streaming(self) -> bool:
        """Return True while a pairing window is open (state 'streaming')."""
        if self.coordinator.data:
            return bool(self.coordinator.data.get("pairing_active", False))
        return False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return non-sensitive pairing status as extra attributes.

        The live pairing PIN is deliberately NOT exposed here: attributes are
        readable by non-admin users and are persisted to the recorder/history
        DB. ``qr_mode`` is the hub option pairing_qr_camera (off, guests,
        all): when the image may be the QR code.
        """
        attrs: dict[str, Any] = {
            "pairing_active": False,
            "is_guest_pairing": False,
            "qr_mode": self._qr_mode(),
        }
        if self.coordinator.data:
            attrs["pairing_active"] = self.coordinator.data.get("pairing_active", False)
            attrs["pairing_remaining"] = self.coordinator.data.get(
                "pairing_remaining", 0
            )
            attrs["is_guest_pairing"] = self.coordinator.data.get(
                "pairing_is_guest", False
            )
        return attrs

    def _qr_mode(self) -> str:
        """The hub's pairing_qr_camera mode (off without a snapshot)."""
        data = self.coordinator.data
        mode = data.get("pairing_qr_camera") if data else None
        return mode if mode in _KINDS_SHOWN else QR_MODE_OFF

    @callback
    def _handle_coordinator_update(self) -> None:
        """Invalidate cached image when pairing state or QR mode changes."""
        pairing_active = False
        if self.coordinator.data:
            pairing_active = bool(self.coordinator.data.get("pairing_active", False))
        qr_mode = self._qr_mode()

        if pairing_active != self._prev_pairing_active or qr_mode != self._prev_qr_mode:
            # Pairing state or mode changed -- clear cache so next image
            # request fetches a fresh image
            self._cached_image = None
            self._prev_pairing_active = pairing_active
            self._prev_qr_mode = qr_mode

        super()._handle_coordinator_update()

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the pairing status image as PNG (the last one on errors)."""
        image, _window = await self._fetch_image()
        return image

    async def _fetch_image(self) -> tuple[bytes | None, tuple[str, str] | None]:
        """Fetch the image; the second value is (kind, window id) of a QR."""
        try:
            status, headers, body = await self.coordinator.api_get_image(
                "/api/integration/pairing-qr", {QR_ACCEPT_HEADER: QR_ACCEPT_VALUE}
            )
        # Not an action: the frontend polls the image every 2 s, and the last
        # image is a better answer than an error while the Hub restarts.
        except (
            aiohttp.ClientError,
            TimeoutError,
        ) as err:
            _LOGGER.warning("Failed to fetch the pairing image: %s", err)
            return self._cached_image, None
        if status != 200:
            _LOGGER.warning("Pairing image endpoint returned HTTP %s", status)
            return self._cached_image, None
        # Every HA user and client token can read this camera. A QR code with
        # the PIN is shown only when the hub reports a mode that allows its
        # kind (the admin's opt-in); it is never cached.
        marker = headers.get("X-Pairing-Pin")
        if marker == "included":
            kind = headers.get("X-Pairing-Kind")
            self._cached_image = None
            if (
                headers.get("X-Pairing-Active") == "true"
                and kind in _KINDS_SHOWN[self._qr_mode()]
            ):
                return body, (str(kind), headers.get("X-Pairing-Window", ""))
            _LOGGER.warning(
                "The hub sent a pairing QR code that the pairing_qr_camera "
                "option does not allow; not shown"
            )
            return None, None
        # Anything not marked PIN-free while a pairing window is open is
        # refused.
        if headers.get("X-Pairing-Active") == "true" and marker != "omitted":
            _LOGGER.warning(
                "The hub sent a pairing image that may contain the pairing code; "
                "not shown (update the hub)"
            )
            self._cached_image = None
            return None, None
        self._cached_image = body
        return body, None

    # ── MJPEG streams ────────────────────────────────────────────────────
    # Home Assistant checks access once when /api/camera_proxy_stream is
    # opened and then fetches an image every frame_interval for as long as
    # the connection lasts. A stream therefore ends when the user or token
    # that opened it is no longer valid, and it never carries a QR code of
    # a pairing window it was not opened in: it ends when a QR appears, its
    # kind changes, a new window starts or the window ends.

    def _stream_authorised(self, request: web.Request) -> bool:
        """Whether the credentials that opened a stream are still valid."""
        refresh_token_id = request.get(KEY_HASS_REFRESH_TOKEN_ID)
        if refresh_token_id is not None:
            token = self.hass.auth.async_get_refresh_token(refresh_token_id)
            return token is not None and token.user.is_active
        return request.query.get("token") in self.access_tokens

    async def handle_async_mjpeg_stream(
        self, request: web.Request
    ) -> web.StreamResponse | None:
        """Serve the MJPEG stream of the image (guarded, see above)."""
        return await self.handle_async_still_stream(request, self.frame_interval)

    async def handle_async_still_stream(
        self, request: web.Request, interval: float
    ) -> web.StreamResponse:
        """Stream images while the opener stays authorised and the window holds."""
        opened: dict[str, Any] = {}

        async def _frame() -> bytes | None:
            if not self._stream_authorised(request):
                return None
            image, window = await self._fetch_image()
            if not image:
                return None
            if "window" not in opened:
                opened["window"] = window
                return image
            if window != opened["window"]:
                # A QR appeared, or the window ended, changed kind or a new
                # one started: end the stream (the frontend opens a new one).
                return None
            return image

        return await async_get_still_stream(
            request, _frame, self.content_type, interval
        )
