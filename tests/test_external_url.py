"""Home Assistant's external URL for devices with remote access.

The integration reports what HA itself answers (``get_url`` without internal
URLs: ``external_url``, else the Nabu Casa address) to
POST /api/integration/ha-url, at setup and whenever it changes, and nothing
while it is unchanged.
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any
from unittest.mock import patch

import pytest
from aioresponses import CallbackResult, aioresponses
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.util import dt as dt_util

from custom_components.halumar import external_url as ext
from custom_components.halumar.external_url import (
    CHECK_INTERVAL,
    ExternalUrlReporter,
    external_url,
)

from ._ha import MockConfigEntry, async_fire_time_changed
from .common import BASE_URL, setup_integration

HA_URL = f"{BASE_URL}/api/integration/ha-url"
NABU = "https://abcdef0123456789.ui.nabu.casa"


class _Hub:
    """Records the reports; answers with ``status``."""

    def __init__(self, mock_api: aioresponses) -> None:
        self.reports: list[Any] = []
        self.ids: list[Any] = []
        self.status = 200
        mock_api.post(HA_URL, callback=self._report, repeat=True)

    def _report(self, _url: Any, **kwargs: Any) -> CallbackResult:
        self.reports.append(kwargs["json"]["external_url"])
        self.ids.append(kwargs["json"].get("ha_instance_id"))
        if self.status != 200:
            return CallbackResult(
                status=self.status,
                payload={"detail": {"code": "url_not_allowed", "message": "no"}},
            )
        return CallbackResult(status=200, payload={"success": True, "changed": True})


@pytest.fixture
def url_source() -> Any:
    """What get_url answers (a URL or None = NoURLAvailableError)."""
    state: dict[str, str | None] = {"url": None}

    def fake_get_url(_hass: HomeAssistant, **kwargs: Any) -> str:
        assert kwargs["allow_internal"] is False
        assert kwargs["prefer_cloud"] is False
        if state["url"] is None:
            raise NoURLAvailableError
        return state["url"]

    with patch.object(ext, "get_url", fake_get_url):
        yield state


async def test_external_url_prefers_ha_external_url_then_cloud(
    hass: HomeAssistant,
) -> None:
    """Real get_url: external_url first, else the cloud URL, never internal."""
    await hass.config.async_update(
        internal_url="http://192.0.2.2:8123", external_url=None
    )
    with patch("homeassistant.helpers.network._get_cloud_url", return_value=NABU + "/"):
        assert external_url(hass) == NABU
        await hass.config.async_update(external_url="https://ha.example.com")
        assert external_url(hass) == "https://ha.example.com"
    await hass.config.async_update(external_url=None)
    with patch(
        "homeassistant.helpers.network._get_cloud_url",
        side_effect=NoURLAvailableError,
    ):
        assert external_url(hass) is None


async def test_reported_at_setup_and_only_on_change(
    hass: HomeAssistant,
    mock_api: aioresponses,
    config_entry: MockConfigEntry,
    url_source: Any,
) -> None:
    hub = _Hub(mock_api)
    url_source["url"] = NABU
    await setup_integration(hass, mock_api, config_entry)
    assert hub.reports == [NABU]

    # unchanged: periodic check sends nothing
    async_fire_time_changed(hass, _later(hass, CHECK_INTERVAL * 2))
    await hass.async_block_till_done()
    assert hub.reports == [NABU]

    # remote access switched off in the cloud settings: seen by the check
    url_source["url"] = None
    async_fire_time_changed(hass, _later(hass, CHECK_INTERVAL * 4))
    await hass.async_block_till_done()
    assert hub.reports == [NABU, None]


async def test_core_config_change_is_reported_at_once(
    hass: HomeAssistant,
    mock_api: aioresponses,
    config_entry: MockConfigEntry,
    url_source: Any,
) -> None:
    hub = _Hub(mock_api)
    await setup_integration(hass, mock_api, config_entry)
    assert hub.reports == [None]

    url_source["url"] = "https://ha.example.com"
    await hass.config.async_update(external_url="https://ha.example.com")
    await hass.async_block_till_done()
    assert hub.reports == [None, "https://ha.example.com"]


async def test_failed_report_is_retried(
    hass: HomeAssistant,
    mock_api: aioresponses,
    config_entry: MockConfigEntry,
    url_source: Any,
) -> None:
    hub = _Hub(mock_api)
    hub.status = 503
    url_source["url"] = NABU
    await setup_integration(hass, mock_api, config_entry)
    assert hub.reports == [NABU]

    hub.status = 200
    async_fire_time_changed(hass, _later(hass, CHECK_INTERVAL * 2))
    await hass.async_block_till_done()
    assert hub.reports == [NABU, NABU]

    async_fire_time_changed(hass, _later(hass, CHECK_INTERVAL * 4))
    await hass.async_block_till_done()
    assert hub.reports == [NABU, NABU]


async def test_refused_url_is_not_offered_again(
    hass: HomeAssistant,
    mock_api: aioresponses,
    config_entry: MockConfigEntry,
    url_source: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hub = _Hub(mock_api)
    hub.status = 422
    url_source["url"] = "https://custom.example.org"
    await setup_integration(hass, mock_api, config_entry)
    async_fire_time_changed(hass, _later(hass, CHECK_INTERVAL * 2))
    await hass.async_block_till_done()
    assert hub.reports == ["https://custom.example.org"]
    assert caplog.text.count("did not accept") == 1

    # a different URL is offered again
    hub.status = 200
    url_source["url"] = NABU
    async_fire_time_changed(hass, _later(hass, CHECK_INTERVAL * 4))
    await hass.async_block_till_done()
    assert hub.reports == ["https://custom.example.org", NABU]


async def test_cloud_connection_change_triggers_a_check(
    hass: HomeAssistant,
    mock_api: aioresponses,
    config_entry: MockConfigEntry,
    url_source: Any,
) -> None:
    listeners: list[Any] = []

    def fake_listen(_hass: HomeAssistant, target: Any) -> Any:
        listeners.append(target)
        return lambda: None

    # The real cloud component needs packages the test harness lacks.
    cloud = types.ModuleType("homeassistant.components.cloud")
    cloud.async_listen_connection_change = fake_listen  # type: ignore[attr-defined]

    hub = _Hub(mock_api)
    hass.config.components.add("cloud")
    with patch.dict(sys.modules, {"homeassistant.components.cloud": cloud}):
        await setup_integration(hass, mock_api, config_entry)
    assert hub.reports == [None] and len(listeners) == 1

    url_source["url"] = NABU
    listeners[0]("connected")
    await hass.async_block_till_done()
    assert hub.reports == [None, NABU]


def _later(_hass: HomeAssistant, delta: Any) -> Any:
    return dt_util.utcnow() + delta


async def test_concurrent_checks_coalesce(
    hass: HomeAssistant, config_entry: MockConfigEntry, url_source: Any
) -> None:
    """A change seen while a report runs is sent right after it, once."""
    sent: list[Any] = []
    gate = asyncio.Event()

    class _Coordinator:
        async def api_post_json(self, _path: str, data: dict[str, Any]) -> None:
            sent.append(data["external_url"])
            await gate.wait()

    reporter = ExternalUrlReporter(hass, config_entry, _Coordinator())  # type: ignore[arg-type]
    url_source["url"] = NABU
    first = hass.async_create_task(reporter.async_report())
    # The report first reads HA's instance ID: wait until it is sent.
    for _ in range(50):
        if sent:
            break
        await asyncio.sleep(0)
    assert sent == [NABU] and reporter.reported is None

    url_source["url"] = "https://ha.example.com"
    reporter.async_schedule()  # coalesced into the running report
    assert await reporter.async_report() is False  # already running
    gate.set()
    assert await first is True
    assert sent == [NABU, "https://ha.example.com"]
    assert reporter.reported == "https://ha.example.com"


async def test_instance_id_travels_with_every_report(
    hass: HomeAssistant,
    mock_api: aioresponses,
    config_entry: MockConfigEntry,
    url_source: Any,
) -> None:
    """HA's instance ID (Bonjour TXT uuid) is reported with the URL."""
    from homeassistant.helpers import instance_id

    hub = _Hub(mock_api)
    await setup_integration(hass, mock_api, config_entry)
    expected = await instance_id.async_get(hass)
    assert len(expected) == 32 and expected == expected.lower()
    assert hub.ids == [expected]

    url_source["url"] = NABU
    async_fire_time_changed(hass, _later(hass, CHECK_INTERVAL * 2))
    await hass.async_block_till_done()
    assert hub.ids == [expected, expected]


async def test_new_instance_id_is_reported_with_an_unchanged_url(
    hass: HomeAssistant,
    mock_api: aioresponses,
    config_entry: MockConfigEntry,
    url_source: Any,
) -> None:
    hub = _Hub(mock_api)
    await setup_integration(hass, mock_api, config_entry)
    assert hub.reports == [None]
    with patch.object(ext.instance_id, "async_get", return_value="0" * 32):
        async_fire_time_changed(hass, _later(hass, CHECK_INTERVAL * 2))
        await hass.async_block_till_done()
    assert hub.reports == [None, None]
    assert hub.ids[-1] == "0" * 32
