"""Tests for the Halumar coordinator: auth, resilience, issues, cleanup."""

from __future__ import annotations

from datetime import timedelta

import aiohttp
import pytest
from aioresponses import aioresponses
from freezegun.api import FrozenDateTimeFactory

from custom_components.halumar.brand import BRAND_NAME
from custom_components.halumar.const import DOMAIN, UPDATE_INTERVAL
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)

from ._ha import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)
from .common import (
    CLIENT_ID,
    GUEST_ID,
    STATUS_URL,
    make_status_payload,
    setup_integration,
)


async def _tick_one_poll(hass: HomeAssistant, freezer: FrozenDateTimeFactory) -> None:
    """Advance time past the poll interval and run the scheduled refresh."""
    freezer.tick(timedelta(seconds=UPDATE_INTERVAL + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


# ── Authentication failures → reauth ─────────────────────────────────────


async def test_first_refresh_auth_failure_starts_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """A 401 on the first refresh puts the entry in error and starts reauth."""
    mock_api.get(STATUS_URL, status=401)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == config_entries.SOURCE_REAUTH
    assert flows[0]["step_id"] == "reauth_confirm"


async def test_polling_auth_failure_starts_reauth(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A 401 on a later poll raises ConfigEntryAuthFailed and starts reauth."""
    await setup_integration(hass, mock_api, config_entry, repeat=False)
    assert config_entry.state is ConfigEntryState.LOADED
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    mock_api.get(STATUS_URL, status=401)
    await _tick_one_poll(hass, freezer)

    coordinator = config_entry.runtime_data
    assert coordinator.last_update_success is False
    assert isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == config_entries.SOURCE_REAUTH


# ── Connection failures → retry, unavailable, no repair issue ───────────


def _halumar_issues(hass: HomeAssistant) -> list[str]:
    """Ids of the repair issues of this integration."""
    return [
        issue_id for (domain, issue_id) in ir.async_get(hass).issues if domain == DOMAIN
    ]


async def test_connection_error_sets_retry_without_issue(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """A refused connection leaves the entry retrying; no repair issue.

    Repair issues must be actionable (Gold rule repair-issues): an outage is
    reported by the entry state and unavailable entities instead.
    """
    mock_api.get(STATUS_URL, exception=aiohttp.ClientConnectionError("refused"))
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert f"Cannot connect to the {BRAND_NAME} Hub" in str(config_entry.reason)
    assert _halumar_issues(hass) == []


async def test_timeout_sets_retry_without_issue(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """A hung hub (timeout) behaves exactly like a refused connection."""
    mock_api.get(STATUS_URL, exception=TimeoutError())
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert "TimeoutError" in str(config_entry.reason)
    assert _halumar_issues(hass) == []


async def test_outage_makes_entities_unavailable_and_recovers(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A failed poll turns the entities unavailable; the next success restores them."""
    await setup_integration(hass, mock_api, config_entry, repeat=False)
    entity_id = "sensor.halumar_hub_version"
    assert hass.states.get(entity_id).state == "2026.12.0"

    mock_api.get(STATUS_URL, exception=aiohttp.ClientConnectionError("refused"))
    await _tick_one_poll(hass, freezer)
    assert hass.states.get(entity_id).state == "unavailable"
    assert _halumar_issues(hass) == []

    mock_api.get(STATUS_URL, status=200, payload=make_status_payload())
    await _tick_one_poll(hass, freezer)
    assert hass.states.get(entity_id).state == "2026.12.0"


async def test_http_error_and_bad_payload_fail_the_update(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Non-200, non-JSON and non-object answers fail the update cleanly."""
    await setup_integration(hass, mock_api, config_entry, repeat=False)
    coordinator = config_entry.runtime_data

    mock_api.get(STATUS_URL, status=500)
    await _tick_one_poll(hass, freezer)
    assert coordinator.last_update_success is False
    assert "HTTP 500" in str(coordinator.last_exception)

    mock_api.get(STATUS_URL, status=200, body="not json", content_type="text/plain")
    await _tick_one_poll(hass, freezer)
    assert "invalid answer" in str(coordinator.last_exception)

    mock_api.get(STATUS_URL, status=200, payload=["a", "list"])
    await _tick_one_poll(hass, freezer)
    assert "invalid answer: list" in str(coordinator.last_exception)


# ── Malformed payloads ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("clients", "junk"),
        ("clients", None),
        ("areas", {"not": "a list"}),
        ("guests", None),
        ("provisioning", None),
        ("ha_user_holds", "x"),
        ("hub", None),
    ],
)
async def test_malformed_collections_are_a_failed_poll(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    key: str,
    value: object,
) -> None:
    """A status answer without the contract's minimal shape is no snapshot."""
    payload = make_status_payload()
    payload[key] = value
    await setup_integration(hass, mock_api, config_entry, payload=payload)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


@pytest.mark.parametrize("payload", [{}, {"clients": None}, {"status": "ok"}])
async def test_shapeless_answer_removes_no_device_and_no_device_user(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    payload: dict[str, object],
) -> None:
    """HTTP 200 with an object that is no status keeps everything."""
    await setup_integration(hass, mock_api, config_entry)
    coordinator = config_entry.runtime_data
    dev_reg = dr.async_get(hass)
    before = dr.async_entries_for_config_entry(dev_reg, config_entry.entry_id)
    clients = dict(coordinator.client_by_id)
    assert len(before) > 1 and clients
    started = coordinator.snapshot_started_at

    mock_api.clear()
    mock_api.get(STATUS_URL, status=200, payload=payload, repeat=True)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    assert coordinator.client_by_id == clients
    assert coordinator.snapshot_started_at == started
    after = dr.async_entries_for_config_entry(dev_reg, config_entry.entry_id)
    assert {d.id for d in after} == {d.id for d in before}


@pytest.mark.parametrize(
    ("key", "items"),
    [
        ("clients", [None]),
        ("clients", [{}]),
        ("clients", ["x"]),
        ("clients", [{"name": "no id"}]),
        ("clients", [{"id": 7}]),
        ("areas", [{"name": "no id"}]),
        ("guests", ["garbage"]),
        ("provisioning", [{"display_name": "no client_id"}]),
        ("ha_user_holds", [None]),
        ("ha_user_holds", [{"client_id": "x"}]),
    ],
)
async def test_list_items_without_their_id_are_a_failed_poll(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    key: str,
    items: list[object],
) -> None:
    """An item that does not name its device or area would drop out silently
    and its device with it: the whole answer is no snapshot then."""
    await setup_integration(hass, mock_api, config_entry)
    coordinator = config_entry.runtime_data
    dev_reg = dr.async_get(hass)
    before = dr.async_entries_for_config_entry(dev_reg, config_entry.entry_id)
    clients = dict(coordinator.client_by_id)
    payload = make_status_payload()
    payload[key] = payload[key] + items

    mock_api.clear()
    mock_api.get(STATUS_URL, status=200, payload=payload, repeat=True)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    assert coordinator.client_by_id == clients
    after = dr.async_entries_for_config_entry(dev_reg, config_entry.entry_id)
    assert {d.id for d in after} == {d.id for d in before}


async def test_non_json_body_sets_retry(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """A non-JSON 200 response fails the refresh cleanly."""
    mock_api.get(STATUS_URL, status=200, body="<html>nope</html>")
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_non_dict_json_sets_retry(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """A JSON body that is not an object fails the refresh cleanly."""
    mock_api.get(STATUS_URL, status=200, payload=["a", "list"])
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


# ── Hub version gate ──────────────────────────────────────────────────


async def test_prerelease_addon_version_is_compatible(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """A pre-release hub (2026.12.0-dev) compares by its release part."""
    payload = make_status_payload()
    payload["hub"]["version"] = "2026.12.0-dev"
    await setup_integration(hass, mock_api, config_entry, payload=payload)
    assert _halumar_issues(hass) == []


async def test_addon_version_incompatible_issue(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A hub below MIN_ADDON_VERSION raises a repair issue; upgrading clears it."""
    payload = make_status_payload()
    payload["hub"]["version"] = "2026.7.2"
    await setup_integration(hass, mock_api, config_entry, payload=payload, repeat=False)

    issue_reg = ir.async_get(hass)
    issue_id = f"addon_version_incompatible_{config_entry.entry_id}"
    issue = issue_reg.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "addon_version_incompatible"
    assert issue.translation_placeholders == {
        "current_version": "2026.7.2",
        "min_version": "2026.12.0",
    }

    mock_api.get(STATUS_URL, status=200, payload=make_status_payload())
    await _tick_one_poll(hass, freezer)
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is None


async def test_addon_version_issue_removed_with_the_entry(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Removing the entry removes its repair issue."""
    payload = make_status_payload()
    del payload["hub"]["version"]
    await setup_integration(hass, mock_api, config_entry, payload=payload)
    assert _halumar_issues(hass) == [
        f"addon_version_incompatible_{config_entry.entry_id}"
    ]

    assert await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()
    assert _halumar_issues(hass) == []


# ── Events and stale device cleanup ──────────────────────────────────────


async def test_pairing_started_event_is_pin_free(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """halumar_pairing_started carries exactly {is_guest} - never a PIN."""
    started = async_capture_events(hass, "halumar_pairing_started")
    stopped = async_capture_events(hass, "halumar_pairing_stopped")
    await setup_integration(hass, mock_api, config_entry, repeat=False)

    payload = make_status_payload()
    payload.update(
        {"pairing_active": True, "pairing_remaining": 300, "pairing_is_guest": True}
    )
    mock_api.get(STATUS_URL, status=200, payload=payload)
    await _tick_one_poll(hass, freezer)

    assert len(started) == 1
    assert dict(started[0].data) == {"is_guest": True}

    mock_api.get(STATUS_URL, status=200, payload=make_status_payload())
    await _tick_one_poll(hass, freezer)
    assert len(stopped) == 1
    assert dict(stopped[0].data) == {}


async def test_stale_client_device_removed_and_guest_expired_event(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A vanished guest fires halumar_guest_expired and its device is removed."""
    await setup_integration(hass, mock_api, config_entry, repeat=False)
    dev_reg = dr.async_get(hass)
    guest_identifier = (DOMAIN, f"halumar_client_{GUEST_ID}")

    def _guest_device() -> dr.DeviceEntry | None:
        # Version-neutral lookup: async_get_device(identifiers=...) raises in
        # tests from HA 2026.11 (deprecated, removed in 2027.8).
        return next(
            (
                d
                for d in dr.async_entries_for_config_entry(
                    dev_reg, config_entry.entry_id
                )
                if guest_identifier in d.identifiers
            ),
            None,
        )

    assert _guest_device() is not None

    expired = async_capture_events(hass, "halumar_guest_expired")
    payload = make_status_payload()
    payload["clients"] = [c for c in payload["clients"] if c["id"] != GUEST_ID]
    payload["guests"] = []
    payload["guest_count"] = 0
    payload["client_count"] = 1
    mock_api.get(STATUS_URL, status=200, payload=payload)
    await _tick_one_poll(hass, freezer)

    assert len(expired) == 1
    assert expired[0].data["client_id"] == GUEST_ID
    assert _guest_device() is None


# ── Unload / shutdown ────────────────────────────────────────────────────


async def test_unload_entry_cleans_up(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Unloading shuts the coordinator down; the actions stay registered.

    Actions are registered in async_setup (Bronze rule action-setup): after
    unloading they still exist and answer with a clear error.
    """
    await setup_integration(hass, mock_api, config_entry)
    coordinator = config_entry.runtime_data
    assert hass.services.has_service(DOMAIN, "display_notification")

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.NOT_LOADED
    # The base-class shutdown ran (cancels the poll timer + debouncer)
    assert coordinator._shutdown_requested
    assert hass.services.has_service(DOMAIN, "display_notification")
    assert hass.services.has_service(DOMAIN, "force_refresh")
    with pytest.raises(ServiceValidationError, match="not set up or not loaded"):
        await hass.services.async_call(DOMAIN, "force_refresh", {}, blocking=True)


# ── Event baseline ──────────────────────────────────────────


async def test_first_snapshot_fires_no_events(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """HA start / reload: already connected clients and an open pairing window
    are the baseline, not news. Later changes still fire."""
    connected = async_capture_events(hass, "halumar_client_connected")
    started = async_capture_events(hass, "halumar_pairing_started")
    payload = make_status_payload()
    payload.update({"pairing_active": True, "pairing_remaining": 300})
    await setup_integration(hass, mock_api, config_entry, payload=payload, repeat=False)

    mock_api.get(STATUS_URL, status=200, payload=payload)
    await _tick_one_poll(hass, freezer)
    assert connected == []
    assert started == []

    # The guest connects for real: exactly one event.
    later = make_status_payload()
    later.update({"pairing_active": True, "pairing_remaining": 200})
    later["clients"][1]["is_connected"] = True
    mock_api.get(STATUS_URL, status=200, payload=later)
    await _tick_one_poll(hass, freezer)
    assert [e.data["client_id"] for e in connected] == [GUEST_ID]
    assert started == []


async def test_reload_takes_a_new_baseline(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """An entry reload does not report the connected tablet again."""
    connected = async_capture_events(hass, "halumar_client_connected")
    await setup_integration(hass, mock_api, config_entry)
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert connected == []


async def test_new_client_connected_after_baseline_fires(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A client that pairs and connects after setup is reported."""
    connected = async_capture_events(hass, "halumar_client_connected")
    await setup_integration(hass, mock_api, config_entry, repeat=False)
    payload = make_status_payload()
    new_client = {**payload["clients"][0], "id": "33333333-3333-3333-3333-333333333333"}
    payload["clients"].append(new_client)
    mock_api.get(STATUS_URL, status=200, payload=payload)
    await _tick_one_poll(hass, freezer)

    assert [e.data["client_id"] for e in connected] == [new_client["id"]]


# ── Orphaned entities ───────────────────────────────────────


def _area_entities(hass: HomeAssistant, entry: MockConfigEntry, area_id: str) -> list:
    ent_reg = er.async_get(hass)
    return [
        e.entity_id
        for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        if e.unique_id
        in (
            f"{entry.entry_id}_hub_area_{area_id}",
            f"{entry.entry_id}_hub_area_switch_{area_id}",
        )
    ]


async def test_deleted_area_entities_are_removed_and_come_back(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A deleted area loses its sensor and switch (no "unknown" leftovers);
    the hub's area counter stays. An area that comes back gets them again."""
    await setup_integration(hass, mock_api, config_entry, repeat=False)
    assert len(_area_entities(hass, config_entry, "area-office")) == 2

    payload = make_status_payload()
    payload["areas"] = [a for a in payload["areas"] if a["id"] != "area-office"]
    mock_api.get(STATUS_URL, status=200, payload=payload)
    await _tick_one_poll(hass, freezer)

    assert _area_entities(hass, config_entry, "area-office") == []
    assert len(_area_entities(hass, config_entry, "area-kitchen")) == 2
    assert hass.states.get("sensor.halumar_hub_areas") is not None
    assert not [
        s.entity_id
        for s in hass.states.async_all()
        if "office" in s.entity_id and s.state != "unavailable"
    ]

    mock_api.get(STATUS_URL, status=200, payload=make_status_payload())
    await _tick_one_poll(hass, freezer)
    assert len(_area_entities(hass, config_entry, "area-office")) == 2


async def test_deleted_client_entities_go_with_the_device(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """All entities of a deleted client leave the registry and the state machine."""
    await setup_integration(hass, mock_api, config_entry, repeat=False)
    ent_reg = er.async_get(hass)

    def _client_entities() -> list[str]:
        return [
            e.entity_id
            for e in er.async_entries_for_config_entry(ent_reg, config_entry.entry_id)
            if f"_client_{CLIENT_ID}_" in (e.unique_id or "")
        ]

    before = _client_entities()
    assert before
    payload = make_status_payload()
    payload["clients"] = [c for c in payload["clients"] if c["id"] != CLIENT_ID]
    mock_api.get(STATUS_URL, status=200, payload=payload)
    await _tick_one_poll(hass, freezer)

    assert _client_entities() == []
    assert all(hass.states.get(entity_id) is None for entity_id in before)


async def test_truncated_snapshot_removes_nothing(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    freezer: FrozenDateTimeFactory,
) -> None:
    """More than 500 areas: a missing id does not mean the area is gone."""
    await setup_integration(hass, mock_api, config_entry, repeat=False)
    payload = make_status_payload()
    filler = payload["areas"][0]
    payload["areas"] = [
        {**filler, "id": f"area-{i}", "name": f"A{i}"} for i in range(501)
    ]
    mock_api.get(STATUS_URL, status=200, payload=payload)
    await _tick_one_poll(hass, freezer)

    assert len(_area_entities(hass, config_entry, "area-office")) == 2


async def test_only_unknown_client_devices_can_be_removed(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """async_remove_config_entry_device: hub and listed clients stay."""
    from custom_components.halumar import async_remove_config_entry_device

    await setup_integration(hass, mock_api, config_entry)
    dev_reg = dr.async_get(hass)
    devices = {
        next(i[1] for i in d.identifiers if i[0] == DOMAIN): d
        for d in dr.async_entries_for_config_entry(dev_reg, config_entry.entry_id)
    }
    hub = next(d for key, d in devices.items() if not key.startswith("halumar_client_"))
    listed = devices[f"halumar_client_{CLIENT_ID}"]
    gone = dev_reg.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={(DOMAIN, "halumar_client_44444444-4444-4444-4444-444444444444")},
    )

    assert not await async_remove_config_entry_device(hass, config_entry, hub)
    assert not await async_remove_config_entry_device(hass, config_entry, listed)
    assert await async_remove_config_entry_device(hass, config_entry, gone)
