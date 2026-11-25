"""Pairing QR in the camera (hub option pairing_qr_camera).

* The camera shows an image marked ``X-Pairing-Pin: included`` only when the
  status snapshot reports a mode that allows its ``X-Pairing-Kind`` (guests:
  guest windows only, all: every window); off, a missing or unknown mode and
  a mismatched kind are refused.
* A QR code with the PIN is never cached: errors after it, a pairing state or
  mode change and a reload never bring it back.
* ``qr_mode`` is an attribute; the PIN never is.
* Who can read the image: a device user the integration creates (guest,
  member, read-only for the control path "checked") reads it through
  ``/api/camera_proxy`` with its own token, and through the camera's
  ``access_token`` attribute even without any sign-in. Home Assistant checks
  no entity permission there; this is why the option is off by default.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from typing import Any

import aiohttp
import pytest
from aiohttp.test_utils import make_mocked_request
from aioresponses import CallbackResult, aioresponses
from homeassistant.auth.const import GROUP_ID_READ_ONLY, GROUP_ID_USER
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ._ha import MockConfigEntry, MockUser
from .common import (
    BASE_URL,
    GUEST_ID,
    SECRET_PIN,
    make_status_payload,
    setup_integration,
)
from .test_device_users import FakeHub, _entry, _request, _setup

QR_URL = f"{BASE_URL}/api/integration/pairing-qr"
CAMERA = "camera.halumar_hub_pairing_qr_code"
QR_PNG = b"\x89PNG-QR-with-pin"
STATUS_PNG = b"\x89PNG-status"


def _payload(mode: Any = "guests", *, active: bool = True, guest: bool = True):
    payload = make_status_payload()
    payload["pairing_active"] = active
    payload["pairing_is_guest"] = guest
    payload["pairing_remaining"] = 200 if active else 0
    if mode is not None:
        payload["pairing_qr_camera"] = mode
    return payload


def _camera(hass: HomeAssistant):
    return hass.data["entity_components"]["camera"].get_entity(CAMERA)


def _qr(mock_api: aioresponses, kind: str = "guest", **kw: Any) -> None:
    mock_api.get(
        QR_URL,
        body=QR_PNG,
        headers={
            "X-Pairing-Active": "true",
            "X-Pairing-Pin": "included",
            "X-Pairing-Kind": kind,
        },
        **kw,
    )


def _status(mock_api: aioresponses, active: bool = True, **kw: Any) -> None:
    mock_api.get(
        QR_URL,
        body=STATUS_PNG,
        headers={
            "X-Pairing-Active": "true" if active else "false",
            "X-Pairing-Pin": "omitted",
        },
        **kw,
    )


# ── Accept or refuse ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "kind", "shown"),
    [
        ("off", "guest", False),
        ("off", "device", False),
        ("guests", "guest", True),
        ("guests", "device", False),
        ("all", "guest", True),
        ("all", "device", True),
        (None, "guest", False),  # a hub without the field
        ("on", "guest", False),  # unknown value
        ("all", "admin", False),  # unknown kind
        ("all", None, False),  # no kind
    ],
)
async def test_included_image_only_in_the_reported_mode(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    mode: Any,
    kind: str | None,
    shown: bool,
) -> None:
    await setup_integration(hass, mock_api, config_entry, _payload(mode))
    headers = {"X-Pairing-Active": "true", "X-Pairing-Pin": "included"}
    if kind is not None:
        headers["X-Pairing-Kind"] = kind
    mock_api.get(QR_URL, body=QR_PNG, headers=headers)
    assert await _camera(hass).async_camera_image() == (QR_PNG if shown else None)


async def test_included_image_needs_an_open_window(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry, _payload("all"))
    mock_api.get(
        QR_URL,
        body=QR_PNG,
        headers={
            "X-Pairing-Active": "false",
            "X-Pairing-Pin": "included",
            "X-Pairing-Kind": "guest",
        },
    )
    assert await _camera(hass).async_camera_image() is None


async def test_unmarked_image_during_a_window_still_refused(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Also with the option on, an image without a marker is not shown."""
    await setup_integration(hass, mock_api, config_entry, _payload("all"))
    mock_api.get(QR_URL, body=QR_PNG, headers={"X-Pairing-Active": "true"})
    assert await _camera(hass).async_camera_image() is None


# ── Never cached ─────────────────────────────────────────────────────────


async def test_qr_is_never_served_from_the_cache(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry, _payload("guests"))
    camera = _camera(hass)
    _status(mock_api)
    assert await camera.async_camera_image() == STATUS_PNG
    _qr(mock_api)
    assert await camera.async_camera_image() == QR_PNG
    # The hub is unreachable right after: the last image would be the QR.
    mock_api.get(QR_URL, exception=aiohttp.ClientConnectionError("down"))
    assert await camera.async_camera_image() is None
    mock_api.get(QR_URL, status=503)
    assert await camera.async_camera_image() is None
    assert camera._cached_image is None  # noqa: SLF001


async def test_cache_cleared_on_window_and_mode_change(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(
        hass, mock_api, config_entry, _payload("guests", active=False), repeat=False
    )
    coordinator = config_entry.runtime_data
    camera = _camera(hass)
    _status(mock_api, active=False)
    assert await camera.async_camera_image() == STATUS_PNG

    # The window opens: the cached standby image goes.
    coordinator.async_set_updated_data(_payload("guests"))
    await hass.async_block_till_done()
    assert camera._cached_image is None  # noqa: SLF001

    _status(mock_api)
    assert await camera.async_camera_image() == STATUS_PNG
    # The mode changes (hub restarted with another option).
    coordinator.async_set_updated_data(_payload("off"))
    await hass.async_block_till_done()
    assert camera._cached_image is None  # noqa: SLF001


async def test_expired_window_and_reload_never_bring_the_qr_back(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    await setup_integration(hass, mock_api, config_entry, _payload("all"))
    _qr(mock_api)
    assert await _camera(hass).async_camera_image() == QR_PNG

    # The window ended; the hub is unreachable.
    config_entry.runtime_data.async_set_updated_data(_payload("all", active=False))
    await hass.async_block_till_done()
    mock_api.get(QR_URL, exception=aiohttp.ClientConnectionError("down"))
    assert await _camera(hass).async_camera_image() is None

    # Reload (as after a restart): a new camera, still nothing to show.
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    mock_api.get(QR_URL, exception=aiohttp.ClientConnectionError("down"))
    assert await _camera(hass).async_camera_image() is None


# ── Attributes ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("off", "off"), ("guests", "guests"), ("all", "all"), (None, "off"), ("x", "off")],
)
async def test_qr_mode_attribute(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    mode: Any,
    expected: str,
) -> None:
    payload = _payload(mode)
    payload["pairing_pin"] = SECRET_PIN  # never sent; must never surface
    await setup_integration(hass, mock_api, config_entry, payload)
    state = hass.states.get(CAMERA)
    assert state.attributes["qr_mode"] == expected
    assert config_entry.runtime_data.data["pairing_qr_camera"] == expected
    assert SECRET_PIN not in repr(state.attributes)


# ── Who can read the image (restricted device users) ─────────────────────


@pytest.fixture
def owner(hass: HomeAssistant) -> Generator[MockUser, None, None]:
    yield MockUser(is_owner=True, name="Owner").add_to_hass(hass)


@pytest.mark.parametrize("group", [GROUP_ID_USER, GROUP_ID_READ_ONLY])
async def test_a_guest_device_token_reads_the_qr(
    hass: HomeAssistant,
    mock_api: aioresponses,
    owner: MockUser,
    hass_client_no_auth,
    hass_ws_client,
    group: str,
) -> None:
    """Home Assistant serves the camera image to every signed-in user.

    The guest's own HA user (created by this integration, never an admin,
    local only; read-only for the control path "checked") gets the QR with
    its bearer token, and the state's access_token alone opens it without
    any sign-in. Nothing tells the camera who asked.
    """
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "websocket_api", {})
    mock_api.passthrough_unmatched = True  # the HA test server
    hub = FakeHub(mock_api)
    hub.snapshot.update(
        pairing_active=True, pairing_is_guest=True, pairing_qr_camera="guests"
    )
    hub.snapshot["provisioning"] = [_request(hub.client(GUEST_ID))]
    entry = _entry(hass)
    await _setup(hass, entry)
    token = hub.deliveries[0]["token"]
    user = (hass.auth.async_validate_access_token(token)).user
    if group != GROUP_ID_USER:
        await hass.auth.async_update_user(user, group_ids=[group])
    assert [g.id for g in user.groups] == [group]
    assert not user.is_admin
    _qr(mock_api, repeat=True)

    client = await hass_client_no_auth()
    resp = await client.get(
        f"/api/camera_proxy/{CAMERA}", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status == 200
    assert await resp.read() == QR_PNG

    ws = await hass_ws_client(hass, access_token=token)
    await ws.send_json({"id": 1, "type": "get_states"})
    msg = await ws.receive_json()
    assert msg["success"]
    camera = next(s for s in msg["result"] if s["entity_id"] == CAMERA)
    signed = camera["attributes"]["entity_picture"]
    assert camera["attributes"]["access_token"] in signed

    anonymous = await hass_client_no_auth()
    resp = await anonymous.get(signed)
    assert resp.status == 200
    assert await resp.read() == QR_PNG


async def test_with_the_option_off_a_guest_token_reads_no_pin(
    hass: HomeAssistant, mock_api: aioresponses, owner: MockUser, hass_client_no_auth
) -> None:
    assert await async_setup_component(hass, "http", {})
    mock_api.passthrough_unmatched = True  # the HA test server
    hub = FakeHub(mock_api)
    hub.snapshot.update(pairing_active=True, pairing_is_guest=True)
    hub.snapshot["provisioning"] = [_request(hub.client(GUEST_ID))]
    entry = _entry(hass)
    await _setup(hass, entry)
    token = hub.deliveries[0]["token"]
    # Even if a hub sent a QR, the integration refuses it without the option.
    _qr(mock_api, repeat=True)

    client = await hass_client_no_auth()
    resp = await client.get(
        f"/api/camera_proxy/{CAMERA}", headers={"Authorization": f"Bearer {token}"}
    )
    # The camera has no image to give: Home Assistant answers 500.
    assert resp.status == 500
    assert QR_PNG not in await resp.read()


# ── MJPEG streams cannot outlive their authorisation or their window ─────


async def _read(resp, needle: bytes, timeout: float = 5.0) -> tuple[bool, bool]:
    """Read a stream until ``needle`` or its end: (needle seen, stream ended)."""
    buf = b""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        try:
            chunk = await asyncio.wait_for(resp.content.read(4096), 0.3)
        except TimeoutError:
            continue
        if not chunk:
            return needle in buf, True
        buf += chunk
        if needle in buf:
            return True, False
    return needle in buf, False


class _Hub:
    """Fake hub whose image follows a mutable window state."""

    def __init__(self, mock_api: aioresponses, mode: str = "guests") -> None:
        self.fake = FakeHub(mock_api)
        self.fake.snapshot.update(
            pairing_active=False, pairing_is_guest=False, pairing_qr_camera=mode
        )
        self.fake.snapshot["provisioning"] = [_request(self.fake.client(GUEST_ID))]
        self.window: str | None = None  # None, "qr" or "status"
        self.window_id = "1"
        mock_api.get(QR_URL, callback=self._image, repeat=True)

    def _image(self, url: Any, **kw: Any) -> CallbackResult:
        if self.window == "qr":
            return CallbackResult(
                status=200,
                body=QR_PNG,
                headers={
                    "X-Pairing-Active": "true",
                    "X-Pairing-Pin": "included",
                    "X-Pairing-Kind": "guest",
                    "X-Pairing-Window": self.window_id,
                },
            )
        active = self.window == "status"
        return CallbackResult(
            status=200,
            body=STATUS_PNG + (b"-pairing" if active else b"-standby"),
            headers={
                "X-Pairing-Active": "true" if active else "false",
                "X-Pairing-Pin": "omitted",
            },
        )


async def _stream_setup(hass: HomeAssistant, mock_api: aioresponses, mode: str):
    assert await async_setup_component(hass, "http", {})
    mock_api.passthrough_unmatched = True  # the HA test server
    hub = _Hub(mock_api, mode)
    entry = _entry(hass)
    await _setup(hass, entry)
    token = hub.fake.deliveries[0]["token"]
    camera = _camera(hass)
    camera._attr_frame_interval = 0.05  # noqa: SLF001
    return hub, entry, token, camera


async def test_streams_end_with_their_credentials_and_never_carry_a_later_qr(
    hass: HomeAssistant, mock_api: aioresponses, owner: MockUser, hass_client_no_auth
) -> None:
    """A removed guest's stream and an expired signed stream get no later QR."""
    hub, entry, token, camera = await _stream_setup(hass, mock_api, "guests")
    user = hass.auth.async_validate_access_token(token).user
    signed = camera.access_tokens[-1]

    client = await hass_client_no_auth()
    by_bearer = await client.get(
        f"/api/camera_proxy_stream/{CAMERA}",
        headers={"Authorization": f"Bearer {token}"},
    )
    anonymous = await hass_client_no_auth()
    by_signed = await anonymous.get(f"/api/camera_proxy_stream/{CAMERA}?token={signed}")
    assert by_bearer.status == by_signed.status == 200
    assert (await _read(by_bearer, b"-standby"))[0]
    assert (await _read(by_signed, b"-standby"))[0]

    await hass.auth.async_remove_user(user)
    camera.async_update_token()
    camera.async_update_token()
    assert signed not in camera.access_tokens

    hub.window = "qr"
    for stream in (by_bearer, by_signed):
        seen, ended = await _read(stream, QR_PNG)
        assert not seen
        assert ended


async def test_a_valid_standby_stream_ends_when_a_qr_appears(
    hass: HomeAssistant, mock_api: aioresponses, owner: MockUser, hass_client_no_auth
) -> None:
    hub, entry, token, camera = await _stream_setup(hass, mock_api, "guests")
    client = await hass_client_no_auth()
    stream = await client.get(
        f"/api/camera_proxy_stream/{CAMERA}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert (await _read(stream, b"-standby"))[0]
    hub.window = "qr"
    assert await _read(stream, QR_PNG) == (False, True)
    # A new stream opened now (as the frontend does) shows the QR.
    again = await client.get(
        f"/api/camera_proxy_stream/{CAMERA}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert (await _read(again, QR_PNG))[0]
    again.close()


@pytest.mark.parametrize("after", ["closed", "used"])
async def test_a_stream_opened_in_a_window_ends_with_it(
    hass: HomeAssistant,
    mock_api: aioresponses,
    owner: MockUser,
    hass_client_no_auth,
    after: str,
) -> None:
    hub, entry, token, camera = await _stream_setup(hass, mock_api, "guests")
    hub.window = "qr"
    client = await hass_client_no_auth()
    stream = await client.get(
        f"/api/camera_proxy_stream/{CAMERA}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert (await _read(stream, QR_PNG))[0]
    # The window closes, or the PIN is used (the hub sends the status image).
    hub.window = None if after == "closed" else "status"
    assert (await _read(stream, b"-standby" if after == "closed" else b"-pairing")) == (
        False,
        True,
    )


async def test_a_new_window_ends_a_qr_stream(
    hass: HomeAssistant, mock_api: aioresponses, owner: MockUser, hass_client_no_auth
) -> None:
    """A QR of a new window is never sent on an old stream."""
    hub, entry, token, camera = await _stream_setup(hass, mock_api, "guests")
    hub.window = "qr"
    client = await hass_client_no_auth()
    stream = await client.get(
        f"/api/camera_proxy_stream/{CAMERA}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert (await _read(stream, QR_PNG))[0]
    hub.window_id = "2"
    _seen, ended = await _read(stream, b"never")
    assert ended


async def test_off_mode_streams_keep_running(
    hass: HomeAssistant, mock_api: aioresponses, owner: MockUser, hass_client_no_auth
) -> None:
    hub, entry, token, camera = await _stream_setup(hass, mock_api, "off")
    client = await hass_client_no_auth()
    stream = await client.get(
        f"/api/camera_proxy_stream/{CAMERA}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert await _read(stream, b"-standby") == (True, False)
    hub.window = "status"
    assert await _read(stream, b"-pairing") == (True, False)
    hub.window = None
    assert await _read(stream, b"-standby") == (True, False)
    stream.close()


async def test_a_stream_without_known_credentials_ends(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """A request that carries neither a refresh token nor a camera token."""
    await setup_integration(hass, mock_api, config_entry, _payload("off"))
    camera = _camera(hass)
    request = make_mocked_request("GET", f"/api/camera_proxy_stream/{CAMERA}")
    assert camera._stream_authorised(request) is False  # noqa: SLF001
