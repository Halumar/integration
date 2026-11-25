"""Renewed integration token after a simultaneous Core and hub restart.

The hub mints a new integration token on every start and announces it via
Supervisor discovery. When Core sets the entry up with the previous token
before it processed that discovery (host reboot, Core and hub restarting
together), the entry must take over the announced token without a reauth.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aioresponses import CallbackResult, aioresponses
from freezegun.api import FrozenDateTimeFactory

from custom_components.halumar.const import CONF_API_TOKEN, DOMAIN, UPDATE_INTERVAL
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from ._ha import MockConfigEntry, async_fire_time_changed
from .common import STATUS_URL, TEST_HOST, TEST_PORT, TEST_TOKEN, make_status_payload

RENEWED = "renewed-token"


def _message(
    host: str = TEST_HOST,
    port: Any = TEST_PORT,
    token: Any = RENEWED,
    service: str = DOMAIN,
) -> SimpleNamespace:
    """A Supervisor discovery message as aiohasupervisor returns it."""
    return SimpleNamespace(
        service=service,
        config={"host": host, "port": port, "api_token": token, "version": "x"},
    )


@pytest.fixture
def supervisor(hass: HomeAssistant) -> Generator[AsyncMock, None, None]:
    """Pretend to run under the Supervisor; returns the discovery list mock."""
    hass.config.components.add("hassio")
    with (
        patch("custom_components.halumar.token_renewal.is_hassio", return_value=True),
        patch(
            "custom_components.halumar.token_renewal._async_supervisor_discovery",
            new_callable=AsyncMock,
            return_value=[_message()],
        ) as listing,
    ):
        yield listing


def _accept_only(mock_api: aioresponses, token: str, seen: list[str]) -> None:
    """Status endpoint that only accepts ``token`` (401 otherwise)."""

    def _status(url: Any, **kwargs: Any) -> CallbackResult:
        sent = kwargs["headers"]["Authorization"].removeprefix("Bearer ")
        seen.append(sent)
        if sent != token:
            return CallbackResult(status=401)
        return CallbackResult(status=200, payload=make_status_payload())

    mock_api.get(STATUS_URL, callback=_status, repeat=True)


def _reauth_flows(hass: HomeAssistant) -> list[Any]:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"]["source"] == config_entries.SOURCE_REAUTH
    ]


async def test_setup_takes_over_token_announced_to_supervisor(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    supervisor: AsyncMock,
) -> None:
    """Old token rejected at setup → announced token used, no reauth."""
    seen: list[str] = []
    _accept_only(mock_api, RENEWED, seen)

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.data[CONF_API_TOKEN] == RENEWED
    assert seen[:2] == [TEST_TOKEN, RENEWED]
    assert _reauth_flows(hass) == []
    supervisor.assert_awaited_once()


async def test_poll_takes_over_token_announced_to_supervisor(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    supervisor: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A loaded entry switches to the announced token in place."""
    seen: list[str] = []
    _accept_only(mock_api, TEST_TOKEN, seen)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED

    # The hub restarted and renewed its token.
    mock_api.clear()
    _accept_only(mock_api, RENEWED, seen)
    freezer.tick(timedelta(seconds=UPDATE_INTERVAL + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    coordinator = config_entry.runtime_data
    assert coordinator.last_update_success is True
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.data[CONF_API_TOKEN] == RENEWED
    assert (await coordinator.async_request_headers())["Authorization"] == (
        f"Bearer {RENEWED}"
    )
    assert _reauth_flows(hass) == []


@pytest.mark.parametrize(
    "message",
    [
        _message(host="other-host"),
        _message(port=9999),
        _message(port="not-a-port"),
        _message(token=""),
        _message(token=None),
        _message(service="other"),
    ],
    ids=[
        "other-host",
        "other-port",
        "bad-port",
        "empty-token",
        "no-token",
        "other-service",
    ],
)
async def test_unusable_announcement_falls_back_to_reauth(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    supervisor: AsyncMock,
    message: SimpleNamespace,
) -> None:
    """Tokens for another endpoint are never used; reauth stays the fallback."""
    supervisor.return_value = [message]
    seen: list[str] = []
    _accept_only(mock_api, RENEWED, seen)

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert config_entry.data[CONF_API_TOKEN] == TEST_TOKEN
    assert set(seen) == {TEST_TOKEN}
    assert len(_reauth_flows(hass)) == 1


async def test_announced_token_refused_retries_without_reauth(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    supervisor: AsyncMock,
) -> None:
    """The hub refusing the token it announced is switching: retry later."""
    seen: list[str] = []
    _accept_only(mock_api, "something-else", seen)

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert "switching to a new integration token" in str(config_entry.reason)
    assert seen == [TEST_TOKEN, RENEWED]
    assert _reauth_flows(hass) == []


async def test_stored_token_announced_but_refused_retries(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    supervisor: AsyncMock,
) -> None:
    """Entry already holds the announced token, the hub refuses it: retry."""
    supervisor.return_value = [_message(token=TEST_TOKEN)]
    mock_api.get(STATUS_URL, status=401, repeat=True)

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert _reauth_flows(hass) == []


async def test_renewed_unannounced_token_refused_starts_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """One retry only: a refused token nobody announced ends in reauth."""
    seen: list[str] = []

    def _status(url: Any, **kwargs: Any) -> CallbackResult:
        seen.append(kwargs["headers"]["Authorization"].removeprefix("Bearer "))
        hass.config_entries.async_update_entry(
            config_entry, data={**config_entry.data, CONF_API_TOKEN: RENEWED}
        )
        return CallbackResult(status=401)

    mock_api.get(STATUS_URL, callback=_status, repeat=True)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert seen == [TEST_TOKEN, RENEWED]
    assert len(_reauth_flows(hass)) == 1


async def test_supervisor_unreadable_starts_reauth(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    supervisor: AsyncMock,
) -> None:
    """A failing Supervisor lookup is not an error of its own."""
    supervisor.side_effect = RuntimeError("supervisor busy")
    mock_api.get(STATUS_URL, status=401, repeat=True)

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert len(_reauth_flows(hass)) == 1


async def test_no_supervisor_lookup_without_supervisor(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Outside the Supervisor no lookup happens."""
    mock_api.get(STATUS_URL, status=401, repeat=True)
    with patch(
        "custom_components.halumar.token_renewal._async_supervisor_discovery",
        new_callable=AsyncMock,
    ) as listing:
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    listing.assert_not_awaited()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR


async def test_token_rediscovered_during_setup_is_used(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """A re-discovery that lands while the setup runs is picked up at once."""
    seen: list[str] = []

    def _status(url: Any, **kwargs: Any) -> CallbackResult:
        sent = kwargs["headers"]["Authorization"].removeprefix("Bearer ")
        seen.append(sent)
        if sent == TEST_TOKEN:
            # HA applied the re-discovery to the entry meanwhile, without a
            # reload (the entry was still being set up).
            hass.config_entries.async_update_entry(
                config_entry, data={**config_entry.data, CONF_API_TOKEN: RENEWED}
            )
            return CallbackResult(status=401)
        return CallbackResult(status=200, payload=make_status_payload())

    mock_api.get(STATUS_URL, callback=_status, repeat=True)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert seen[:2] == [TEST_TOKEN, RENEWED]
    assert _reauth_flows(hass) == []


async def test_rediscovery_reloads_entry_after_failed_setup(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """An entry stuck in setup_error is reloaded by the re-discovery."""
    seen: list[str] = []
    _accept_only(mock_api, RENEWED, seen)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    assert len(_reauth_flows(hass)) == 1

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_HASSIO},
        data=HassioServiceInfo(
            config={
                "host": TEST_HOST,
                "port": TEST_PORT,
                "api_token": RENEWED,
                "version": "x",
            },
            name="Hub",
            slug="halumar",
            uuid="abcd1234",
        ),
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.data[CONF_API_TOKEN] == RENEWED
    # The reload ended the pending reauth flow.
    assert _reauth_flows(hass) == []


async def test_rediscovery_reloads_failed_entry_with_unchanged_data(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Same announced data as stored (applied earlier) still heals the entry."""
    hass.config_entries.async_update_entry(
        config_entry, data={**config_entry.data, CONF_API_TOKEN: RENEWED}
    )
    mock_api.get(STATUS_URL, status=401)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR

    mock_api.get(STATUS_URL, status=200, payload=make_status_payload(), repeat=True)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_HASSIO},
        data=HassioServiceInfo(
            config={
                "host": TEST_HOST,
                "port": TEST_PORT,
                "api_token": RENEWED,
                "version": "x",
            },
            name="Hub",
            slug="halumar",
            uuid="abcd1234",
        ),
    )
    await hass.async_block_till_done()

    assert result["reason"] == "already_configured"
    assert config_entry.state is ConfigEntryState.LOADED


async def test_supervisor_discovery_reads_hassio_client(hass: HomeAssistant) -> None:
    """The lookup asks the hassio integration's Supervisor client."""
    from custom_components.halumar import token_renewal

    listing = AsyncMock(return_value=(_message(),))
    client = SimpleNamespace(discovery=SimpleNamespace(list=listing))
    fake_hassio = SimpleNamespace(get_supervisor_client=lambda _hass: client)
    with patch.dict("sys.modules", {"homeassistant.components.hassio": fake_hassio}):
        messages = await token_renewal._async_supervisor_discovery(hass)

    assert [m.config["api_token"] for m in messages] == [RENEWED]


async def test_entry_without_numeric_port_takes_nothing_over(
    hass: HomeAssistant, supervisor: AsyncMock
) -> None:
    """A malformed entry never matches an announcement."""
    from custom_components.halumar.token_renewal import async_token_after_rejection

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": TEST_HOST, "port": "8099", CONF_API_TOKEN: TEST_TOKEN},
    )
    entry.add_to_hass(hass)

    assert await async_token_after_rejection(hass, entry, TEST_TOKEN) == (None, None)
    supervisor.assert_not_awaited()


async def test_poll_refused_announced_token_keeps_entry_loaded(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    supervisor: AsyncMock,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A loaded entry waits for the switch instead of starting reauth."""
    seen: list[str] = []
    _accept_only(mock_api, TEST_TOKEN, seen)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    mock_api.clear()
    _accept_only(mock_api, "something-else", seen)
    freezer.tick(timedelta(seconds=UPDATE_INTERVAL + 1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    coordinator = config_entry.runtime_data
    assert coordinator.last_update_success is False
    assert config_entry.state is ConfigEntryState.LOADED
    assert _reauth_flows(hass) == []
