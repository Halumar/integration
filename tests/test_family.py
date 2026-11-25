"""Family manager in the integration.

* Releases of the family manager in the rule snapshot (contracts
  common/enforcement-snapshot.json, docs/enforcement.md): bedtime, pause and
  one entity, compared with the integration's own clock.
* The away route (contracts integration/ha-ws-family.*.json): HA WebSocket
  ``<ns>/family`` → ``result.messages[]`` (body of POST /api/integration/family),
  ``<ns>/family/subscribe`` → one event per ``family_relay`` push, the hub
  message itself; only the device user of a Halumar device.
"""

# ruff: noqa: F811 - pytest fixtures imported from test_enforce are parameters here

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from aioresponses import CallbackResult, aioresponses
from homeassistant.auth.const import GROUP_ID_READ_ONLY
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from yarl import URL

from custom_components.halumar.const import DOMAIN
from custom_components.halumar.device_users import (
    DATA_UNLOADED,
    ISSUE_FOREIGN_TOKENS,
)
from custom_components.halumar.family import async_handle_relay
from custom_components.halumar.snapshot import (
    SnapshotError,
    rules_hash,
    validate_snapshot,
)

from ._ha import MockUser
from .common import BASE_URL, CLIENT_ID
from .test_device_users import _poll, _settle
from .test_enforce import (  # noqa: F401 - the fixtures env and existing_owner
    ALL_DAY,
    Env,
    _kids_env_snapshot,
    env,
    existing_owner,
)

FAMILY_URL = f"{BASE_URL}/api/integration/family"
KIDS_URL = f"{BASE_URL}/api/integration/kids-check"


def _rehash(snap: dict[str, Any]) -> dict[str, Any]:
    snap["rules_hash"] = rules_hash(snap)
    return snap


def _in(minutes: int) -> str:
    return (dt_util.utcnow() + timedelta(minutes=minutes)).isoformat()


# ── releases in the snapshot ─────────────────────────────────────────────


async def test_bedtime_release(env: Env, hass: HomeAssistant) -> None:
    snap = _kids_env_snapshot(env, quiet=ALL_DAY)
    await env.apply(snap)
    await env.denied("blocked_kids_bedtime", entity_id="light.bed", action="turn_on")
    snap["kids"]["bedtime_released_until"] = _in(30)
    await env.apply(_rehash(snap))
    await env.control(entity_id="light.bed", action="turn_on")
    # a release in the past is none (also while the hub is away)
    snap["kids"]["bedtime_released_until"] = _in(-1)
    await env.apply(_rehash(snap))
    await env.denied("blocked_kids_bedtime", entity_id="light.bed", action="turn_on")


async def test_entity_release_lifts_type_block_and_bedtime(
    env: Env, hass: HomeAssistant
) -> None:
    snap = _kids_env_snapshot(env, blocked=True, quiet=ALL_DAY)
    await env.apply(snap)
    await env.denied("blocked_kids", entity_id="light.bed", action="turn_on")
    snap["kids"]["entities"]["light.bed"]["release_until"] = _in(15)
    await env.apply(_rehash(snap))
    await env.control(entity_id="light.bed", action="turn_on")
    # the other entity stays blocked (no release of its own)
    await env.denied("blocked_kids", entity_id="light.ceiling", action="turn_on")


async def test_pause_release_skips_the_kids_check(
    env: Env, hass: HomeAssistant, mock_api: aioresponses
) -> None:
    asked: list[Any] = []

    def kids(url: URL, **kwargs: Any) -> CallbackResult:
        asked.append(kwargs["json"])
        return CallbackResult(
            status=200, payload={"allowed": False, "reason": "cooldown", "until": None}
        )

    mock_api.post(KIDS_URL, callback=kids, repeat=True)
    snap = _kids_env_snapshot(env, rate=True)
    await env.apply(snap)
    await env.denied("blocked_kids_cooldown", entity_id="light.bed", action="turn_on")
    assert len(asked) == 1
    snap["kids"]["pause_released_until"] = _in(15)
    await env.apply(_rehash(snap))
    await env.control(entity_id="light.bed", action="turn_on")
    assert len(asked) == 1


def test_release_fields_are_validated(env: Env) -> None:
    snap = _kids_env_snapshot(env)
    snap["kids"]["bedtime_released_until"] = None
    snap["kids"]["entities"]["light.bed"]["release_until"] = _in(5)
    validate_snapshot(_rehash(snap))
    snap["kids"]["entities"]["light.bed"]["release_until"] = "tomorrow"
    with pytest.raises(SnapshotError):
        validate_snapshot(_rehash(snap))
    snap["kids"]["entities"]["light.bed"].pop("release_until")
    snap["kids"]["released"] = True
    with pytest.raises(SnapshotError):
        validate_snapshot(_rehash(snap))


# ── away route over the HA WebSocket ─────────────────────────────────────


async def _ws_of_device(env: Env, hass: HomeAssistant, hass_ws_client: Any) -> Any:
    record = env.entry.runtime_data.device_users.records[CLIENT_ID]
    token = hass.auth.async_create_access_token(
        env.user.refresh_tokens[record["refresh_token_id"]]
    )
    return await hass_ws_client(hass, token)


async def test_family_command_relays_and_answers_messages(
    env: Env, hass: HomeAssistant, hass_ws_client: Any, mock_api: aioresponses
) -> None:
    mock_api.passthrough_unmatched = True
    bodies: list[dict[str, Any]] = []
    answer = {
        "messages": [{"type": "family_children", "request_id": "c1", "children": []}]
    }

    def family(url: URL, **kwargs: Any) -> CallbackResult:
        bodies.append(kwargs["json"])
        return CallbackResult(status=200, payload=answer)

    mock_api.post(FAMILY_URL, callback=family, repeat=True)
    ws = await _ws_of_device(env, hass, hass_ws_client)
    await ws.send_json_auto_id(
        {
            "type": f"{DOMAIN}/family",
            "message": {"type": "family_get_children", "request_id": "c1"},
        }
    )
    msg = await ws.receive_json()
    assert msg["success"] is True, msg
    assert msg["result"] == answer
    assert bodies == [
        {
            "client_id": CLIENT_ID,
            "message": {"type": "family_get_children", "request_id": "c1"},
        }
    ]
    # a child device's request goes the same way (the hub decides)
    await ws.send_json_auto_id(
        {
            "type": f"{DOMAIN}/family",
            "message": {"type": "kids_request", "category": "stay_up"},
        }
    )
    msg = await ws.receive_json()
    assert msg["success"] is True, msg
    assert bodies[-1] == {
        "client_id": CLIENT_ID,
        "message": {"type": "kids_request", "category": "stay_up"},
    }
    # A recipient answers an early access request the same way
    answer_msg = {
        "type": "access_request_answer",
        "request_id": "r1",
        "answer": "yes",
        "duration": "1h",
    }
    await ws.send_json_auto_id({"type": f"{DOMAIN}/family", "message": answer_msg})
    msg = await ws.receive_json()
    assert msg["success"] is True, msg
    assert bodies[-1] == {"client_id": CLIENT_ID, "message": answer_msg}
    # anything else is refused before the hub
    await ws.send_json_auto_id(
        {"type": f"{DOMAIN}/family", "message": {"type": "control"}}
    )
    msg = await ws.receive_json()
    assert msg["success"] is False and msg["error"]["code"] == "invalid_format"
    assert len(bodies) == 3


async def test_family_command_hub_away(
    env: Env, hass: HomeAssistant, hass_ws_client: Any, mock_api: aioresponses
) -> None:
    mock_api.passthrough_unmatched = True
    mock_api.post(FAMILY_URL, status=500, repeat=True)
    ws = await _ws_of_device(env, hass, hass_ws_client)
    await ws.send_json_auto_id(
        {"type": f"{DOMAIN}/family", "message": {"type": "family_get_requests"}}
    )
    msg = await ws.receive_json()
    assert msg["success"] is True
    (error,) = msg["result"]["messages"]
    assert error["type"] == "family_error" and error["reason"] == "hub_unreachable"


async def test_family_command_only_for_device_users(
    env: Env,
    hass: HomeAssistant,
    hass_ws_client: Any,
    hass_admin_user: MockUser,
    mock_api: aioresponses,
) -> None:
    mock_api.passthrough_unmatched = True
    ws = await hass_ws_client(hass)  # the admin of the harness
    await ws.send_json_auto_id(
        {"type": f"{DOMAIN}/family", "message": {"type": "family_get_children"}}
    )
    msg = await ws.receive_json()
    assert msg["success"] is False and msg["error"]["code"] == "unauthorized"
    await ws.send_json_auto_id({"type": f"{DOMAIN}/family/subscribe"})
    msg = await ws.receive_json()
    assert msg["success"] is False and msg["error"]["code"] == "unauthorized"


async def test_subscription_gets_the_hub_push_itself(
    env: Env, hass: HomeAssistant, hass_ws_client: Any, mock_api: aioresponses
) -> None:
    mock_api.passthrough_unmatched = True
    ws = await _ws_of_device(env, hass, hass_ws_client)
    await ws.send_json_auto_id({"type": f"{DOMAIN}/family/subscribe"})
    msg = await ws.receive_json()
    assert msg["success"] is True and msg["result"] is None
    sub_id = msg["id"]
    push = {"type": "family_request", "request": {"request_id": "r1", "status": "open"}}
    assert async_handle_relay(
        hass,
        {"type": "family_relay", "client_ids": [CLIENT_ID, "other"], "message": push},
    )
    event = await ws.receive_json()
    assert event == {"id": sub_id, "type": "event", "event": push}
    # The answer to a child's request is relayed as well
    state = {"type": "kids_request_state", "request": {"request_id": "r1"}}
    assert async_handle_relay(
        hass, {"type": "family_relay", "client_ids": [CLIENT_ID], "message": state}
    )
    event = await ws.receive_json()
    assert event == {"id": sub_id, "type": "event", "event": state}
    # Early access requests reach the recipients away
    update = {"type": "access_request_update", "request": {"request_id": "r2"}}
    assert async_handle_relay(
        hass, {"type": "family_relay", "client_ids": [CLIENT_ID], "message": update}
    )
    event = await ws.receive_json()
    assert event == {"id": sub_id, "type": "event", "event": update}
    # other messages are not relayed
    assert async_handle_relay(
        hass,
        {"type": "family_relay", "client_ids": [CLIENT_ID], "message": {"type": "x"}},
    )
    # other types are not ours; a broken relay is swallowed
    assert not async_handle_relay(hass, {"type": "family_changed", "data": {}})
    assert async_handle_relay(hass, {"type": "family_relay", "client_ids": "x"})
    # after unsubscribing nothing more arrives
    await ws.send_json_auto_id({"type": "unsubscribe_events", "subscription": sub_id})
    msg = await ws.receive_json()
    assert msg["success"] is True
    await _settle(hass)
    async_handle_relay(
        hass, {"type": "family_relay", "client_ids": [CLIENT_ID], "message": push}
    )
    await ws.send_json_auto_id({"type": "ping"})
    assert (await ws.receive_json())["type"] == "pong"


async def test_family_command_limits_and_odd_answers(
    env: Env,
    hass: HomeAssistant,
    hass_ws_client: Any,
    mock_api: aioresponses,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.halumar import family

    mock_api.passthrough_unmatched = True
    answers = [
        CallbackResult(status=429, payload={"messages": []}),
        CallbackResult(status=200, payload={"messages": []}),
        CallbackResult(status=200, payload={"messages": [{"type": "family_children"}]}),
    ]
    mock_api.post(FAMILY_URL, callback=lambda url, **kw: answers.pop(0), repeat=True)
    ws = await _ws_of_device(env, hass, hass_ws_client)
    command = {"type": f"{DOMAIN}/family", "message": {"type": "family_get_children"}}

    async def send() -> dict[str, Any]:
        await ws.send_json_auto_id(dict(command))
        msg: dict[str, Any] = await ws.receive_json()
        assert msg["success"] is True, msg
        result: dict[str, Any] = msg["result"]
        return result

    # the hub's 429 and an empty answer
    assert (await send())["messages"][0]["reason"] == "rate_limited"
    assert (await send())["messages"][0]["reason"] == "hub_unreachable"
    # per HA user: the integration's own window
    family.family_calls(hass).clear()
    monkeypatch.setattr(family, "RATE_LIMIT", 1)
    assert (await send())["messages"][0]["type"] == "family_children"
    assert (await send())["messages"][0]["reason"] == "rate_limited"
    family.family_calls(hass).clear()
    monkeypatch.setattr(family, "RATE_WINDOW", 0.0)
    # without a loaded entry the hub is unreachable
    await hass.config_entries.async_unload(env.entry.entry_id)
    assert (await send())["messages"][0]["reason"] == "hub_unreachable"
    # a relay whose message is not a family message is swallowed
    assert async_handle_relay(
        hass,
        {"type": "family_relay", "client_ids": [CLIENT_ID], "message": {"type": "x"}},
    )


# ── identity of the caller ────────────────────────────────────────────────

PARENT_ID = "99999999-9999-4999-8999-999999999999"


@pytest.fixture
def expected_lingering_timers() -> bool:
    """A self-minted LLAT has an expiry timer in HA's auth manager."""
    return True


async def test_family_device_is_not_taken_from_token_names(
    env: Env,
    hass: HomeAssistant,
    hass_ws_client: Any,
    mock_api: aioresponses,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A device user that mints a token named after another device and
    deletes its own stays its own device, and the reconcile cleans up."""
    mock_api.passthrough_unmatched = True
    bodies: list[dict[str, Any]] = []

    def family(url: URL, **kwargs: Any) -> CallbackResult:
        bodies.append(kwargs["json"])
        return CallbackResult(
            status=200, payload={"messages": [{"type": "family_children"}]}
        )

    mock_api.post(FAMILY_URL, callback=family, repeat=True)
    assert await async_setup_component(hass, "auth", {})
    await env.apply()  # the strictest case: a read-only device user
    assert [g.id for g in env.user.groups] == [GROUP_ID_READ_ONLY]
    record = env.entry.runtime_data.device_users.records[CLIENT_ID]
    own_token = env.user.refresh_tokens[record["refresh_token_id"]]
    ws = await hass_ws_client(hass, hass.auth.async_create_access_token(own_token))
    await ws.send_json_auto_id(
        {
            "type": "auth/long_lived_access_token",
            "client_name": f"{DOMAIN}:{PARENT_ID}",
            "lifespan": 30,
        }
    )
    msg = await ws.receive_json()
    assert msg["success"], msg
    self_minted_ws = await hass_ws_client(hass, msg["result"])
    await self_minted_ws.send_json_auto_id(
        {"type": "auth/delete_refresh_token", "refresh_token_id": own_token.id}
    )
    assert (await self_minted_ws.receive_json())["success"]

    await self_minted_ws.send_json_auto_id(
        {
            "type": f"{DOMAIN}/family",
            "message": {"type": "family_release", "request_id": "x"},
        }
    )
    msg = await self_minted_ws.receive_json()
    assert msg["success"], msg
    assert [body["client_id"] for body in bodies] == [CLIENT_ID]
    await self_minted_ws.send_json_auto_id({"type": f"{DOMAIN}/family/subscribe"})
    msg = await self_minted_ws.receive_json()
    assert msg["success"], msg
    sub_id = msg["id"]
    push = {"type": "family_request", "request": {"request_id": "r1"}}
    foreign = {"type": "family_request", "request": {"request_id": "parent"}}
    async_handle_relay(
        hass, {"type": "family_relay", "client_ids": [PARENT_ID], "message": foreign}
    )
    async_handle_relay(
        hass, {"type": "family_relay", "client_ids": [CLIENT_ID], "message": push}
    )
    # the push for the other device never arrives, the own one does
    event = await self_minted_ws.receive_json()
    assert event == {"id": sub_id, "type": "event", "event": push}

    # The next poll removes the foreign token, reports it and gives the
    # device its own token again.
    await _poll(hass, env.entry)
    names = [t.client_name for t in env.user.refresh_tokens.values()]
    assert names and all(
        name == f"{DOMAIN}:{CLIENT_ID}" or name.startswith(f"{DOMAIN}:{CLIENT_ID}/")
        for name in names
    )
    assert "did not create" in caplog.text
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_FOREIGN_TOKENS}_{CLIENT_ID}"
    )
    assert issue is not None
    assert issue.translation_key == ISSUE_FOREIGN_TOKENS
    # a poll that finds nothing foreign ends the report
    await _poll(hass, env.entry)
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"{ISSUE_FOREIGN_TOKENS}_{CLIENT_ID}"
        )
        is None
    )


async def test_family_command_refuses_a_user_with_device_token_names_only(
    env: Env, hass: HomeAssistant, hass_ws_client: Any, mock_api: aioresponses
) -> None:
    """A plain HA user is no device because of how it names its tokens."""
    mock_api.passthrough_unmatched = True
    stranger = MockUser(name="Stranger").add_to_hass(hass)
    token = await hass.auth.async_create_refresh_token(
        stranger,
        client_name=f"{DOMAIN}:{CLIENT_ID}",
        token_type="long_lived_access_token",
    )
    ws = await hass_ws_client(hass, hass.auth.async_create_access_token(token))
    await ws.send_json_auto_id(
        {"type": f"{DOMAIN}/family", "message": {"type": "family_get_children"}}
    )
    msg = await ws.receive_json()
    assert msg["success"] is False and msg["error"]["code"] == "unauthorized"
    await ws.send_json_auto_id({"type": f"{DOMAIN}/family/subscribe"})
    msg = await ws.receive_json()
    assert msg["success"] is False and msg["error"]["code"] == "unauthorized"


async def test_family_command_knows_the_device_while_the_entry_is_unloaded(
    env: Env, hass: HomeAssistant, hass_ws_client: Any, mock_api: aioresponses
) -> None:
    """Without a loaded entry the persisted mapping still names the device."""
    mock_api.passthrough_unmatched = True
    ws = await _ws_of_device(env, hass, hass_ws_client)
    assert await hass.config_entries.async_unload(env.entry.entry_id)
    await ws.send_json_auto_id(
        {"type": f"{DOMAIN}/family", "message": {"type": "family_get_children"}}
    )
    msg = await ws.receive_json()
    assert msg["success"] is True
    assert msg["result"]["messages"][0]["reason"] == "hub_unreachable"
    # the mapping is read from disk once, not per message
    with patch(
        "custom_components.halumar.device_users.DeviceUserManager.async_setup",
        side_effect=AssertionError("read again"),
    ):
        await ws.send_json_auto_id({"type": f"{DOMAIN}/family/subscribe"})
        assert (await ws.receive_json())["success"] is True
    # loading the entry ends the kept copy
    assert await hass.config_entries.async_setup(env.entry.entry_id)
    await _settle(hass)
    assert env.entry.entry_id not in hass.data[DATA_UNLOADED]
