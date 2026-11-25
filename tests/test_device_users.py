"""Tests for one HA user per paired device.

The only mode: no owner token, no managed user, no flag.
Covers the locked-down device user
(never admin, guests always local only), idempotent provisioning and
delivery, rename / remote-access toggle with token rotation, deprovisioning
(push, reconcile, hub answer), the reconcile that never deletes foreign
users, reload/restart keeping every token, entry removal and the push
dispatch.
"""

from __future__ import annotations

import copy
from collections.abc import Generator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aioresponses import CallbackResult, aioresponses
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN, User
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from yarl import URL

from custom_components.halumar.brand import BRAND_NAME
from custom_components.halumar.const import (
    CONF_API_TOKEN,
    CONF_USE_SSL,
    DOMAIN,
    ENTRY_UNIQUE_ID,
)
from custom_components.halumar.device_users import (
    DEVICE_TOKEN_EXPIRATION,
    GUEST_TOKEN_GRACE,
    storage_key,
)
from custom_components.halumar.push import HubPushClient

from ._ha import MockConfigEntry, MockUser
from .common import (
    CLIENT_ID,
    CLIENT_TOKEN_URL,
    GUEST_ID,
    STATUS_URL,
    TEST_HOST,
    TEST_PORT,
    TEST_TOKEN,
    make_status_payload,
)

PREFIX = f"{DOMAIN}:"


# ── Fixtures and helpers ─────────────────────────────────────────────────


@pytest.fixture
def existing_owner(hass: HomeAssistant) -> Generator[MockUser, None, None]:
    """A human owner exists, as in every real installation."""
    yield MockUser(is_owner=True, name="Primary Owner").add_to_hass(hass)


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ENTRY_UNIQUE_ID,
        data={
            CONF_HOST: TEST_HOST,
            CONF_PORT: TEST_PORT,
            CONF_API_TOKEN: TEST_TOKEN,
            CONF_USE_SSL: False,
        },
    )
    entry.add_to_hass(hass)
    return entry


def _guest_expiry(days: float = 3) -> str:
    return (dt_util.utcnow() + timedelta(days=days)).replace(microsecond=0).isoformat()


def _snapshot() -> dict[str, Any]:
    payload = make_status_payload()
    tablet, guest = payload["clients"]
    tablet.update(
        name="iPhone",
        owner_label="Admin",
        remote_allowed=False,
        ha_user_id=None,
        ha_token_status="pending",
    )
    guest.update(
        name="iPhone",
        owner_label="Visitor",
        remote_allowed=False,
        expires_at=_guest_expiry(),
    )
    payload["provisioning"] = []
    return payload


def _request(client: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """provision_client data for a snapshot client."""
    item = {
        "client_id": client["id"],
        "display_name": client["name"],
        "owner_label": client.get("owner_label"),
        "is_guest": client.get("is_guest", False),
        "remote_allowed": client.get("remote_allowed", False),
        "guest_expires_at": client.get("expires_at")
        if client.get("is_guest")
        else None,
        "token_action": "ensure",
        "reason": "pair",
        "request_id": "5b0c7a52-9d4e-4c1a-8f3e-2a6b1c0d9e11",
    }
    item.update(overrides)
    return item


class FakeHub:
    """Mutable hub stand-in on top of aioresponses."""

    def __init__(self, mock_api: aioresponses) -> None:
        """Register the status and client-token endpoints."""
        self.snapshot = _snapshot()
        self.token_status = 200
        self.deliveries: list[dict[str, Any]] = []
        # The hub drops a device from provisioning[] once it has the token.
        self.clear_provisioning_on_ack = True
        mock_api.get(STATUS_URL, callback=self._status, repeat=True)
        mock_api.post(CLIENT_TOKEN_URL, callback=self._client_token, repeat=True)

    def client(self, client_id: str) -> dict[str, Any]:
        return next(c for c in self.snapshot["clients"] if c["id"] == client_id)

    def _status(self, url: URL, **kwargs: Any) -> CallbackResult:
        return CallbackResult(status=200, payload=copy.deepcopy(self.snapshot))

    def _client_token(self, url: URL, **kwargs: Any) -> CallbackResult:
        body = kwargs["json"]
        self.deliveries.append(body)
        if self.token_status != 200:
            return CallbackResult(
                status=self.token_status,
                payload={"detail": {"code": "client_not_found", "message": "x"}},
            )
        if self.clear_provisioning_on_ack:
            self.snapshot["provisioning"] = [
                p
                for p in self.snapshot["provisioning"]
                if p["client_id"] != body["client_id"]
            ]
        return CallbackResult(
            status=200,
            payload={
                "success": True,
                "client_id": body["client_id"],
                "ha_token_status": "active",
                "unchanged": False,
                "delivered": True,
                "superseded_refresh_token_id": None,
                "replaced_ha_user_id": None,
            },
        )


async def _settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done(wait_background_tasks=True)


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass)
    assert entry.state is ConfigEntryState.LOADED


async def _poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await _settle(hass)


async def _device_users(hass: HomeAssistant) -> list[User]:
    return [
        u
        for u in await hass.auth.async_get_users()
        if any(
            (t.client_name or "").startswith(PREFIX) for t in u.refresh_tokens.values()
        )
    ]


async def _user_of(hass: HomeAssistant, entry: MockConfigEntry, client_id: str) -> User:
    record = entry.runtime_data.device_users.records[client_id]
    user = await hass.auth.async_get_user(record["user_id"])
    assert user is not None
    return user


async def _provisioned(
    hass: HomeAssistant, mock_api: aioresponses, *client_ids: str
) -> tuple[MockConfigEntry, FakeHub]:
    hub = FakeHub(mock_api)
    hub.snapshot["provisioning"] = [_request(hub.client(cid)) for cid in client_ids]
    entry = _entry(hass)
    await _setup(hass, entry)
    return entry, hub


# ── No owner token ───────────────────────────────────────────────────────


async def test_setup_creates_no_managed_user_and_no_owner_token(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """Setup alone creates no HA user and never calls an owner-token endpoint."""
    hub = FakeHub(mock_api)
    entry = _entry(hass)
    await _setup(hass, entry)

    assert entry.runtime_data.device_users is not None
    users = [u for u in await hass.auth.async_get_users() if not u.system_generated]
    assert [u.id for u in users] == [existing_owner.id]
    assert hub.deliveries == []
    assert all("owner-token" not in str(key[1]) for key in mock_api.requests)


# ── Provisioning ─────────────────────────────────────────────────────────


async def test_pair_creates_locked_down_device_user_and_delivers(
    hass: HomeAssistant,
    mock_api: aioresponses,
    existing_owner: MockUser,
    hass_storage: dict[str, Any],
) -> None:
    """A provisioning request yields one non-admin, local-only user + one LLAT."""
    entry, hub = await _provisioned(hass, mock_api, CLIENT_ID)

    users = await _device_users(hass)
    assert len(users) == 1
    user = users[0]
    assert user.name == f"{BRAND_NAME} · Admin iPhone"
    assert user.is_admin is False
    assert user.is_owner is False
    assert user.system_generated is False
    assert user.credentials == []
    assert [g.id for g in user.groups] == [GROUP_ID_USER]
    assert user.local_only is True

    tokens = list(user.refresh_tokens.values())
    assert len(tokens) == 1
    assert tokens[0].token_type == TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
    assert tokens[0].client_name == f"{PREFIX}{CLIENT_ID}"
    assert tokens[0].access_token_expiration == DEVICE_TOKEN_EXPIRATION

    assert len(hub.deliveries) == 1
    body = hub.deliveries[0]
    assert body["client_id"] == CLIENT_ID
    assert body["ha_user_id"] == user.id
    assert body["refresh_token_id"] == tokens[0].id
    assert body["request_id"] == "5b0c7a52-9d4e-4c1a-8f3e-2a6b1c0d9e11"
    assert body["token"].count(".") == 2
    assert hass.auth.async_validate_access_token(body["token"]) is tokens[0]

    # Only ids are persisted: never the bearer, neither in the store nor the entry.
    stored = hass_storage[storage_key(entry.entry_id)]["data"]["clients"][CLIENT_ID]
    assert stored["user_id"] == user.id
    assert stored["delivered_refresh_token_id"] == tokens[0].id
    assert body["token"] not in repr(hass_storage)
    assert body["token"] not in repr(entry.data)


async def test_guest_with_remote_access_and_bounded_token(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """A guest follows remote_allowed (its profile); token is bounded."""
    hub = FakeHub(mock_api)
    hub.snapshot["provisioning"] = [_request(hub.client(GUEST_ID), remote_allowed=True)]
    entry = _entry(hass)
    await _setup(hass, entry)

    user = await _user_of(hass, entry, GUEST_ID)
    assert user.name == f"{BRAND_NAME} · Guest Visitor iPhone"
    assert user.local_only is False
    (token,) = user.refresh_tokens.values()
    assert token.access_token_expiration <= timedelta(
        days=3
    ) + GUEST_TOKEN_GRACE + timedelta(days=1)

    # Extending the guest beyond the token lifetime rotates the token.
    hub.client(GUEST_ID)["expires_at"] = _guest_expiry(days=60)
    await _poll(hass, entry)
    (new_token,) = user.refresh_tokens.values()
    assert new_token.id != token.id
    assert new_token.access_token_expiration >= timedelta(days=60)
    assert hub.deliveries[-1]["refresh_token_id"] == new_token.id


async def test_remote_allowed_device_is_not_local_only(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """remote_allowed=true -> local_only=false for a regular device."""
    hub = FakeHub(mock_api)
    hub.client(CLIENT_ID)["remote_allowed"] = True
    hub.snapshot["provisioning"] = [_request(hub.client(CLIENT_ID))]
    entry = _entry(hass)
    await _setup(hass, entry)

    user = await _user_of(hass, entry, CLIENT_ID)
    assert user.local_only is False
    assert user.is_admin is False


async def test_repeated_request_is_idempotent(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """The hub repeats a request until it has the token: same user, same token."""
    entry, hub = await _provisioned(hass, mock_api, CLIENT_ID)
    hub.snapshot["provisioning"] = [_request(hub.client(CLIENT_ID), reason="retry")]
    hub.clear_provisioning_on_ack = False

    await _poll(hass, entry)
    await _poll(hass, entry)

    users = await _device_users(hass)
    assert len(users) == 1
    assert len(users[0].refresh_tokens) == 1
    assert len(hub.deliveries) == 3
    assert len({d["refresh_token_id"] for d in hub.deliveries}) == 1


async def test_failed_delivery_retries_with_the_same_token(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """503 keeps user and token; the next poll delivers the same token."""
    hub = FakeHub(mock_api)
    hub.token_status = 503
    hub.snapshot["provisioning"] = [_request(hub.client(CLIENT_ID))]
    entry = _entry(hass)
    await _setup(hass, entry)
    user = await _user_of(hass, entry, CLIENT_ID)
    assert (
        entry.runtime_data.device_users.records[CLIENT_ID]["delivered_refresh_token_id"]
        is None
    )

    hub.token_status = 200
    await _poll(hass, entry)

    assert [u.id for u in await _device_users(hass)] == [user.id]
    assert len({d["refresh_token_id"] for d in hub.deliveries}) == 1
    assert len(hub.deliveries) == 2


async def test_gone_answer_removes_the_device_user(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """404 client_not_found: the device is gone, so is its HA user."""
    hub = FakeHub(mock_api)
    hub.token_status = 404
    hub.snapshot["provisioning"] = [_request(hub.client(CLIENT_ID))]
    entry = _entry(hass)
    await _setup(hass, entry)

    assert await _device_users(hass) == []
    assert CLIENT_ID not in entry.runtime_data.device_users.records


# ── Never admin ──────────────────────────────────────────────────────────


async def test_promoted_to_admin_gets_no_token(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """A device user an admin moved into the admin group never gets a token."""
    entry, hub = await _provisioned(hass, mock_api, CLIENT_ID)
    user = await _user_of(hass, entry, CLIENT_ID)
    (token,) = user.refresh_tokens.values()
    await hass.auth.async_update_user(user, group_ids=[GROUP_ID_ADMIN])
    assert user.is_admin is True

    await entry.runtime_data.device_users.async_handle_push(
        {
            "type": "provision_client",
            "data": _request(hub.client(CLIENT_ID), token_action="rotate"),
        }
    )

    assert len(hub.deliveries) == 1  # only the original delivery
    assert list(user.refresh_tokens) == [token.id]  # nothing minted
    assert await hass.auth.async_get_user(user.id) is not None  # not deleted either


async def test_no_device_user_without_an_owner(
    hass: HomeAssistant, mock_api: aioresponses
) -> None:
    """Without an HA owner the first new user would become the owner: refuse."""
    hub = FakeHub(mock_api)
    hub.snapshot["provisioning"] = [_request(hub.client(CLIENT_ID))]
    entry = _entry(hass)
    await _setup(hass, entry)

    assert await _device_users(hass) == []
    assert [
        u for u in await hass.auth.async_get_users() if not u.system_generated
    ] == []
    assert hub.deliveries == []


# ── Identity changes ─────────────────────────────────────────────────────


async def test_rename_and_remote_toggle_rotate_after_ack(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """update_client_identity renames and flips local_only.

    Granting remote access keeps the token: it works from outside at once,
    also for a device that is away. Taking it away rotates the
    token, so open sessions from outside end.
    """
    entry, hub = await _provisioned(hass, mock_api, CLIENT_ID)
    user = await _user_of(hass, entry, CLIENT_ID)
    (old,) = user.refresh_tokens.values()
    deliveries = len(hub.deliveries)

    identity = _request(
        hub.client(CLIENT_ID), display_name="iPhone 17", remote_allowed=True
    )
    for key in ("token_action", "reason", "request_id"):
        identity.pop(key)
    await entry.runtime_data.device_users.async_handle_push(
        {"type": "update_client_identity", "data": identity}
    )

    assert user.name == f"{BRAND_NAME} · Admin iPhone 17"
    assert user.local_only is False
    assert list(user.refresh_tokens) == [old.id]
    assert len(hub.deliveries) == deliveries

    identity["remote_allowed"] = False
    await entry.runtime_data.device_users.async_handle_push(
        {"type": "update_client_identity", "data": identity}
    )

    assert user.local_only is True
    (new,) = user.refresh_tokens.values()  # old one removed after the ack
    assert new.id != old.id
    assert new.client_name.startswith(f"{PREFIX}{CLIENT_ID}")
    assert hass.auth.async_get_refresh_token(old.id) is None
    assert hub.deliveries[-1]["refresh_token_id"] == new.id


async def test_admin_rotation_keeps_remote_access(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """A rotated token belongs to the same user: local_only stays off."""
    entry, hub = await _provisioned(hass, mock_api, CLIENT_ID)
    user = await _user_of(hass, entry, CLIENT_ID)
    client = hub.client(CLIENT_ID)
    client["remote_allowed"] = True
    identity = _request(client)
    for key in ("token_action", "reason", "request_id"):
        identity.pop(key)
    manager = entry.runtime_data.device_users
    await manager.async_handle_push(
        {"type": "update_client_identity", "data": identity}
    )
    (old,) = user.refresh_tokens.values()

    await manager.async_provision(
        _request(client, token_action="rotate", reason="rotate")
    )

    (new,) = user.refresh_tokens.values()
    assert new.id != old.id
    assert user.local_only is False


async def test_manual_local_only_on_remote_device_is_reverted_without_rotation(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """An admin ticking local_only by hand is undone; the token stays."""
    entry, hub = await _provisioned(hass, mock_api, CLIENT_ID)
    user = await _user_of(hass, entry, CLIENT_ID)
    hub.client(CLIENT_ID)["remote_allowed"] = True
    await _poll(hass, entry)
    (token,) = user.refresh_tokens.values()
    assert user.local_only is False

    await hass.auth.async_update_user(user, local_only=True)
    await _poll(hass, entry)

    assert user.local_only is False
    assert list(user.refresh_tokens) == [token.id]


async def test_identity_sync_from_poll_renames_without_new_token(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """A rename seen only in the snapshot updates the user, keeps the token."""
    entry, hub = await _provisioned(hass, mock_api, CLIENT_ID)
    user = await _user_of(hass, entry, CLIENT_ID)
    (token,) = user.refresh_tokens.values()

    hub.client(CLIENT_ID)["name"] = "Küchen-iPad"
    hub.client(CLIENT_ID)["owner_label"] = None
    await _poll(hass, entry)

    assert user.name == f"{BRAND_NAME} · Küchen-iPad"
    assert list(user.refresh_tokens) == [token.id]
    assert len(hub.deliveries) == 1


# ── Deprovision and reconcile ────────────────────────────────────────────


async def test_deprovision_push_removes_the_user(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """deprovision_client removes exactly that device's user."""
    entry, _hub = await _provisioned(hass, mock_api, CLIENT_ID, GUEST_ID)
    keep = await _user_of(hass, entry, CLIENT_ID)
    gone = await _user_of(hass, entry, GUEST_ID)

    await entry.runtime_data.device_users.async_handle_push(
        {
            "type": "deprovision_client",
            "data": {"client_id": GUEST_ID, "reason": "guest_expired"},
        }
    )

    assert await hass.auth.async_get_user(gone.id) is None
    assert await hass.auth.async_get_user(keep.id) is not None
    assert GUEST_ID not in entry.runtime_data.device_users.records


async def test_reconcile_deletes_only_orphaned_device_users(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """Missing device -> its user goes; foreign users always stay."""
    entry, hub = await _provisioned(hass, mock_api, CLIENT_ID, GUEST_ID)
    keep = await _user_of(hass, entry, CLIENT_ID)
    orphan = await _user_of(hass, entry, GUEST_ID)

    async def llat(user: User, name: str) -> str:
        token = await hass.auth.async_create_refresh_token(
            user, client_name=name, token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
        )
        return token.id

    admin = await hass.auth.async_create_user("Admin", group_ids=[GROUP_ID_ADMIN])
    admin_token = await llat(admin, f"{PREFIX}{GUEST_ID}")  # marked, but admin
    human = await hass.auth.async_create_user("Human", group_ids=[GROUP_ID_USER])
    script = await hass.auth.async_create_user("Script", group_ids=[GROUP_ID_USER])
    script_token = await llat(script, "Some other script")
    look_alike = await hass.auth.async_create_user(
        f"{BRAND_NAME} · Fake", group_ids=[GROUP_ID_USER]
    )
    other = await hass.auth.async_create_user("Other", group_ids=[GROUP_ID_USER])
    other_token = await llat(other, f"hub-{GUEST_ID}-a1b2c3")  # not the prefix
    mixed = await hass.auth.async_create_user("Mixed", group_ids=[GROUP_ID_USER])
    await llat(mixed, f"{PREFIX}{GUEST_ID}")
    await llat(mixed, "Personal script")

    hub.snapshot["clients"] = [hub.client(CLIENT_ID)]
    await _poll(hass, entry)

    assert await hass.auth.async_get_user(orphan.id) is None
    for user in (keep, existing_owner, admin, human, script, look_alike, mixed, other):
        assert await hass.auth.async_get_user(user.id) is not None, user.name
    for token_id in (admin_token, script_token, other_token):
        assert hass.auth.async_get_refresh_token(token_id) is not None


async def test_reconcile_skips_deletion_when_client_list_truncated(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """A truncated snapshot (> 500 clients) never deletes anything."""
    entry, hub = await _provisioned(hass, mock_api, GUEST_ID)
    user = await _user_of(hass, entry, GUEST_ID)
    filler = copy.deepcopy(hub.client(CLIENT_ID))
    hub.snapshot["clients"] = [
        {**filler, "id": f"00000000-0000-4000-8000-{i:012d}"} for i in range(501)
    ]
    await _poll(hass, entry)

    assert entry.runtime_data.clients_truncated is True
    assert await hass.auth.async_get_user(user.id) is not None


async def test_pending_deletion_or_revoked_client_loses_its_user(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """Revoked (is_active false) and pending-deletion clients are not active."""
    entry, hub = await _provisioned(hass, mock_api, CLIENT_ID, GUEST_ID)
    tablet = await _user_of(hass, entry, CLIENT_ID)
    guest = await _user_of(hass, entry, GUEST_ID)
    hub.client(CLIENT_ID)["is_active"] = False
    hub.client(GUEST_ID)["pending_deletion"] = True
    await _poll(hass, entry)

    assert await hass.auth.async_get_user(tablet.id) is None
    assert await hass.auth.async_get_user(guest.id) is None


async def test_held_deleted_device_keeps_its_user_until_released(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """A deleted device the hub still holds (ha_user_holds, e.g. offline
    until its app confirms the reset) keeps its user; once released (no longer
    held) the reconcile removes it. Hidden tombstones are not in clients[]."""
    entry, hub = await _provisioned(hass, mock_api, CLIENT_ID, GUEST_ID)
    tablet = await _user_of(hass, entry, CLIENT_ID)
    guest = await _user_of(hass, entry, GUEST_ID)
    hub.client(CLIENT_ID)["pending_deletion"] = True
    hub.client(CLIENT_ID)["is_active"] = False
    hub.snapshot["clients"] = [
        c for c in hub.snapshot["clients"] if c["id"] != GUEST_ID
    ]
    hub.snapshot["ha_user_holds"] = [CLIENT_ID, GUEST_ID]
    await _poll(hass, entry)

    assert await hass.auth.async_get_user(tablet.id) is not None
    assert await hass.auth.async_get_user(guest.id) is not None

    hub.snapshot["ha_user_holds"] = []
    await _poll(hass, entry)
    assert await hass.auth.async_get_user(tablet.id) is None
    assert await hass.auth.async_get_user(guest.id) is None


async def test_lost_store_adopts_marked_user_instead_of_duplicating(
    hass: HomeAssistant,
    mock_api: aioresponses,
    existing_owner: MockUser,
    hass_storage: dict[str, Any],
) -> None:
    """Without the mapping, the marked user of an active device is reused."""
    entry, hub = await _provisioned(hass, mock_api, CLIENT_ID)
    user = await _user_of(hass, entry, CLIENT_ID)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await _settle(hass)
    hass_storage.pop(storage_key(entry.entry_id), None)

    hub.snapshot["provisioning"] = [
        _request(hub.client(CLIENT_ID), reason="reprovision")
    ]
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass)

    assert [u.id for u in await _device_users(hass)] == [user.id]
    assert entry.runtime_data.device_users.records[CLIENT_ID]["user_id"] == user.id


@pytest.mark.parametrize("label", ["Guest", "Gast"])
async def test_lost_store_adopts_guest_user_with_either_label(
    hass: HomeAssistant,
    mock_api: aioresponses,
    existing_owner: MockUser,
    hass_storage: dict[str, Any],
    label: str,
) -> None:
    """Without the store a guest user is recognised by its name with the
    English or the German guest label, and then carries the English one."""
    entry, hub = await _provisioned(hass, mock_api, GUEST_ID)
    user = await _user_of(hass, entry, GUEST_ID)
    await hass.auth.async_update_user(
        user, name=f"{BRAND_NAME} · {label} Visitor iPhone"
    )
    assert await hass.config_entries.async_unload(entry.entry_id)
    await _settle(hass)
    hass_storage.pop(storage_key(entry.entry_id), None)

    hub.snapshot["provisioning"] = [
        _request(hub.client(GUEST_ID), reason="reprovision")
    ]
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass)

    assert [u.id for u in await _device_users(hass)] == [user.id]
    assert entry.runtime_data.device_users.records[GUEST_ID]["user_id"] == user.id
    assert user.name == f"{BRAND_NAME} · Guest Visitor iPhone"


async def test_shapeless_status_answer_keeps_device_users(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """HTTP 200 with ``{}`` or ``clients: null`` is a failed poll: no device
    user is removed."""
    entry, hub = await _provisioned(hass, mock_api, CLIENT_ID)
    user = await _user_of(hass, entry, CLIENT_ID)
    good = hub.snapshot
    for bad in (
        {},
        {**good, "clients": None},
        {**good, "clients": [None]},
        {**good, "clients": [{}]},
        {**good, "clients": ["x"]},
        {**good, "clients": [{"name": "no id"}]},
    ):
        hub.snapshot = bad
        await _poll(hass, entry)
        await _poll(hass, entry)
        assert entry.runtime_data.last_update_success is False
        assert await hass.auth.async_get_user(user.id) is not None
        assert CLIENT_ID in entry.runtime_data.device_users.records


async def _name_token_after(hass: HomeAssistant, user: User, client_id: str) -> None:
    """A device user mints an LLAT named after another device, drops its own."""
    own = list(user.refresh_tokens.values())
    await hass.auth.async_create_refresh_token(
        user,
        client_name=f"{PREFIX}{client_id}",
        token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
    )
    for token in own:
        hass.auth.async_remove_refresh_token(token)


async def test_token_named_after_other_device_is_not_adopted_with_store(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """A device user named after another device is not taken over."""
    entry, hub = await _provisioned(hass, mock_api, CLIENT_ID)
    other_device_user = await _user_of(hass, entry, CLIENT_ID)
    await _name_token_after(hass, other_device_user, GUEST_ID)

    hub.snapshot["clients"] = [hub.client(CLIENT_ID), hub.client(GUEST_ID)]
    hub.snapshot["provisioning"] = [_request(hub.client(GUEST_ID))]
    await _poll(hass, entry)

    guest_user = await _user_of(hass, entry, GUEST_ID)
    assert guest_user.id != other_device_user.id
    assert guest_user.local_only is True
    # The other device's user still belongs to its own device and is not removed.
    assert (
        entry.runtime_data.device_users.records[CLIENT_ID]["user_id"]
        == other_device_user.id
    )
    assert await hass.auth.async_get_user(other_device_user.id) is not None


async def test_token_named_after_other_device_is_not_adopted_with_lost_store(
    hass: HomeAssistant,
    mock_api: aioresponses,
    existing_owner: MockUser,
    hass_storage: dict[str, Any],
) -> None:
    """Without the store only a user with the expected name is adopted."""
    entry, hub = await _provisioned(hass, mock_api, CLIENT_ID)
    other_device_user = await _user_of(hass, entry, CLIENT_ID)
    await _name_token_after(hass, other_device_user, GUEST_ID)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await _settle(hass)
    hass_storage.pop(storage_key(entry.entry_id), None)

    hub.snapshot["clients"] = [hub.client(GUEST_ID)]
    hub.snapshot["provisioning"] = [_request(hub.client(GUEST_ID))]
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass)

    guest_user = await _user_of(hass, entry, GUEST_ID)
    assert guest_user.id != other_device_user.id


async def test_two_claimants_are_not_adopted(
    hass: HomeAssistant,
    mock_api: aioresponses,
    existing_owner: MockUser,
    hass_storage: dict[str, Any],
) -> None:
    entry, hub = await _provisioned(hass, mock_api, CLIENT_ID)
    user = await _user_of(hass, entry, CLIENT_ID)
    twin = await hass.auth.async_create_user(user.name, group_ids=[GROUP_ID_USER])
    await hass.auth.async_create_refresh_token(
        twin,
        client_name=f"{PREFIX}{CLIENT_ID}/abcdef",
        token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
    )
    assert await hass.config_entries.async_unload(entry.entry_id)
    await _settle(hass)
    hass_storage.pop(storage_key(entry.entry_id), None)
    hub.snapshot["provisioning"] = [
        _request(hub.client(CLIENT_ID), reason="reprovision")
    ]
    assert await hass.config_entries.async_setup(entry.entry_id)
    await _settle(hass)
    adopted = entry.runtime_data.device_users.records[CLIENT_ID]["user_id"]
    assert adopted not in (user.id, twin.id)


# ── Restart, removal ──────────────────────────────────────────


async def test_reload_and_restart_keep_device_tokens(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """Reload and HA restart leave every device user and token untouched."""
    entry, hub = await _provisioned(hass, mock_api, CLIENT_ID, GUEST_ID)
    before = {u.id: sorted(u.refresh_tokens) for u in await _device_users(hass)}
    deliveries = len(hub.deliveries)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await _settle(hass)
    for _ in range(2):  # HA restart: unload + setup from persisted state
        assert await hass.config_entries.async_unload(entry.entry_id)
        await _settle(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await _settle(hass)

    after = {u.id: sorted(u.refresh_tokens) for u in await _device_users(hass)}
    assert after == before
    for token_ids in after.values():
        for token_id in token_ids:
            assert hass.auth.async_get_refresh_token(token_id) is not None
    assert len(hub.deliveries) == deliveries


async def test_entry_removal_removes_device_users_only(
    hass: HomeAssistant,
    mock_api: aioresponses,
    existing_owner: MockUser,
    hass_storage: dict[str, Any],
) -> None:
    """Removing the integration removes its device users and the mapping."""
    entry, _hub = await _provisioned(hass, mock_api, CLIENT_ID, GUEST_ID)
    human = await hass.auth.async_create_user("Human", group_ids=[GROUP_ID_USER])

    assert await hass.config_entries.async_remove(entry.entry_id)
    await _settle(hass)

    assert await _device_users(hass) == []
    assert await hass.auth.async_get_user(human.id) is not None
    assert await hass.auth.async_get_user(existing_owner.id) is not None
    assert storage_key(entry.entry_id) not in hass_storage


# ── Push dispatch ────────────────────────────────────────────────────────


class _FakeWS:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        import json

        self._frames = [
            aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(m), None)
            for m in messages
        ] + [aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, None, None)]
        self.sent: list[dict[str, Any]] = []

    async def receive(self) -> aiohttp.WSMessage:
        return self._frames.pop(0)

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)


async def test_push_dispatches_device_user_messages(
    hass: HomeAssistant, mock_api: aioresponses, existing_owner: MockUser
) -> None:
    """provision_client goes to the manager, other events still refresh."""
    entry, hub = await _provisioned(hass, mock_api)
    coordinator = entry.runtime_data
    message = {"type": "provision_client", "data": _request(hub.client(CLIENT_ID))}
    ws = _FakeWS([message, {"type": "client_connected", "data": {}}])
    client = HubPushClient(hass, coordinator)

    with (
        patch.object(coordinator.device_users, "async_schedule_push") as schedule,
        patch.object(coordinator, "async_request_refresh", AsyncMock()) as refresh,
    ):
        await client._pump(ws)

    schedule.assert_called_once_with(message)
    refresh.assert_awaited_once()
