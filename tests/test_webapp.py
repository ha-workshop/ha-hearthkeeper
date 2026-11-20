"""Tests for the Telegram Mini App endpoints."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hearthkeeper.const import DOMAIN
from custom_components.hearthkeeper.settings import InvalidPreference
from custom_components.hearthkeeper.webapp import (
    WEBAPP_API_PATH,
    WEBAPP_PATH,
    async_register_webapp,
    get_webapp_url,
    validate_init_data,
)

TOKEN = "123456:TEST-token"
USER_ID = 42


def _sign(fields: dict[str, str], token: str = TOKEN) -> str:
    """Build initData signed the way Telegram does it."""
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": digest})


def _init_data(
    user_id: int = USER_ID, *, auth_date: int | None = None, token: str = TOKEN
) -> str:
    return _sign(
        {
            "auth_date": str(int(time.time()) if auth_date is None else auth_date),
            "query_id": "AAE",
            "user": json.dumps(
                {"id": user_id, "first_name": "Олег", "username": "oleg"}
            ),
        },
        token,
    )


def test_valid_init_data_returns_user() -> None:
    """A correctly signed initData names its user."""
    user = validate_init_data(_init_data(), TOKEN)

    assert user is not None
    assert user.id == USER_ID
    assert user.first_name == "Олег"


@pytest.mark.parametrize(
    "init_data",
    [
        "",
        "garbage",
        _init_data(token="999:other"),
        _init_data(auth_date=int(time.time()) - 2 * 24 * 3600),
        _init_data().replace("%5Cu041e", "%5Cu0410"),
        _sign({"auth_date": str(int(time.time())), "user": "not json"}),
        _sign({"auth_date": str(int(time.time())), "user": '{"id": "42"}'}),
    ],
)
def test_invalid_init_data_is_refused(init_data: str) -> None:
    """Wrong token, stale date, tampering and bad users are all refused."""
    assert validate_init_data(init_data, TOKEN) is None


def test_tampered_field_breaks_the_signature() -> None:
    """Changing the user id after signing invalidates the hash."""
    tampered = _init_data().replace("%22id%22%3A+42", "%22id%22%3A+43")

    assert validate_init_data(tampered, TOKEN) is None


async def test_webapp_url_requires_https(hass: HomeAssistant) -> None:
    """Telegram opens Mini Apps only over HTTPS."""
    hass.config.external_url = "http://192.168.1.10:8123"
    hass.config.internal_url = "http://192.168.1.10:8123"
    assert get_webapp_url(hass) is None

    hass.config.external_url = "https://home.example.org"
    assert get_webapp_url(hass) == f"https://home.example.org{WEBAPP_PATH}"


class FakeBrain:
    """Record what the API asks the brain to do."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def webapp_state(self, chat_id: int, first_name: str = "") -> dict[str, Any]:
        self.calls.append(("state", chat_id))
        return {"chat_id": chat_id, "first_name": first_name}

    async def async_set_reply_mode(self, chat_id: int, mode: str) -> bool:
        self.calls.append(("mode", mode))
        return mode == "text"

    async def async_set_agent(self, chat_id: int, agent_id: str | None) -> bool:
        self.calls.append(("agent", agent_id))
        return agent_id in (None, "conversation.home")

    async def async_new_conversation(self, chat_id: int) -> None:
        self.calls.append(("new", chat_id))

    def cancel_reminders(self, chat_id: int) -> int:
        self.calls.append(("cancel", chat_id))
        return 0

    async def async_update_preferences(self, chat_id: int, patch: object) -> list[str]:
        self.calls.append(("prefs", (chat_id, patch)))
        if patch == {"brief_time": "25:00"}:
            raise InvalidPreference("brief_time")
        return sorted(patch)  # type: ignore[arg-type]


class FakeTransport:
    """Record injected commands."""

    def __init__(self) -> None:
        self.commands: list[tuple[int, str, list[str]]] = []

    def async_inject_command(
        self, chat_id: int, user_id: int, command: str, args: list[str]
    ) -> None:
        self.commands.append((chat_id, command, args))


@pytest.fixture
async def api(hass: HomeAssistant, hass_client: Any) -> Any:
    """Set up the views with one loaded entry and return a client helper."""
    assert await async_setup_component(hass, "http", {})
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    runtime = SimpleNamespace(
        bot_token=TOKEN,
        allowed_chats={USER_ID},
        brain=FakeBrain(),
        transport=FakeTransport(),
    )
    entry.runtime_data = runtime
    entry.mock_state(hass, ConfigEntryState.LOADED)

    await async_register_webapp(hass)
    await async_register_webapp(hass)  # a reload must not register twice
    client = await hass_client()

    async def call(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        response = await client.post(WEBAPP_API_PATH, json=body)
        return response.status, await response.json()

    return SimpleNamespace(call=call, client=client, runtime=runtime)


async def test_page_is_served_without_auth(api: Any) -> None:
    """The page itself carries no data and needs no HA token."""
    response = await api.client.get(WEBAPP_PATH, headers={"Authorization": ""})

    assert response.status == 200
    assert "Hearthkeeper" in await response.text()
    assert "frame-ancestors" in response.headers["Content-Security-Policy"]


async def test_state_for_an_allowed_user(api: Any) -> None:
    """A signed call from an allowed user reads that user's private chat."""
    status, body = await api.call({"init_data": _init_data(), "action": "state"})

    assert status == 200
    assert body["state"] == {
        "chat_id": USER_ID,
        "first_name": "Олег",
        "account": {"id": USER_ID, "name": "Олег", "username": "oleg"},
    }


async def test_preferences_belong_to_the_signed_in_account(api: Any) -> None:
    """Personal settings are stored for the account that signed initData."""
    init = _init_data()

    status, body = await api.call(
        {"init_data": init, "action": "set_prefs", "prefs": {"brief_time": "07:15"}}
    )
    assert status == 200
    assert body["state"]["account"]["id"] == USER_ID
    assert ("prefs", (USER_ID, {"brief_time": "07:15"})) in api.runtime.brain.calls

    status, body = await api.call(
        {"init_data": init, "action": "set_prefs", "prefs": {"brief_time": "25:00"}}
    )
    assert (status, body["error"]) == (400, "bad_value")

    # Another user of the same bot cannot write into this account.
    status, _ = await api.call(
        {"init_data": _init_data(7), "action": "set_prefs", "prefs": {"home": False}}
    )
    assert status == 401


async def test_unknown_signature_is_unauthorized(api: Any) -> None:
    """A call signed by another bot gets nothing."""
    status, body = await api.call(
        {"init_data": _init_data(token="1:other"), "action": "state"}
    )

    assert status == 401
    assert body == {"ok": False, "error": "unauthorized"}
    assert api.runtime.brain.calls == []


async def test_user_outside_allowlist_is_unauthorized(api: Any) -> None:
    """A valid signature is not enough: the user must be in the allowlist."""
    status, _ = await api.call({"init_data": _init_data(7), "action": "state"})

    assert status == 401
    assert api.runtime.brain.calls == []


async def test_settings_actions(api: Any) -> None:
    """Settings change through the brain and return the new state."""
    init = _init_data()

    status, _ = await api.call(
        {"init_data": init, "action": "set_reply_mode", "mode": "text"}
    )
    assert status == 200
    status, body = await api.call(
        {"init_data": init, "action": "set_reply_mode", "mode": "voice"}
    )
    assert (status, body["error"]) == (409, "no_tts")
    status, _ = await api.call(
        {"init_data": init, "action": "set_reply_mode", "mode": "loud"}
    )
    assert status == 400

    status, _ = await api.call(
        {"init_data": init, "action": "set_agent", "agent_id": None}
    )
    assert status == 200
    status, body = await api.call(
        {"init_data": init, "action": "set_agent", "agent_id": "conversation.x"}
    )
    assert (status, body["error"]) == (400, "unknown_agent")

    status, _ = await api.call({"init_data": init, "action": "new_conversation"})
    assert status == 200
    status, _ = await api.call({"init_data": init, "action": "cancel_reminders"})
    assert status == 200

    assert ("new", USER_ID) in api.runtime.brain.calls
    assert ("cancel", USER_ID) in api.runtime.brain.calls


async def test_commands_are_allowlisted(api: Any) -> None:
    """Only a few read-only commands may run from the page."""
    init = _init_data()

    status, _ = await api.call(
        {"init_data": init, "action": "command", "command": "brief"}
    )
    assert status == 200
    status, _ = await api.call(
        {
            "init_data": init,
            "action": "command",
            "command": "remind",
            "args": ["list"],
        }
    )
    assert status == 200
    for bad in ("model", "voice", "/brief", "anything"):
        status, _ = await api.call(
            {"init_data": init, "action": "command", "command": bad}
        )
        assert status == 400

    assert api.runtime.transport.commands == [
        (USER_ID, "brief", []),
        (USER_ID, "remind", ["list"]),
    ]


async def test_bad_bodies(api: Any) -> None:
    """Malformed requests are refused before any work is done."""
    response = await api.client.post(WEBAPP_API_PATH, data="not json")
    assert response.status == 400

    status, _ = await api.call({"action": "state"})
    assert status == 401

    status, _ = await api.call({"init_data": _init_data(), "action": "rm -rf"})
    assert status == 400
