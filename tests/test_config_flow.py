"""Tests for the Halumar config, discovery, reauth, reconfigure and options flows."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from aioresponses import aioresponses
from yarl import URL

from custom_components.halumar.brand import BRAND_NAME
from custom_components.halumar.const import (
    CONF_API_TOKEN,
    CONF_BLUEPRINTS_INSTALLED,
    CONF_HUB_INSTANCE_ID,
    CONF_USE_SSL,
    DOMAIN,
    ENTRY_UNIQUE_ID,
)
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from ._ha import MockConfigEntry
from .common import (
    HUB_INSTANCE_ID,
    MANIFEST_VERSION,
    STATUS_URL,
    TEST_HOST,
    TEST_PORT,
    TEST_TOKEN,
    make_status_payload,
    setup_integration,
)

USER_INPUT = {
    CONF_HOST: TEST_HOST,
    CONF_PORT: TEST_PORT,
    CONF_API_TOKEN: TEST_TOKEN,
    CONF_USE_SSL: False,
}


def _hassio_discovery_info(
    host: str = "local-halumar",
    port: Any = 8099,
    api_token: str | None = "supervisor-token",
    version: str = "2026.7.4",
) -> HassioServiceInfo:
    """Build a Supervisor discovery payload as the hub announces it."""
    config: dict[str, Any] = {"host": host, "port": port, "version": version}
    if api_token is not None:
        config["api_token"] = api_token
    return HassioServiceInfo(
        config=config, name=BRAND_NAME, slug="halumar", uuid="abcd1234"
    )


# ── Manual setup (user step) ─────────────────────────────────────────────


async def test_user_flow_success(hass: HomeAssistant, mock_api: aioresponses) -> None:
    """A valid host/port/token creates the single Halumar entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert not result["errors"]

    mock_api.get(STATUS_URL, status=200, payload={"status": "ok"})
    with patch(
        "custom_components.halumar.async_setup_entry", return_value=True
    ) as mock_setup:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"{BRAND_NAME} ({TEST_HOST}:{TEST_PORT})"
    assert result["data"] == USER_INPUT
    entry = result["result"]
    assert entry.unique_id == ENTRY_UNIQUE_ID
    assert entry.options == {}
    assert len(mock_setup.mock_calls) == 1

    # Validation hit the status endpoint with bearer auth + version header
    request = mock_api.requests[("GET", URL(STATUS_URL))][0]
    headers = request.kwargs["headers"]
    assert headers["Authorization"] == f"Bearer {TEST_TOKEN}"
    assert headers["X-Hub-Integration-Version"] == MANIFEST_VERSION


@pytest.mark.parametrize(
    ("answer", "stored"),
    [
        ({"payload": make_status_payload()}, HUB_INSTANCE_ID),
        ({"body": "not json"}, None),
    ],
)
async def test_user_flow_stores_the_hub_id_it_saw(
    hass: HomeAssistant,
    mock_api: aioresponses,
    answer: dict[str, Any],
    stored: str | None,
) -> None:
    """The validated status names the hub: the entry keeps its id (else the first poll does)."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    mock_api.get(STATUS_URL, status=200, **answer)
    with patch("custom_components.halumar.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == ENTRY_UNIQUE_ID
    assert result["data"].get(CONF_HUB_INSTANCE_ID) == stored


async def test_user_flow_invalid_auth_recovers(
    hass: HomeAssistant, mock_api: aioresponses
) -> None:
    """A 401 shows invalid_auth; the flow recovers with a valid token."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    mock_api.get(STATUS_URL, status=401)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}

    mock_api.get(STATUS_URL, status=200, payload={"status": "ok"})
    with patch("custom_components.halumar.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize(
    "response_kwargs",
    [
        {"status": 500},
        {"status": 404},
        {"exception": aiohttp.ClientConnectionError("refused")},
        {"exception": TimeoutError()},
    ],
)
async def test_user_flow_cannot_connect(
    hass: HomeAssistant, mock_api: aioresponses, response_kwargs: dict[str, Any]
) -> None:
    """Non-200/401 answers and transport errors map to cannot_connect."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    mock_api.get(STATUS_URL, **response_kwargs)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}

    # Recovery: the Hub answers again and the flow finishes.
    mock_api.get(STATUS_URL, status=200, payload=make_status_payload())
    with patch("custom_components.halumar.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == ENTRY_UNIQUE_ID


async def test_user_flow_unexpected_error(
    hass: HomeAssistant, mock_api: aioresponses
) -> None:
    """An unexpected exception maps to the unknown error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    mock_api.get(STATUS_URL, exception=ValueError("boom"))
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}

    mock_api.get(STATUS_URL, status=200, payload=make_status_payload())
    with patch("custom_components.halumar.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == ENTRY_UNIQUE_ID


async def test_user_flow_single_instance_abort(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """A second user flow aborts while an entry exists."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# ── Supervisor discovery (hassio step) ───────────────────────────────────


@pytest.mark.parametrize("discovered_port", [9123, "9123"])
async def test_hassio_discovery_creates_entry(
    hass: HomeAssistant, mock_api: aioresponses, discovered_port: Any
) -> None:
    """Confirmed discovery creates the entry from the Supervisor payload.

    The port is stored as int even when the payload stringifies it.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_HASSIO},
        data=_hassio_discovery_info(host="local-halumar", port=discovered_port),
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_confirm"
    assert result["description_placeholders"] == {
        "addon": BRAND_NAME,
        "version": "2026.7.4",
    }

    mock_api.get(
        "http://local-halumar:9123/api/integration/status",
        status=200,
        payload={"status": "ok"},
    )
    with patch(
        "custom_components.halumar.async_setup_entry", return_value=True
    ) as mock_setup:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == BRAND_NAME
    assert result["data"] == {
        CONF_HOST: "local-halumar",
        CONF_PORT: 9123,
        CONF_API_TOKEN: "supervisor-token",
        CONF_USE_SSL: False,
    }
    assert result["result"].unique_id == ENTRY_UNIQUE_ID
    assert len(mock_setup.mock_calls) == 1


async def test_hassio_discovery_does_not_warn_about_cleartext(
    hass: HomeAssistant, mock_api: aioresponses, caplog: pytest.LogCaptureFixture
) -> None:
    """The discovered hub host lives on the internal Supervisor
    network; the cleartext warning is for hosts on the LAN only."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_HASSIO},
        data=_hassio_discovery_info(host="local-halumar"),
    )
    mock_api.get(
        "http://local-halumar:8099/api/integration/status",
        status=200,
        payload={"status": "ok"},
    )
    with patch("custom_components.halumar.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == ENTRY_UNIQUE_ID
    assert "without SSL" not in caplog.text


async def test_manual_lan_host_without_ssl_still_warns(
    hass: HomeAssistant, mock_api: aioresponses, caplog: pytest.LogCaptureFixture
) -> None:
    """A manually entered LAN host without SSL keeps the warning."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    mock_api.get(
        "http://192.0.2.20:8099/api/integration/status",
        status=200,
        payload={"status": "ok"},
    )
    with patch("custom_components.halumar.async_setup_entry", return_value=True):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_HOST: "192.0.2.20"}
        )
        await hass.async_block_till_done()

    assert "without SSL for remote host 192.0.2.20" in caplog.text


@pytest.mark.parametrize(
    "info",
    [
        _hassio_discovery_info(host=""),
        _hassio_discovery_info(api_token=None),
        _hassio_discovery_info(api_token=""),
        _hassio_discovery_info(port=None),
        _hassio_discovery_info(port="not-a-number"),
    ],
)
async def test_hassio_discovery_invalid_payload(
    hass: HomeAssistant, info: HassioServiceInfo
) -> None:
    """Incomplete or malformed discovery payloads abort cleanly."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_HASSIO}, data=info
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_discovery_info"


async def test_hassio_rediscovery_updates_entry_and_reloads(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Re-discovery rotates host/port/token in place and reloads the entry."""
    with (
        patch(
            "custom_components.halumar.async_setup_entry", return_value=True
        ) as mock_setup,
        patch(
            "custom_components.halumar.async_unload_entry", return_value=True
        ) as mock_unload,
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        assert config_entry.state is ConfigEntryState.LOADED

        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_HASSIO},
            data=_hassio_discovery_info(
                host="rotated-host", port=9100, api_token="rotated-token"
            ),
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert config_entry.data[CONF_HOST] == "rotated-host"
    assert config_entry.data[CONF_PORT] == 9100
    assert config_entry.data[CONF_API_TOKEN] == "rotated-token"
    assert config_entry.data[CONF_USE_SSL] is False
    # The loaded entry was reloaded so the coordinator picks up the rotation
    assert len(mock_unload.mock_calls) == 1
    assert len(mock_setup.mock_calls) == 2


# ── Reauthentication ─────────────────────────────────────────────────────


async def test_reauth_flow(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """Reauth rejects a bad replacement token, then applies a valid one."""
    with (
        patch(
            "custom_components.halumar.async_setup_entry", return_value=True
        ) as mock_setup,
        patch("custom_components.halumar.async_unload_entry", return_value=True),
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

        result = await config_entry.start_reauth_flow(hass)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"
        assert result["description_placeholders"]["host"] == TEST_HOST

        # A rejected replacement token keeps the form open
        mock_api.get(STATUS_URL, status=401)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_TOKEN: "still-bad"}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "invalid_auth"}
        assert config_entry.data[CONF_API_TOKEN] == TEST_TOKEN

        # An accepted token is written to the entry and the entry reloads
        mock_api.get(STATUS_URL, status=200, payload={"status": "ok"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_API_TOKEN: "rotated-token"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_API_TOKEN] == "rotated-token"
    assert len(mock_setup.mock_calls) == 2


# ── Options flow ─────────────────────────────────────────────────────────


async def test_options_flow_guest_button_all_areas(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """The only option lands in the entry options; no reload, data untouched."""
    await setup_integration(hass, mock_api, config_entry)
    coordinator = config_entry.runtime_data

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert [str(key) for key in result["data_schema"].schema] == [
        "guest_button_all_areas"
    ]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"guest_button_all_areas": True}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {"guest_button_all_areas": True}
    assert "guest_button_all_areas" not in config_entry.data
    # Read at button press: no reload needed
    assert config_entry.runtime_data is coordinator

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"guest_button_all_areas": False}
    )
    assert config_entry.options == {"guest_button_all_areas": False}


# ── Reconfigure flow ─────────────────────────────────────────────────────


async def _start_reconfigure(
    hass: HomeAssistant, entry: MockConfigEntry
) -> dict[str, Any]:
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    return result  # type: ignore[return-value]


async def test_reconfigure_updates_connection_and_keeps_token(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_api: aioresponses
) -> None:
    """New host/port are validated, stored and the entry reloads.

    An empty token keeps the stored one.
    """
    await setup_integration(hass, mock_api, config_entry)
    result = await _start_reconfigure(hass, config_entry)
    assert result["description_placeholders"] == {"host": TEST_HOST}

    mock_api.get(
        "http://198.51.100.5:9000/api/integration/status",
        status=200,
        payload=make_status_payload(),
        repeat=True,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_HOST: " 198.51.100.5 ", CONF_PORT: 9000, CONF_USE_SSL: False},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data == {
        CONF_HOST: "198.51.100.5",
        CONF_PORT: 9000,
        CONF_API_TOKEN: TEST_TOKEN,
        CONF_USE_SSL: False,
        CONF_HUB_INSTANCE_ID: HUB_INSTANCE_ID,
        CONF_BLUEPRINTS_INSTALLED: True,
    }
    assert config_entry.unique_id == ENTRY_UNIQUE_ID
    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.runtime_data.base_url == "http://198.51.100.5:9000"


@pytest.mark.parametrize(
    ("user_input", "mock", "errors"),
    [
        ({CONF_HOST: "bad host!", CONF_PORT: 9000}, None, {CONF_HOST: "invalid_host"}),
        (
            {CONF_HOST: "198.51.100.6", CONF_PORT: 70000},
            None,
            {CONF_PORT: "invalid_port"},
        ),
        (
            {CONF_HOST: "198.51.100.6", CONF_PORT: 9001, CONF_API_TOKEN: "bad"},
            {"status": 401},
            {"base": "invalid_auth"},
        ),
        (
            {CONF_HOST: "198.51.100.6", CONF_PORT: 9001},
            {"exception": aiohttp.ClientConnectionError("refused")},
            {"base": "cannot_connect"},
        ),
    ],
    ids=["host", "port", "auth", "connect"],
)
async def test_reconfigure_errors_and_recovery(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
    user_input: dict[str, Any],
    mock: dict[str, Any] | None,
    errors: dict[str, str],
) -> None:
    """Every error leaves the entry untouched; fixing the input finishes."""
    await setup_integration(hass, mock_api, config_entry)
    result = await _start_reconfigure(hass, config_entry)

    if mock is not None:
        mock_api.get("http://198.51.100.6:9001/api/integration/status", **mock)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors
    assert config_entry.data[CONF_HOST] == TEST_HOST
    assert config_entry.data[CONF_API_TOKEN] == TEST_TOKEN

    mock_api.get(
        "http://198.51.100.6:9001/api/integration/status",
        status=200,
        payload=make_status_payload(),
        repeat=True,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_HOST: "198.51.100.6",
            CONF_PORT: 9001,
            CONF_API_TOKEN: "new-token",
            CONF_USE_SSL: False,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_API_TOKEN] == "new-token"


# ── Setup by hand while the Supervisor holds an announcement ───────────────

ANNOUNCED = {
    "host": "local-halumar",
    "port": 8099,
    "api_token": "supervisor-token",
    "version": "2026.12.0",
}


def _announcement(config: dict[str, Any], service: str = DOMAIN) -> Any:
    """A Supervisor discovery message as aiohasupervisor returns it."""
    from types import SimpleNamespace

    return SimpleNamespace(service=service, config=dict(config))


async def _start_user_flow(
    hass: HomeAssistant, announcements: Any = (), *, supervisor: bool = True
) -> tuple[dict[str, Any], Any]:
    """Start the user step with the given Supervisor discovery list."""
    from unittest.mock import AsyncMock

    if supervisor:
        hass.config.components.add("hassio")
    listing = (
        announcements
        if isinstance(announcements, AsyncMock)
        else AsyncMock(return_value=list(announcements))
    )
    with (
        patch(
            "custom_components.halumar.token_renewal.is_hassio",
            return_value=supervisor,
        ),
        patch(
            "custom_components.halumar.token_renewal._async_supervisor_discovery",
            listing,
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
    return result, listing  # type: ignore[return-value]


async def test_user_step_continues_with_the_announced_hub(
    hass: HomeAssistant, mock_api: aioresponses
) -> None:
    """One announcement: the confirmation of the discovery, no token to enter."""
    result, _listing = await _start_user_flow(hass, [_announcement(ANNOUNCED)])
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_confirm"
    assert result["description_placeholders"] == {
        "addon": f"{BRAND_NAME} Hub",
        "version": "2026.12.0",
    }

    mock_api.get(
        "http://local-halumar:8099/api/integration/status",
        status=200,
        payload=make_status_payload(),
    )
    with patch("custom_components.halumar.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_HOST: "local-halumar",
        CONF_PORT: 8099,
        CONF_API_TOKEN: "supervisor-token",
        CONF_USE_SSL: False,
        CONF_HUB_INSTANCE_ID: HUB_INSTANCE_ID,
    }
    assert result["result"].unique_id == ENTRY_UNIQUE_ID


@pytest.mark.parametrize(
    "announcements",
    [
        pytest.param([], id="none"),
        pytest.param([_announcement(ANNOUNCED, service="other")], id="other-service"),
        pytest.param(
            [_announcement(ANNOUNCED), _announcement({**ANNOUNCED, "port": 9000})],
            id="several",
        ),
        pytest.param(
            [_announcement({k: v for k, v in ANNOUNCED.items() if k != "api_token"})],
            id="without-token",
        ),
        pytest.param([_announcement({**ANNOUNCED, "port": "x"})], id="bad-port"),
    ],
)
async def test_user_step_without_a_usable_announcement_shows_the_form(
    hass: HomeAssistant, mock_api: aioresponses, announcements: list[Any]
) -> None:
    """No, another, several or an incomplete announcement: the manual form,
    which still sets up the entry."""
    result, listing = await _start_user_flow(hass, announcements)
    listing.assert_awaited_once()
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    mock_api.get(STATUS_URL, status=200, payload=make_status_payload())
    with patch("custom_components.halumar.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == ENTRY_UNIQUE_ID


async def test_user_step_without_supervisor_never_asks_it(hass: HomeAssistant) -> None:
    result, listing = await _start_user_flow(
        hass, [_announcement(ANNOUNCED)], supervisor=False
    )
    listing.assert_not_awaited()
    assert result["step_id"] == "user"


async def test_user_step_with_failing_supervisor_shows_the_form(
    hass: HomeAssistant,
) -> None:
    from unittest.mock import AsyncMock

    failing = AsyncMock(side_effect=RuntimeError("Supervisor unavailable"))
    result, _listing = await _start_user_flow(hass, failing)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_user_step_with_slow_supervisor_shows_the_form(
    hass: HomeAssistant,
) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    async def _never(_hass: HomeAssistant) -> list[Any]:
        await asyncio.sleep(3600)
        return []

    slow = AsyncMock(side_effect=_never)
    with patch("custom_components.halumar.token_renewal.SUPERVISOR_TIMEOUT", 0.01):
        result, _listing = await _start_user_flow(hass, slow)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_user_step_with_configured_hub_aborts(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """An announcement of the hub that is already set up: already configured."""
    result, listing = await _start_user_flow(hass, [_announcement(ANNOUNCED)])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    listing.assert_not_awaited()


async def test_user_step_aborts_when_a_discovery_flow_owns_the_hub(
    hass: HomeAssistant, mock_api: aioresponses
) -> None:
    """A discovery flow already in progress for the hub: no second flow."""
    discovered = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_HASSIO},
        data=_hassio_discovery_info(host="local-halumar"),
    )
    assert discovered["step_id"] == "hassio_confirm"
    result, _listing = await _start_user_flow(hass, [_announcement(ANNOUNCED)])
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_in_progress"
