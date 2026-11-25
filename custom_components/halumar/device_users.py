"""One Home Assistant user per paired device.

Every paired device gets its own HA user with exactly one long-lived access
token (LLAT): device, user and token stand 1:1:1. The hub asks for it
(``provision_client`` over the WS push session, repeated in the status
snapshot's ``provisioning`` list), this module creates the user and token in
HA and delivers the token to the hub via ``POST /api/integration/client-token``.
The hub hands it to the app exactly like today (``client_config``).

Guarantees:

* A device user is never an admin: before every token the user is checked
  (not owner, not admin, not system-generated, no credentials, exactly the
  group the control path asks for (``system-read-only`` for "checked", else
  ``system-users``), active, and local_only without remote access). No user is
  created while HA has no owner yet (HA would make it the owner).
* ``local_only`` = not ``remote_allowed`` (the hub's effective profile rule
  remote_access: guests too may have it); a guest's token lifetime is
  bounded to the guest expiry plus a grace period.
  Taking remote access away rotates the token (open sessions from outside
  end); granting it keeps the token, so a device on the road is not locked
  out.
* Handling is idempotent: ``token_action=ensure`` reuses the device's token
  and derives a fresh access token from it (same refresh-token id), only
  ``rotate`` mints a new one. A superseded token is removed only after the
  hub acknowledged the new one.
* The reconcile deletes only device users this module owns (recorded in its
  store, or marked by a token client_name ``<domain>:<client_id>``) and never
  owners, admins, system users or users with credentials. It deletes nothing
  when the client list was truncated, nor users created after the snapshot
  was requested, nor users of deleted devices the hub still holds
  (``ha_user_holds``: until the app confirmed its reset or the hub's grace
  period ran out, then the hub sends ``deprovision_client``).
* Only ids are persisted (``Store``); the bearer lives in memory only.
* This is the only way the hub gets HA tokens: there is no shared owner
  token and no other managed user.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import secrets
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.auth.const import GROUP_ID_READ_ONLY, GROUP_ID_USER
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN, User
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .brand import BRAND_NAME
from .const import (
    DEVICE_TOKEN_CLIENT_NAME_PREFIX,
    DEVICE_USER_GUEST_LABEL,
    DEVICE_USER_GUEST_LABEL_ALIASES,
    DEVICE_USER_NAME_SEPARATOR,
    DOMAIN,
)
from .coordinator import HubApiError
from .enforcement import DATA_ENFORCEMENT
from .entity import owner_device_label

if TYPE_CHECKING:
    from homeassistant.auth.models import RefreshToken

    from .coordinator import HubConfigEntry, HubCoordinator

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

# Lifetime of a regular device token; rotation and deprovisioning are the
# hygiene mechanisms, not expiry.
DEVICE_TOKEN_EXPIRATION = timedelta(days=3650)

# Guests: token lifetime = time to expiry (rounded up to days) + grace. The
# hub removes the guest precisely at expiry (deprovision); this is the
# backstop should that never happen.
GUEST_TOKEN_GRACE = timedelta(days=7)

# A guest token still covers a (new) expiry when it outlives it by this much.
GUEST_TOKEN_COVER_MARGIN = timedelta(days=1)
# Bound of the remembered minted token ids (one live token per device).
MAX_MINTED_IDS = 1000

# client-token answers that mean the device no longer exists on the hub.
_GONE_STATUSES = frozenset({404, 409, 410})

DATA_UNLOADED = f"{DOMAIN}_device_users_unloaded"

# Repair issue: a device user carried tokens this integration never gives it.
ISSUE_FOREIGN_TOKENS = "device_user_foreign_tokens"

MSG_PROVISION = "provision_client"
MSG_UPDATE_IDENTITY = "update_client_identity"
MSG_DEPROVISION = "deprovision_client"
PUSH_MESSAGE_TYPES = frozenset({MSG_PROVISION, MSG_UPDATE_IDENTITY, MSG_DEPROVISION})

_CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9\-]{1,64}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_MAX_DISPLAY_NAME = 100
_MAX_OWNER_LABEL = 50


class DeviceUserSafetyError(Exception):
    """A device user does not meet the safety rules; no token is issued."""


class DeviceIdentity:
    """Validated identity of one device (contracts common/client-identity)."""

    __slots__ = (
        "client_id",
        "display_name",
        "guest_expires_at",
        "is_guest",
        "owner_label",
        "remote_allowed",
    )

    def __init__(
        self,
        client_id: str,
        display_name: str,
        owner_label: str | None,
        is_guest: bool,
        remote_allowed: bool,
        guest_expires_at: datetime | None,
    ) -> None:
        """Store the already validated fields."""
        self.client_id = client_id
        self.display_name = display_name
        self.owner_label = owner_label
        self.is_guest = is_guest
        self.remote_allowed = remote_allowed
        self.guest_expires_at = guest_expires_at

    @property
    def local_only(self) -> bool:
        """HA local_only flag: not the device's remote access (guests too)."""
        return not self.remote_allowed

    @property
    def user_name(self) -> str:
        """HA user name: '<brand> · [<guest label> ][<owner> ]<device>'."""
        return self._user_name(DEVICE_USER_GUEST_LABEL)

    @property
    def accepted_user_names(self) -> frozenset[str]:
        """User names that identify this device's user (any guest label)."""
        if not self.is_guest:
            return frozenset({self.user_name})
        return frozenset(
            self._user_name(label)
            for label in (DEVICE_USER_GUEST_LABEL, *DEVICE_USER_GUEST_LABEL_ALIASES)
        )

    def _user_name(self, guest_label: str) -> str:
        parts = []
        if self.is_guest:
            parts.append(guest_label)
        parts.append(owner_device_label(self.display_name, self.owner_label))
        return f"{BRAND_NAME}{DEVICE_USER_NAME_SEPARATOR}{' '.join(parts)}"

    def token_expiration(self) -> timedelta:
        """Access-token lifetime of a token minted now for this device."""
        if not self.is_guest or self.guest_expires_at is None:
            return DEVICE_TOKEN_EXPIRATION
        remaining = (self.guest_expires_at - dt_util.utcnow()).total_seconds()
        days = max(1, math.ceil(remaining / 86400))
        return timedelta(days=days) + GUEST_TOKEN_GRACE

    @classmethod
    def from_request(cls, data: Any) -> DeviceIdentity:
        """Build from a hub request (provision_client / update_client_identity)."""
        if not isinstance(data, dict):
            data = {}  # rejected below as an invalid client_id
        return cls._build(
            client_id=data.get("client_id"),
            display_name=data.get("display_name"),
            owner_label=data.get("owner_label"),
            is_guest=data.get("is_guest"),
            remote_allowed=data.get("remote_allowed", False),
            guest_expires_at=data.get("guest_expires_at"),
        )

    @classmethod
    def from_snapshot_client(cls, client: dict[str, Any]) -> DeviceIdentity:
        """Build from a clients[] entry of GET /api/integration/status."""
        return cls._build(
            client_id=client.get("id"),
            display_name=client.get("name"),
            owner_label=client.get("owner_label"),
            is_guest=client.get("is_guest", False),
            remote_allowed=client.get("remote_allowed", False),
            guest_expires_at=client.get("expires_at"),
        )

    @classmethod
    def _build(
        cls,
        *,
        client_id: Any,
        display_name: Any,
        owner_label: Any,
        is_guest: Any,
        remote_allowed: Any,
        guest_expires_at: Any,
    ) -> DeviceIdentity:
        if not isinstance(client_id, str) or not _CLIENT_ID_PATTERN.match(client_id):
            raise ValueError("invalid client_id")
        name = _clean_text(display_name, _MAX_DISPLAY_NAME)
        if not name:
            raise ValueError("display_name missing")
        owner = _clean_text(owner_label, _MAX_OWNER_LABEL) or None
        guest = is_guest is True
        expires: datetime | None = None
        if isinstance(guest_expires_at, str) and guest_expires_at:
            expires = dt_util.parse_datetime(guest_expires_at)
            if expires is not None and expires.tzinfo is None:
                expires = None  # naive timestamps are rejected, not guessed
        return cls(
            client_id=client_id,
            display_name=name,
            owner_label=owner,
            is_guest=guest,
            # Fail closed: anything but a literal true keeps the device local.
            remote_allowed=remote_allowed is True,
            guest_expires_at=expires,
        )


def _clean_text(value: Any, max_len: int) -> str:
    if not isinstance(value, str):
        return ""
    return _CONTROL_CHARS.sub("", value).strip()[:max_len]


def device_token_client_id(token: RefreshToken) -> str | None:
    """Return the client id a device-token client_name points to, if any."""
    if token.token_type != TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN:
        return None
    name = token.client_name or ""
    if not name.startswith(DEVICE_TOKEN_CLIENT_NAME_PREFIX):
        return None
    client_id = name[len(DEVICE_TOKEN_CLIENT_NAME_PREFIX) :].split("/", 1)[0]
    return client_id if _CLIENT_ID_PATTERN.match(client_id) else None


def _is_plain_user(user: User) -> bool:
    """Not owner, admin or system user, and no login credentials."""
    return not (
        user.is_owner or user.is_admin or user.system_generated or user.credentials
    )


def marked_device_client_id(user: User) -> str | None:
    """Return the client id of a device user recognised by its tokens.

    A user counts as a device user of this integration only when it is a
    plain user (see _is_plain_user), has at least one token, and EVERY token
    is a device LLAT for the same client id. Anything else is foreign.

    Token names are chosen by the token's owner (any HA user can mint an LLAT
    with any name), so this is only a hint for finding leftovers of a lost
    store, never the identity of a caller: for that see
    async_device_client_id.
    """
    if not _is_plain_user(user) or not user.refresh_tokens:
        return None
    client_ids = {device_token_client_id(t) for t in user.refresh_tokens.values()}
    if len(client_ids) != 1:
        return None
    return next(iter(client_ids))


DEVICE_USER_GROUPS = (GROUP_ID_USER, GROUP_ID_READ_ONLY)


async def async_device_client_id(hass: HomeAssistant, user: User) -> str | None:
    """Return the device a calling HA user stands for, if any.

    Only from the integration's own mapping (which user it created for
    which device), never from token names: those the user chooses itself.
    While the entry is not loaded the persisted mapping is read.
    """
    if not _is_plain_user(user):
        return None
    # Mapping read from disk per entry, kept while the entry stays unloaded
    # (nothing writes it then), so callers cannot make it be read per call.
    unloaded: dict[str, DeviceUserManager] = hass.data.setdefault(DATA_UNLOADED, {})
    for entry in hass.config_entries.async_entries(DOMAIN):
        manager: DeviceUserManager | None = None
        if entry.state is ConfigEntryState.LOADED:
            unloaded.pop(entry.entry_id, None)
            manager = entry.runtime_data.device_users
        if manager is None:
            manager = unloaded.get(entry.entry_id)
        if manager is None:
            manager = DeviceUserManager(hass, entry, None)
            await manager.async_setup()
            unloaded[entry.entry_id] = manager
        client_id = manager.client_id_of_user(user.id)
        if client_id is not None:
            return client_id
    return None


def assert_safe_device_user(
    user: User, identity: DeviceIdentity, group_id: str = GROUP_ID_USER
) -> None:
    """Raise DeviceUserSafetyError unless the user may hold a device token.

    ``group_id`` is the one group the device user must be in:
    system-users, or system-read-only for the control path "checked".
    """
    if group_id not in DEVICE_USER_GROUPS:
        raise DeviceUserSafetyError(f"group {group_id} is not a device user group")
    problems = []
    if user.is_owner:
        problems.append("owner")
    if user.is_admin:
        problems.append("admin")
    if user.system_generated:
        problems.append("system-generated")
    if user.credentials:
        problems.append("has login credentials")
    if [group.id for group in user.groups] != [group_id]:
        problems.append(f"groups are not exactly {group_id}")
    if not user.is_active:
        problems.append("deactivated")
    if identity.local_only and not user.local_only:
        problems.append("must be local only")
    if problems:
        raise DeviceUserSafetyError(
            f"HA user {user.id} for device {identity.client_id}: {', '.join(problems)}"
        )


class DeviceUserManager:
    """Owns the per-device HA users of one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: HubConfigEntry,
        coordinator: HubCoordinator | None,
    ) -> None:
        """Store collaborators; no side effects.

        ``coordinator`` is None only for async_remove_all on entry removal.
        """
        self.hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, storage_key(entry.entry_id)
        )
        # client_id -> {user_id, refresh_token_id, delivered_refresh_token_id,
        #               token_expires_at, created_at}; ids and times only.
        self._records: dict[str, dict[str, Any]] = {}
        # Refresh-token ids this integration minted: only those
        # count as device markers while the store exists.
        self._minted: list[str] = []
        self._locks: dict[str, asyncio.Lock] = {}
        self._reconcile_running = False
        self._reconcile_again = False

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Load the client -> user mapping."""
        if self._coordinator is not None:
            # The entry is being loaded: its mapping changes from now on.
            self.hass.data.get(DATA_UNLOADED, {}).pop(self._entry.entry_id, None)
        data = await self._store.async_load() or {}
        clients = data.get("clients")
        self._records = dict(clients) if isinstance(clients, dict) else {}
        minted = data.get("minted_token_ids")
        self._minted = (
            [m for m in minted if isinstance(m, str)]
            if isinstance(minted, list)
            else []
        )
        for record in self._records.values():
            for key in ("refresh_token_id", "delivered_refresh_token_id"):
                token_id = record.get(key)
                if isinstance(token_id, str) and token_id not in self._minted:
                    self._minted.append(token_id)

    def group_id_of(self, client_id: str) -> str:
        """HA group the device user of ``client_id`` must have."""
        manager = self.hass.data.get(DATA_ENFORCEMENT)
        if manager is None:
            return GROUP_ID_USER
        group_id: str = manager.desired_group_id(client_id)
        return group_id

    def user_id_of(self, client_id: str) -> str | None:
        """HA user id mapped to ``client_id`` (None if unknown)."""
        record = self._records.get(client_id)
        user_id = record.get("user_id") if record else None
        return user_id if isinstance(user_id, str) else None

    def client_id_of_user(self, user_id: str) -> str | None:
        """Device the HA user ``user_id`` was created for (None if none)."""
        client_ids = [
            client_id
            for client_id, record in self._records.items()
            if record.get("user_id") == user_id
        ]
        return client_ids[0] if len(client_ids) == 1 else None

    @property
    def records(self) -> dict[str, dict[str, Any]]:
        """Read-only view of the persisted mapping (for tests/diagnostics)."""
        return dict(self._records)

    # ── Entry points ─────────────────────────────────────────────────────

    @callback
    def async_schedule_push(self, message: dict[str, Any]) -> None:
        """Handle a push message in the background (never blocks the pump)."""
        self._entry.async_create_background_task(
            self.hass,
            self.async_handle_push(message),
            name=f"{DOMAIN}-device-user-{message.get('type')}",
        )

    @callback
    def async_schedule_reconcile(self) -> None:
        """Coordinator listener: reconcile after every successful poll."""
        if self._coordinator is None or not self._coordinator.last_update_success:
            return
        if self._reconcile_running:
            self._reconcile_again = True
            return
        self._entry.async_create_background_task(
            self.hass, self.async_reconcile(), name=f"{DOMAIN}-device-user-reconcile"
        )

    async def async_handle_push(self, message: dict[str, Any]) -> None:
        """Dispatch provision_client / update_client_identity / deprovision_client."""
        msg_type = message.get("type")
        data = message.get("data")
        try:
            if msg_type == MSG_PROVISION:
                await self.async_provision(data)
            elif msg_type == MSG_UPDATE_IDENTITY:
                await self.async_update_identity(DeviceIdentity.from_request(data))
            elif msg_type == MSG_DEPROVISION:
                client_id = data.get("client_id") if isinstance(data, dict) else None
                if isinstance(client_id, str):
                    await self.async_deprovision(client_id)
        except ValueError as err:
            _LOGGER.warning("Ignoring invalid %s message: %s", msg_type, err)

    async def async_provision(self, request: Any) -> bool:
        """Ensure user + token for one device and deliver the token.

        Returns True when the hub acknowledged the token.
        """
        identity = DeviceIdentity.from_request(request)
        rotate = request.get("token_action") == "rotate"
        request_id = request.get("request_id")
        async with self._lock(identity.client_id):
            return await self._async_ensure_and_deliver(
                identity,
                rotate=rotate,
                force_delivery=True,
                request_id=request_id if isinstance(request_id, str) else None,
            )

    async def async_update_identity(self, identity: DeviceIdentity) -> None:
        """Apply name / remote access / guest expiry to an existing device user."""
        async with self._lock(identity.client_id):
            record = self._records.get(identity.client_id)
            if record is None:
                return  # not provisioned yet; provision_client handles it
            user = await self.hass.auth.async_get_user(record["user_id"])
            if user is None:
                _LOGGER.warning(
                    "Home Assistant user of device %s was removed outside the "
                    "integration; recreating it",
                    identity.client_id,
                )
            await self._async_ensure_and_deliver(
                identity, rotate=False, force_delivery=False
            )

    async def async_deprovision(self, client_id: str) -> None:
        """Remove the device's HA user (its token and sessions go with it)."""
        async with self._lock(client_id):
            await self._async_remove_device_user(client_id)

    async def async_reconcile(self) -> None:
        """Bring HA in line with the latest status snapshot.

        Handles provisioning[] (fallback for missed pushes), applies the
        identity of every known device, re-delivers undelivered tokens and
        deletes device users whose device is no longer active.
        """
        if self._reconcile_running:
            self._reconcile_again = True
            return
        self._reconcile_running = True
        try:
            while True:
                self._reconcile_again = False
                await self._async_reconcile_once()
                if not self._reconcile_again:
                    break
        finally:
            self._reconcile_running = False

    async def async_remove_all(self) -> None:
        """Remove every device user and the mapping (config entry removed)."""
        await self.async_setup()
        for client_id in list(self._records):
            await self._async_remove_device_user(client_id)
        for user in await self.hass.auth.async_get_users():
            marked = marked_device_client_id(user)
            if marked is not None:
                await self._async_remove_user(user, marked)
        await self._store.async_remove()

    # ── Core ─────────────────────────────────────────────────────────────

    def _lock(self, client_id: str) -> asyncio.Lock:
        return self._locks.setdefault(client_id, asyncio.Lock())

    async def _async_save(self) -> None:
        await self._store.async_save(
            {"clients": self._records, "minted_token_ids": self._minted}
        )

    def _remember_minted(self, token_id: str) -> None:
        if token_id not in self._minted:
            self._minted.append(token_id)
            del self._minted[:-MAX_MINTED_IDS]

    async def _async_owner_exists(self) -> bool:
        return any(
            user.is_owner and not user.system_generated
            for user in await self.hass.auth.async_get_users()
        )

    def _adoptable(
        self,
        user: User,
        client_id: str,
        identity: DeviceIdentity | None,
        *,
        for_removal: bool = False,
    ) -> bool:
        """Whether a marked user may be taken over for ``client_id``.

        Token names are chosen by the token's owner: any device user can mint
        an LLAT named after another device. So a marked user is adopted only
        if it is not the mapped user of another device and, while the store
        exists, carries a token this integration minted itself; with a lost
        store its name must be exactly the device's expected user name.
        """
        if any(
            record.get("user_id") == user.id
            for other, record in self._records.items()
            if other != client_id
        ):
            return False
        if for_removal:
            # Removing a stray marked user of a gone device harms nobody else.
            return True
        if self._minted:
            return any(token_id in self._minted for token_id in user.refresh_tokens)
        return identity is not None and user.name in identity.accepted_user_names

    async def _async_find_marked_user(
        self,
        client_id: str,
        identity: DeviceIdentity | None = None,
        *,
        for_removal: bool = False,
    ) -> User | None:
        candidates = [
            user
            for user in await self.hass.auth.async_get_users()
            if marked_device_client_id(user) == client_id
            and self._adoptable(user, client_id, identity, for_removal=for_removal)
        ]
        if len(candidates) > 1:
            _LOGGER.warning(
                "Several Home Assistant users claim device %s; adopting none (a "
                "new user is created, check Settings > People > Users)",
                client_id,
            )
            return None
        return candidates[0] if candidates else None

    async def _async_ensure_user(self, identity: DeviceIdentity) -> tuple[User, bool]:
        """Return (user, became_local_only); create or adopt the user if needed.

        ``became_local_only``: remote access was taken away. HA checks
        ``local_only`` only when a session signs in, so the token is rotated
        then: open sessions from outside end at once. Granting remote access
        keeps the token: it works from outside immediately, also
        for a device that is away and could not fetch a new token.
        """
        record = self._records.get(identity.client_id)
        user: User | None = None
        if record is not None:
            user = await self.hass.auth.async_get_user(record["user_id"])
        if user is None:
            # Store lost or never written: adopt a marked user of this device.
            user = await self._async_find_marked_user(identity.client_id, identity)
        if user is None:
            if not await self._async_owner_exists():
                raise DeviceUserSafetyError(
                    "Home Assistant has no owner yet; refusing to create a device "
                    "user (it would become the owner)"
                )
            user = await self.hass.auth.async_create_user(
                identity.user_name,
                group_ids=[self.group_id_of(identity.client_id)],
                local_only=identity.local_only,
            )
            _LOGGER.info(
                "Created HA user %s for device %s", user.id, identity.client_id
            )
            if not _is_plain_user(user):
                # Defensive: never keep an unexpected privileged account.
                if not user.is_owner:
                    await self.hass.auth.async_remove_user(user)
                raise DeviceUserSafetyError(
                    f"newly created HA user {user.id} is privileged"
                )
            return user, False

        if not _is_plain_user(user):
            raise DeviceUserSafetyError(
                f"HA user {user.id} of device {identity.client_id} is privileged "
                "or has login credentials"
            )
        local_only_changed = bool(user.local_only) != identity.local_only
        became_local_only = local_only_changed and identity.local_only
        manager = self.hass.data.get(DATA_ENFORCEMENT)
        if manager is not None:
            # The enforcement sets the group and reports a group changed outside
            # the integration.
            await manager.async_ensure_group(identity.client_id, user)
        elif [group.id for group in user.groups] != [GROUP_ID_USER]:
            await self.hass.auth.async_update_user(user, group_ids=[GROUP_ID_USER])
        if user.name != identity.user_name or local_only_changed:
            await self.hass.auth.async_update_user(
                user, name=identity.user_name, local_only=identity.local_only
            )
            _LOGGER.info(
                "Updated HA user %s of device %s (local_only=%s)",
                user.id,
                identity.client_id,
                identity.local_only,
            )
        return user, became_local_only

    def _device_tokens(self, user: User, client_id: str) -> list[RefreshToken]:
        return [
            token
            for token in user.refresh_tokens.values()
            if device_token_client_id(token) == client_id
        ]

    def _covers_guest_expiry(
        self, record: dict[str, Any] | None, identity: DeviceIdentity
    ) -> bool:
        if not identity.is_guest or identity.guest_expires_at is None:
            return True
        token_expires = dt_util.parse_datetime(
            (record or {}).get("token_expires_at") or ""
        )
        if token_expires is None:
            return False
        return token_expires >= identity.guest_expires_at + GUEST_TOKEN_COVER_MARGIN

    async def _async_ensure_and_deliver(
        self,
        identity: DeviceIdentity,
        *,
        rotate: bool,
        force_delivery: bool,
        request_id: str | None = None,
    ) -> bool:
        """Create/update user and token, then deliver (caller holds the lock).

        ``force_delivery`` is set for explicit hub requests: the hub asks
        because it lacks the token, so it is re-delivered even if this side
        believes it was acknowledged before. The identity sync of the poll
        only delivers when something changed.
        """
        client_id = identity.client_id
        group_id = self.group_id_of(client_id)
        try:
            user, became_local_only = await self._async_ensure_user(identity)
            assert_safe_device_user(user, identity, group_id)
        except DeviceUserSafetyError as err:
            _LOGGER.error("No token for device %s: %s", client_id, err)
            return False

        record = self._records.get(client_id)
        if record is not None and record.get("user_id") != user.id:
            record = None
        self._remove_foreign_tokens(user, identity)
        tokens = self._device_tokens(user, client_id)
        current = next(
            (t for t in tokens if record and t.id == record.get("refresh_token_id")),
            None,
        )
        if current is None and tokens:
            current = max(tokens, key=lambda t: t.created_at)

        need_new = (
            rotate
            or current is None
            or became_local_only
            or not self._covers_guest_expiry(record, identity)
        )
        if (
            not need_new
            and not force_delivery
            and record is not None
            and current is not None
            and record.get("delivered_refresh_token_id") == current.id
        ):
            # Identity sync: nothing changed and the hub already has this token.
            return True

        if need_new:
            current = await self.hass.auth.async_create_refresh_token(
                user,
                client_name=self._new_client_name(user, client_id),
                token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
                access_token_expiration=identity.token_expiration(),
            )
            self._remember_minted(current.id)
            _LOGGER.info("Minted token %s for device %s", current.id, client_id)

        assert current is not None
        # Last check right before a bearer exists ("never admin").
        try:
            assert_safe_device_user(user, identity, self.group_id_of(client_id))
        except DeviceUserSafetyError as err:
            _LOGGER.error("No token for device %s: %s", client_id, err)
            return False
        bearer = self.hass.auth.async_create_access_token(current)
        self._records[client_id] = {
            "user_id": user.id,
            "refresh_token_id": current.id,
            "delivered_refresh_token_id": (record or {}).get(
                "delivered_refresh_token_id"
            ),
            "token_expires_at": (
                dt_util.utcnow() + current.access_token_expiration
            ).isoformat(),
            "created_at": (record or {}).get("created_at")
            or dt_util.utcnow().isoformat(),
        }
        await self._async_save()
        return await self._async_deliver(client_id, user, current, bearer, request_id)

    def _remove_foreign_tokens(self, user: User, identity: DeviceIdentity) -> None:
        """Remove tokens of a device user that are not tokens of its device.

        The integration only ever gives a device user tokens named after its
        own device. Anything else the user minted itself (for example named
        after another device): removed at once, not only with the next
        delivery, and reported.
        """
        client_id = identity.client_id
        foreign = [
            token
            for token in user.refresh_tokens.values()
            if device_token_client_id(token) != client_id
        ]
        if not foreign:
            # A clean sync: the report of an earlier one is done.
            ir.async_delete_issue(
                self.hass, DOMAIN, f"{ISSUE_FOREIGN_TOKENS}_{client_id}"
            )
            return
        for token in foreign:
            self.hass.auth.async_remove_refresh_token(token)
        _LOGGER.warning(
            "HA user %s of device %s carried %d token(s) this integration did "
            "not create (%s); removed",
            user.id,
            client_id,
            len(foreign),
            ", ".join(sorted({str(token.client_name)[:80] for token in foreign})),
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_FOREIGN_TOKENS}_{client_id}",
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_FOREIGN_TOKENS,
            translation_placeholders={
                "device": owner_device_label(
                    identity.display_name, identity.owner_label
                )
            },
        )

    def _new_client_name(self, user: User, client_id: str) -> str:
        base = f"{DEVICE_TOKEN_CLIENT_NAME_PREFIX}{client_id}"
        taken = {t.client_name for t in user.refresh_tokens.values()}
        name = base
        while name in taken:
            name = f"{base}/{secrets.token_hex(3)}"
        return name

    async def _async_deliver(
        self,
        client_id: str,
        user: User,
        token: RefreshToken,
        bearer: str,
        request_id: str | None,
    ) -> bool:
        if self._coordinator is None:  # pragma: no cover - only on entry removal
            return False
        try:
            await self._coordinator.post_client_token(
                client_id, user.id, token.id, bearer, request_id
            )
        except HubApiError as err:
            if err.status in _GONE_STATUSES:
                _LOGGER.info(
                    "Hub no longer knows device %s (HTTP %s); removing its HA user",
                    client_id,
                    err.status,
                )
                await self._async_remove_device_user(client_id)
            else:
                _LOGGER.debug(
                    "Token delivery for %s failed, will retry: %s", client_id, err
                )
            return False
        except Exception as err:  # noqa: BLE001 - retried on the next poll
            _LOGGER.debug(
                "Token delivery for %s failed, will retry: %s", client_id, err
            )
            return False

        # Acknowledged: the hub holds this token now. Exactly one token stays.
        superseded = False
        for other in list(user.refresh_tokens.values()):
            if other.id != token.id:
                superseded = True
                self.hass.auth.async_remove_refresh_token(other)
                _LOGGER.debug(
                    "Removed superseded token %s of device %s", other.id, client_id
                )
        record = self._records.get(client_id)
        if record is not None and record.get("refresh_token_id") == token.id:
            record["delivered_refresh_token_id"] = token.id
            await self._async_save()
        if superseded:
            # An away device still holds the removed token: the hub now
            # reports need_token_update for it, which the compliance view
            # must serve soon, so the app stops signing in from outside
            # (HA IP ban).
            await self._coordinator.async_request_refresh()
        return True

    async def _async_remove_device_user(self, client_id: str) -> None:
        manager = self.hass.data.get(DATA_ENFORCEMENT)
        if manager is not None:
            await manager.async_forget(client_id)
        ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_FOREIGN_TOKENS}_{client_id}")
        record = self._records.pop(client_id, None)
        user = None
        if record is not None:
            user = await self.hass.auth.async_get_user(record["user_id"])
        if user is None:
            user = await self._async_find_marked_user(client_id, for_removal=True)
        if user is not None:
            await self._async_remove_user(user, client_id, record)
        if record is not None:
            await self._async_save()

    async def _async_remove_user(
        self, user: User, client_id: str, record: dict[str, Any] | None = None
    ) -> None:
        owned = (record is not None and record.get("user_id") == user.id) or (
            marked_device_client_id(user) == client_id
        )
        if not owned or not _is_plain_user(user):
            _LOGGER.warning(
                "Not removing HA user %s for device %s: not a device user",
                user.id,
                client_id,
            )
            return
        await self.hass.auth.async_remove_user(user)
        _LOGGER.info("Removed HA user %s of device %s", user.id, client_id)

    # ── Reconcile ────────────────────────────────────────────────────────

    async def _async_reconcile_once(self) -> None:
        if self._coordinator is None or not self._coordinator.last_update_success:
            return
        data = self._coordinator.data or {}
        started_at = self._coordinator.snapshot_started_at
        truncated = self._coordinator.clients_truncated
        active: dict[str, dict[str, Any]] = {
            client["id"]: client
            for client in data.get("clients") or []
            if isinstance(client, dict)
            and isinstance(client.get("id"), str)
            and client.get("is_active", True) is not False
            and not client.get("pending_deletion")
        }

        # Deleted devices whose user the hub still holds until the app
        # confirmed its reset or the grace period ran out: neither
        # active nor to be removed yet.
        holds = {
            client_id
            for client_id in data.get("ha_user_holds") or []
            if isinstance(client_id, str)
        }

        handled: set[str] = set()
        for item in data.get("provisioning") or []:
            if not isinstance(item, dict):
                continue
            try:
                await self.async_provision(item)
            except ValueError as err:
                _LOGGER.warning("Ignoring invalid provisioning entry: %s", err)
                continue
            handled.add(str(item.get("client_id")))

        for client_id in list(self._records):
            if client_id in handled or client_id not in active:
                continue
            try:
                identity = DeviceIdentity.from_snapshot_client(active[client_id])
            except ValueError:
                continue
            await self.async_update_identity(identity)

        if truncated or started_at is None:
            return
        for client_id, record in list(self._records.items()):
            if client_id in active or client_id in holds:
                continue
            created_at = dt_util.parse_datetime(record.get("created_at") or "")
            if created_at is not None and created_at >= started_at:
                continue  # created after the snapshot was requested
            async with self._lock(client_id):
                await self._async_remove_device_user(client_id)

        # Marked device users the mapping does not know (store lost) or that
        # duplicate the mapped user of their device.
        for user in await self.hass.auth.async_get_users():
            marked = marked_device_client_id(user)
            if marked is None or marked in holds:
                continue
            client_id = marked
            mapped = self._records.get(client_id)
            if mapped is not None and mapped.get("user_id") == user.id:
                continue
            if any(
                other.get("user_id") == user.id
                for other_id, other in self._records.items()
                if other_id != client_id
            ):
                # The mapped user of another device carries tokens named after
                # this one (self-minted): neither adopt nor remove;
                # the identity sync of its own device removes those tokens.
                _LOGGER.warning(
                    "HA user %s of another device carries tokens of device %s; ignored",
                    user.id,
                    client_id,
                )
                continue
            newest = max(t.created_at for t in user.refresh_tokens.values())
            if newest >= started_at:
                continue
            if client_id in active and mapped is None:
                adopt_as: DeviceIdentity | None
                try:
                    adopt_as = DeviceIdentity.from_snapshot_client(active[client_id])
                except ValueError:
                    adopt_as = None
                if not self._adoptable(user, client_id, adopt_as):
                    _LOGGER.warning(
                        "Not adopting HA user %s for device %s: not a user this "
                        "integration created for it",
                        user.id,
                        client_id,
                    )
                    continue
                async with self._lock(client_id):
                    self._records[client_id] = {
                        "user_id": user.id,
                        "refresh_token_id": None,
                        "delivered_refresh_token_id": None,
                        "token_expires_at": None,
                        "created_at": newest.isoformat(),
                    }
                    await self._async_save()
                _LOGGER.info("Adopted HA user %s of device %s", user.id, client_id)
                continue
            async with self._lock(client_id):
                await self._async_remove_user(user, client_id)


def storage_key(entry_id: str) -> str:
    """Store key of the client -> user mapping of one config entry."""
    return f"{DOMAIN}.device_users.{entry_id}"
