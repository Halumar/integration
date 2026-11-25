"""Edge cases of the per-device HA users (device_users.py) on a stub hub.

The end-to-end flows run in test_device_users.py against a fake hub;
these tests drive DeviceUserManager directly to reach the defensive paths:
invalid hub data, privileged or foreign users, races and lost stores.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN, User
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.halumar.brand import BRAND_NAME
from custom_components.halumar.const import DOMAIN
from custom_components.halumar.coordinator import HubApiError
from custom_components.halumar.device_users import (
    DeviceIdentity,
    DeviceUserManager,
    DeviceUserSafetyError,
    assert_safe_device_user,
    device_token_client_id,
    storage_key,
)

from ._ha import MockConfigEntry, MockUser

CID = "aaaaaaaa-0000-4000-8000-000000000001"
OTHER = "aaaaaaaa-0000-4000-8000-000000000002"


class StubCoordinator:
    """The coordinator attributes DeviceUserManager reads."""

    def __init__(self, clients: list[dict[str, Any]] | None = None) -> None:
        self.last_update_success = True
        self.data: dict[str, Any] = {"clients": clients or []}
        self.snapshot_started_at = dt_util.utcnow() + timedelta(seconds=5)
        self.clients_truncated = False
        self.post_client_token = AsyncMock(return_value={"success": True})
        self.async_request_refresh = AsyncMock()


def _client(client_id: str = CID, name: str = "iPad", **extra: Any) -> dict[str, Any]:
    return {"id": client_id, "name": name, "is_active": True, **extra}


@pytest.fixture
def owner(hass: HomeAssistant) -> MockUser:
    return MockUser(is_owner=True, name="Owner").add_to_hass(hass)


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    return entry


async def _manager(
    hass: HomeAssistant, entry: MockConfigEntry, coordinator: StubCoordinator | None
) -> DeviceUserManager:
    manager = DeviceUserManager(hass, entry, coordinator)  # type: ignore[arg-type]
    await manager.async_setup()
    return manager


async def _marked_user(
    hass: HomeAssistant, client_id: str, name: str | None = None
) -> User:
    user = await hass.auth.async_create_user(
        name or f"{BRAND_NAME} · iPad", group_ids=[GROUP_ID_USER], local_only=True
    )
    await hass.auth.async_create_refresh_token(
        user,
        client_name=f"{DOMAIN}:{client_id}",
        token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
    )
    return user


# ── DeviceIdentity and helpers ───────────────────────────────────────────


def test_identity_rejects_invalid_requests() -> None:
    """Non-dict data, bad ids and missing names are rejected."""
    with pytest.raises(ValueError, match="client_id"):
        DeviceIdentity.from_request("not a dict")
    with pytest.raises(ValueError, match="client_id"):
        DeviceIdentity.from_request({"client_id": "../x", "display_name": "x"})
    with pytest.raises(ValueError, match="display_name"):
        DeviceIdentity.from_request({"client_id": CID, "display_name": "\x00 "})


def test_identity_normalizes_fields() -> None:
    """Naive expiry is dropped, guests follow remote access, owner prefix
    is not doubled."""
    guest = DeviceIdentity.from_request(
        {
            "client_id": CID,
            "display_name": "Visitor iPhone",
            "owner_label": "visitor",
            "is_guest": True,
            "remote_allowed": True,
            "guest_expires_at": "2026-07-04T10:00:00",
        }
    )
    assert guest.guest_expires_at is None
    assert guest.remote_allowed is True
    assert guest.local_only is False
    assert guest.user_name == f"{BRAND_NAME} · Guest Visitor iPhone"
    assert guest.token_expiration() == timedelta(days=3650)


@pytest.mark.parametrize(
    ("display_name", "owner_label", "expected"),
    [
        ("Kind", "Kind", "Kind"),
        ("kind", "Kind", "kind"),
        ("Kind iPad", "Kind", "Kind iPad"),
        ("Kindle", "Kind", "Kind Kindle"),
        ("iPad", "Kind", "Kind iPad"),
        ("iPad", None, "iPad"),
    ],
)
def test_user_name_does_not_double_the_owner(
    display_name: str, owner_label: str | None, expected: str
) -> None:
    """A device named like its owner is not named twice."""
    identity = DeviceIdentity.from_request(
        {
            "client_id": CID,
            "display_name": display_name,
            "owner_label": owner_label,
            "is_guest": False,
            "remote_allowed": False,
        }
    )
    assert identity.user_name == f"{BRAND_NAME} · {expected}"


async def test_device_token_client_id_ignores_other_tokens(
    hass: HomeAssistant, owner: MockUser
) -> None:
    """Only long-lived tokens with a valid device name mark a device."""
    user = await hass.auth.async_create_user("x", group_ids=[GROUP_ID_USER])
    normal = await hass.auth.async_create_refresh_token(user, client_id="http://x/")
    bad = await hass.auth.async_create_refresh_token(
        user,
        client_name=f"{DOMAIN}:not valid!",
        token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
    )
    assert device_token_client_id(normal) is None
    assert device_token_client_id(bad) is None


def test_assert_safe_device_user_lists_every_problem() -> None:
    """Every unsafe property of a user is named."""

    class Group:
        id = "system-admin"

    class Unsafe:
        id = "u1"
        is_owner = True
        is_admin = True
        system_generated = True
        credentials = ["x"]
        groups = [Group()]
        is_active = False
        local_only = False

    identity = DeviceIdentity.from_request({"client_id": CID, "display_name": "iPad"})
    with pytest.raises(DeviceUserSafetyError) as err:
        assert_safe_device_user(Unsafe(), identity)  # type: ignore[arg-type]
    for problem in (
        "owner",
        "admin",
        "system-generated",
        "login credentials",
        "system-users",
        "deactivated",
        "local only",
    ):
        assert problem in str(err.value)


# ── Manager: store, push dispatch, scheduling ────────────────────────────


async def test_setup_seeds_minted_ids_from_records(
    hass: HomeAssistant, entry: MockConfigEntry, hass_storage: dict[str, Any]
) -> None:
    """Token ids of stored records count as minted (after an upgrade)."""
    hass_storage[storage_key(entry.entry_id)] = {
        "version": 1,
        "key": storage_key(entry.entry_id),
        "data": {
            "clients": {CID: {"user_id": "u", "refresh_token_id": "t1"}},
            "minted_token_ids": "not a list",
        },
    }
    manager = await _manager(hass, entry, None)
    assert manager._minted == ["t1"]  # noqa: SLF001
    assert manager.user_id_of(CID) == "u"
    assert manager.user_id_of(OTHER) is None


async def test_push_with_invalid_data_is_ignored(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed hub request is logged and dropped."""
    manager = await _manager(hass, entry, StubCoordinator())
    await manager.async_handle_push(
        {"type": "provision_client", "data": {"client_id": "../x"}}
    )
    await manager.async_handle_push({"type": "deprovision_client", "data": None})
    assert "Ignoring invalid provision_client message" in caplog.text


async def test_schedule_push_and_reconcile_run_in_background(
    hass: HomeAssistant, entry: MockConfigEntry, owner: MockUser
) -> None:
    """Scheduled work runs as entry background tasks; a busy reconcile repeats."""
    coordinator = StubCoordinator([_client()])
    coordinator.data["provisioning"] = [
        {"client_id": CID, "display_name": "iPad", "token_action": "ensure"}
    ]
    manager = await _manager(hass, entry, coordinator)

    manager._reconcile_running = True  # noqa: SLF001
    manager.async_schedule_reconcile()
    await manager.async_reconcile()
    assert manager._reconcile_again is True  # noqa: SLF001
    manager._reconcile_running = False  # noqa: SLF001

    manager.async_schedule_reconcile()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert CID in manager.records

    manager.async_schedule_push(
        {"type": "deprovision_client", "data": {"client_id": CID}}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert CID not in manager.records

    coordinator.last_update_success = False
    manager.async_schedule_reconcile()
    await manager.async_reconcile()  # no snapshot: nothing happens


# ── Manager: identity updates and delivery ───────────────────────────────


async def test_update_identity_recreates_a_user_removed_outside(
    hass: HomeAssistant, entry: MockConfigEntry, owner: MockUser
) -> None:
    """Unknown devices are skipped; a user deleted by hand is created again."""
    manager = await _manager(hass, entry, StubCoordinator([_client()]))
    identity = DeviceIdentity.from_snapshot_client(_client())
    await manager.async_update_identity(identity)
    assert manager.records == {}

    assert await manager.async_provision(
        {"client_id": CID, "display_name": "iPad", "request_id": 5}
    )
    first = manager.user_id_of(CID)
    assert first is not None
    user = await hass.auth.async_get_user(first)
    assert user is not None
    await hass.auth.async_remove_user(user)

    await manager.async_update_identity(identity)
    assert manager.user_id_of(CID) not in (None, first)


async def test_delivery_failures_keep_the_user(
    hass: HomeAssistant, entry: MockConfigEntry, owner: MockUser
) -> None:
    """Unexpected delivery errors are retried later; the user stays."""
    coordinator = StubCoordinator([_client()])
    coordinator.post_client_token.side_effect = RuntimeError("boom")
    manager = await _manager(hass, entry, coordinator)
    assert not await manager.async_provision({"client_id": CID, "display_name": "iPad"})
    assert manager.user_id_of(CID) is not None

    coordinator.post_client_token.side_effect = HubApiError("x", status=500)
    assert not await manager.async_provision({"client_id": CID, "display_name": "iPad"})
    assert manager.user_id_of(CID) is not None


async def test_last_check_before_the_bearer(
    hass: HomeAssistant, entry: MockConfigEntry, owner: MockUser
) -> None:
    """A user that turns unsafe between the checks gets no bearer."""
    coordinator = StubCoordinator([_client()])
    manager = await _manager(hass, entry, coordinator)
    with patch(
        "custom_components.halumar.device_users.assert_safe_device_user",
        side_effect=[None, DeviceUserSafetyError("promoted")],
    ):
        assert not await manager.async_provision(
            {"client_id": CID, "display_name": "iPad"}
        )
    coordinator.post_client_token.assert_not_called()


async def test_privileged_new_user_is_removed(
    hass: HomeAssistant, entry: MockConfigEntry, owner: MockUser
) -> None:
    """Should HA hand back a privileged user, it is removed and gets no token."""
    manager = await _manager(hass, entry, StubCoordinator([_client()]))
    admin = await hass.auth.async_create_user("Admin", group_ids=["system-admin"])
    with (
        patch.object(hass.auth, "async_create_user", AsyncMock(return_value=admin)),
        patch.object(hass.auth, "async_remove_user", AsyncMock()) as remove,
    ):
        assert not await manager.async_provision(
            {"client_id": CID, "display_name": "iPad"}
        )
    remove.assert_awaited_once_with(admin)


async def test_guest_token_without_known_expiry_is_renewed(
    hass: HomeAssistant, entry: MockConfigEntry, owner: MockUser
) -> None:
    """A guest record without token expiry never counts as covering."""
    expires = (dt_util.utcnow() + timedelta(days=2)).isoformat()
    guest = _client(is_guest=True, expires_at=expires)
    coordinator = StubCoordinator([guest])
    manager = await _manager(hass, entry, coordinator)
    request = {
        "client_id": CID,
        "display_name": "iPad",
        "is_guest": True,
        "guest_expires_at": expires,
    }
    assert await manager.async_provision(request)
    manager._records[CID]["token_expires_at"] = None  # noqa: SLF001
    assert await manager.async_provision(request)
    assert coordinator.post_client_token.await_count == 2


# ── Manager: removal ─────────────────────────────────────────────────────


async def test_remove_refuses_foreign_and_privileged_users(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    owner: MockUser,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only users this integration owns are ever removed."""
    manager = await _manager(hass, entry, StubCoordinator())
    foreign = await hass.auth.async_create_user("Foreign", group_ids=[GROUP_ID_USER])
    await manager._async_remove_user(foreign, CID)  # noqa: SLF001
    assert await hass.auth.async_get_user(foreign.id) is not None
    assert "not a device user" in caplog.text

    # Record points to a user that is gone: the marked user is found instead.
    marked = await _marked_user(hass, CID)
    manager._records[CID] = {"user_id": "gone"}  # noqa: SLF001
    await manager.async_deprovision(CID)
    assert await hass.auth.async_get_user(marked.id) is None


async def test_remove_all_removes_marked_users_without_record(
    hass: HomeAssistant, entry: MockConfigEntry, owner: MockUser
) -> None:
    """Entry removal also finds device users the store does not know."""
    marked = await _marked_user(hass, OTHER)
    await DeviceUserManager(hass, entry, None).async_remove_all()
    assert await hass.auth.async_get_user(marked.id) is None
    assert await hass.auth.async_get_user(owner.id) is not None


# ── Reconcile ────────────────────────────────────────────────────────────


async def test_reconcile_skips_bad_entries_and_young_records(
    hass: HomeAssistant, entry: MockConfigEntry, owner: MockUser
) -> None:
    """Invalid provisioning items, invalid snapshot clients, young records."""
    coordinator = StubCoordinator([_client(name="\x00")])
    coordinator.data["provisioning"] = ["x", {"client_id": "../bad"}]
    manager = await _manager(hass, entry, coordinator)
    manager._records = {  # noqa: SLF001
        CID: {"user_id": "u-cid"},
        OTHER: {
            "user_id": "u-other",
            "created_at": (
                coordinator.snapshot_started_at + timedelta(seconds=1)
            ).isoformat(),
        },
    }

    await manager.async_reconcile()
    # CID: active but its identity is invalid → kept; OTHER: younger than the
    # snapshot request → kept.
    assert set(manager.records) == {CID, OTHER}

    coordinator.snapshot_started_at = None
    await manager.async_reconcile()
    assert set(manager.records) == {CID, OTHER}


async def test_reconcile_adopts_and_removes_marked_users(
    hass: HomeAssistant, entry: MockConfigEntry, owner: MockUser
) -> None:
    """Lost store: the marked user of an active device with the expected
    name is adopted, the one of a gone device is removed."""
    coordinator = StubCoordinator([_client()])
    manager = await _manager(hass, entry, coordinator)
    adopt = await _marked_user(hass, CID)
    stray = await _marked_user(hass, OTHER, name="Stray")

    await manager.async_reconcile()

    assert manager.user_id_of(CID) == adopt.id
    assert await hass.auth.async_get_user(stray.id) is None


async def test_reconcile_never_adopts_unexpected_or_young_users(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    owner: MockUser,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Wrong name (lost store), tokens newer than the snapshot, foreign tokens."""
    coordinator = StubCoordinator([_client(), _client(OTHER, name="\x00")])
    manager = await _manager(hass, entry, coordinator)
    wrong_name = await _marked_user(hass, CID, name="Someone else")
    invalid_identity = await _marked_user(hass, OTHER, name="Other")

    await manager.async_reconcile()
    assert manager.records == {}
    assert await hass.auth.async_get_user(wrong_name.id) is not None
    assert await hass.auth.async_get_user(invalid_identity.id) is not None
    assert "Not adopting HA user" in caplog.text

    # A user created after the snapshot was requested is left alone.
    coordinator.snapshot_started_at = dt_util.utcnow() - timedelta(minutes=1)
    coordinator.data["clients"] = []
    young = await _marked_user(hass, "aaaaaaaa-0000-4000-8000-000000000003")
    await manager.async_reconcile()
    assert await hass.auth.async_get_user(young.id) is not None


async def test_reconcile_ignores_tokens_on_another_devices_user(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    owner: MockUser,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A mapped user carrying only tokens named after another device stays."""
    coordinator = StubCoordinator([_client(OTHER)])
    # CID is deleted but held: its record stays and gets no identity sync.
    coordinator.data["ha_user_holds"] = [CID]
    manager = await _manager(hass, entry, coordinator)
    user = await _marked_user(hass, OTHER)
    manager._records[CID] = {"user_id": user.id}  # noqa: SLF001

    await manager.async_reconcile()
    assert await hass.auth.async_get_user(user.id) is not None
    assert "carries tokens of device" in caplog.text


async def test_adoptable_rules(
    hass: HomeAssistant, entry: MockConfigEntry, owner: MockUser
) -> None:
    """Removal may take any stray marked user; adoption needs a minted token."""
    manager = await _manager(hass, entry, None)
    user = await _marked_user(hass, CID)
    assert manager._adoptable(user, CID, None, for_removal=True)  # noqa: SLF001
    manager._minted = ["not-this-token"]  # noqa: SLF001
    assert not manager._adoptable(user, CID, None)  # noqa: SLF001
    manager._minted = list(user.refresh_tokens)  # noqa: SLF001
    assert manager._adoptable(user, CID, None)  # noqa: SLF001
