"""Regression tests: flow forms must serialize like the frontend requests them.

The unit tests in test_config_flow.py drive flows through
hass.config_entries.flow directly, which never serializes data_schema.
The frontend goes through the HTTP flow manager views, which serialize the
schema (voluptuous_serialize up to HA 2026.8, probatio from 2026.9). A
non-serializable validator in a schema yields HTTP 500 there.
"""

from __future__ import annotations

from http import HTTPStatus

from aioresponses import aioresponses
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from homeassistant.setup import async_setup_component

from custom_components.halumar.const import (
    CONF_API_TOKEN,
    CONF_USE_SSL,
    DOMAIN,
)

from ._ha import MockConfigEntry
from .common import STATUS_URL, TEST_HOST, TEST_PORT, TEST_TOKEN, setup_integration

FLOW_URL = "/api/config/config_entries/flow"
OPTIONS_URL = "/api/config/config_entries/options/flow"


async def _http_client(hass: HomeAssistant, hass_client, mock_api: aioresponses):
    """Return an HTTP test client whose requests skip aioresponses.

    Everything that registers HTTP routes is set up first: the flow views
    (config) and the integration's own dependencies (http, websocket_api),
    which the flow manager would otherwise set up only when the first flow
    starts. Depending on the order in which pytest loads its plugins, the
    aiohttp_client fixture can come from pytest-aiohttp, whose test server
    freezes the router; a route registered after the client started would
    then fail.
    """
    integration = await async_get_integration(hass, DOMAIN)
    for domain in ("config", *integration.dependencies):
        assert await async_setup_component(hass, domain, {})
    client = await hass_client()
    mock_api._passthrough.append(str(client.make_url("/")).rstrip("/"))
    return client


def _field_names(schema: list[dict]) -> set[str]:
    return {field["name"] for field in schema}


async def test_user_flow_form_serializes_and_submits(
    hass: HomeAssistant, hass_client, mock_api: aioresponses
) -> None:
    """GET the user form over HTTP (as the UI does) and submit it."""
    client = await _http_client(hass, hass_client, mock_api)

    resp = await client.post(FLOW_URL, json={"handler": DOMAIN})
    assert resp.status == HTTPStatus.OK, await resp.text()
    data = await resp.json()
    assert data["type"] == "form"
    assert data["step_id"] == "user"
    assert {CONF_HOST, CONF_PORT, CONF_API_TOKEN, CONF_USE_SSL} <= _field_names(
        data["data_schema"]
    )

    # Invalid host must come back as a field error, not as HTTP 400/500.
    resp = await client.post(
        f"{FLOW_URL}/{data['flow_id']}",
        json={CONF_HOST: "bad host!", CONF_PORT: TEST_PORT, CONF_API_TOKEN: "x"},
    )
    assert resp.status == HTTPStatus.OK, await resp.text()
    data = await resp.json()
    assert data["type"] == "form"
    assert data["errors"] == {CONF_HOST: "invalid_host"}

    mock_api.get(STATUS_URL, status=200, payload={"hub": {"version": "2026.7.7"}})
    resp = await client.post(
        f"{FLOW_URL}/{data['flow_id']}",
        json={
            CONF_HOST: TEST_HOST,
            CONF_PORT: TEST_PORT,
            CONF_API_TOKEN: TEST_TOKEN,
            CONF_USE_SSL: False,
        },
    )
    assert resp.status == HTTPStatus.OK, await resp.text()
    data = await resp.json()
    assert data["type"] == "create_entry"


async def test_options_flow_form_serializes_and_submits(
    hass: HomeAssistant,
    hass_client,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """Open "Configure" over HTTP and submit the option."""
    await setup_integration(hass, mock_api, config_entry)
    client = await _http_client(hass, hass_client, mock_api)

    resp = await client.post(OPTIONS_URL, json={"handler": config_entry.entry_id})
    assert resp.status == HTTPStatus.OK, await resp.text()
    data = await resp.json()
    assert data["type"] == "form"
    assert data["step_id"] == "init"
    assert _field_names(data["data_schema"]) == {"guest_button_all_areas"}

    resp = await client.post(
        f"{OPTIONS_URL}/{data['flow_id']}", json={"guest_button_all_areas": True}
    )
    assert resp.status == HTTPStatus.OK, await resp.text()
    data = await resp.json()
    assert data["type"] == "create_entry"
    assert config_entry.options == {"guest_button_all_areas": True}


async def test_reconfigure_flow_form_serializes_and_submits(
    hass: HomeAssistant,
    hass_client,
    config_entry: MockConfigEntry,
    mock_api: aioresponses,
) -> None:
    """Open "Reconfigure" over HTTP and submit a new port (token kept)."""
    await setup_integration(hass, mock_api, config_entry)
    client = await _http_client(hass, hass_client, mock_api)

    result = await config_entry.start_reconfigure_flow(hass)
    # GET serializes the form exactly like the frontend sees it.
    resp = await client.get(f"{FLOW_URL}/{result['flow_id']}")
    assert resp.status == HTTPStatus.OK, await resp.text()
    data = await resp.json()
    assert data["step_id"] == "reconfigure", data
    assert {CONF_HOST, CONF_PORT, CONF_API_TOKEN, CONF_USE_SSL} <= _field_names(
        data["data_schema"]
    )

    resp = await client.post(
        f"{FLOW_URL}/{data['flow_id']}",
        json={CONF_HOST: TEST_HOST, CONF_PORT: 70000},
    )
    assert resp.status == HTTPStatus.OK, await resp.text()
    data = await resp.json()
    assert data["errors"] == {CONF_PORT: "invalid_port"}

    resp = await client.post(
        f"{FLOW_URL}/{data['flow_id']}",
        json={CONF_HOST: TEST_HOST, CONF_PORT: TEST_PORT},
    )
    assert resp.status == HTTPStatus.OK, await resp.text()
    data = await resp.json()
    assert data["type"] == "abort"
    assert data["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_API_TOKEN] == TEST_TOKEN


async def test_all_flow_schemas_serialize(hass: HomeAssistant) -> None:
    """Every static flow schema converts with the frontend serializer.

    Mirrors homeassistant/helpers/data_entry_flow.py (HA >= 2026.9 uses
    probatio.to_field_list; older releases voluptuous_serialize.convert).
    """
    from homeassistant.helpers import config_validation as cv

    from custom_components.halumar import config_flow

    try:
        from probatio import to_field_list as convert
    except ImportError:  # HA < 2026.9
        from voluptuous_serialize import convert

    for schema in (
        config_flow.STEP_USER_DATA_SCHEMA,
        config_flow.STEP_REAUTH_DATA_SCHEMA,
        config_flow.STEP_HASSIO_CONFIRM_DATA_SCHEMA,
        config_flow.STEP_RECONFIGURE_DATA_SCHEMA,
    ):
        convert(schema, custom_serializer=cv.custom_serializer)
