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

from custom_components.hearthkeeper.connections import ConnectionPlugins
from custom_components.hearthkeeper.const import DOMAIN
from custom_components.hearthkeeper.personal_calendar import CalendarError
from custom_components.hearthkeeper.personal_mail import MailboxError
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


def test_webapp_url_override(hass: HomeAssistant) -> None:
    """A tunnel address from the options is used instead of the automatic URL."""
    hass.config.external_url = None
    hass.config.internal_url = None

    assert (
        get_webapp_url(hass, "https://tunnel.example.org")
        == f"https://tunnel.example.org{WEBAPP_PATH}"
    )
    assert (
        get_webapp_url(hass, "https://tunnel.example.org/")
        == f"https://tunnel.example.org{WEBAPP_PATH}"
    )
    assert (
        get_webapp_url(hass, "https://tunnel.example.org/ha")
        == f"https://tunnel.example.org/ha{WEBAPP_PATH}"
    )
    # A full Mini App URL must not get the path twice.
    assert (
        get_webapp_url(hass, f"https://tunnel.example.org{WEBAPP_PATH}")
        == f"https://tunnel.example.org{WEBAPP_PATH}"
    )
    for bad in (
        "http://tunnel.example.org",
        "https://tunnel.example.org/?a=1",
        "https://tunnel.example.org/#x",
        "tunnel.example.org",
        42,
    ):
        assert get_webapp_url(hass, bad) is None
    assert get_webapp_url(hass, "") is None


class FakeBrain:
    """Record what the API asks the brain to do."""

    def __init__(self, hass: HomeAssistant | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.hass = hass

    def chat_language(self, chat_id: int) -> str:
        return "ru"

    async def async_set_personal_mail(
        self, chat_id: int, raw: Any
    ) -> dict[str, Any]:
        if raw.get("password") != "app-pass":
            raise MailboxError("auth")
        self.calls.append(("mail", chat_id))
        return {"host": raw["host"], "username": raw["username"], "unread": 2}

    async def async_add_personal_calendar(
        self, chat_id: int, name: Any, url: Any
    ) -> dict[str, Any]:
        if not str(url).startswith("https://"):
            raise CalendarError("bad_url")
        return {"id": "c1", "name": name, "host": "cal.example.com", "today": 1}

    async def async_remove_personal_calendar(self, chat_id: int, cal_id: Any) -> None:
        self.calls.append(("cal_off", cal_id))

    async def async_remove_personal_mail(self, chat_id: int) -> None:
        self.calls.append(("mail_off", chat_id))

    def has_custom_skill(self, chat_id: int, skill_id: str) -> bool:
        return skill_id == "s1"

    async def async_run_custom_skill(self, chat_id: int, skill_id: str) -> bool:
        self.calls.append(("run_skill", (chat_id, skill_id)))
        return True

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
        if patch == {"briefings": [{"kind": "morning", "time": "25:00", "enabled": True}]}:
            raise InvalidPreference("briefings")
        return sorted(patch)  # type: ignore[arg-type]


class FakeConnections:
    """No plugins."""

    async def async_plugins(self) -> list[Any]:
        return []

    def integrations(self, kind: str) -> set[str]:
        return set()


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
        brain=FakeBrain(hass),
        transport=FakeTransport(),
        connections=FakeConnections(),
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
        "is_admin": False,
        "account": {"id": USER_ID, "name": "Олег", "username": "oleg"},
    }


async def test_preferences_belong_to_the_signed_in_account(api: Any) -> None:
    """Personal settings are stored for the account that signed initData."""
    init = _init_data()

    status, body = await api.call(
        {"init_data": init, "action": "set_prefs", "prefs": {"briefings": [{"kind": "morning", "time": "07:15", "enabled": True}]}}
    )
    assert status == 200
    assert body["state"]["account"]["id"] == USER_ID
    assert ("prefs", (USER_ID, {"briefings": [{"kind": "morning", "time": "07:15", "enabled": True}]})) in api.runtime.brain.calls

    status, body = await api.call(
        {"init_data": init, "action": "set_prefs", "prefs": {"briefings": [{"kind": "morning", "time": "25:00", "enabled": True}]}}
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


async def test_connections_list_is_public(api: Any) -> None:
    """Every allowed user can see the plugins; changing them needs an admin."""
    init = _init_data()
    status, body = await api.call({"init_data": init, "action": "connections"})
    assert (status, body) == (200, {"ok": True, "connections": []})

    for action in ("preview_connection", "add_connection", "remove_connection"):
        status, body = await api.call(
            {"init_data": init, "action": action, "repo": "owner/repo"}
        )
        assert (status, body["error"]) == (403, "forbidden")

    status, body = await api.call({"init_data": init, "action": "shared"})
    assert (status, body["error"]) == (403, "forbidden")


async def test_run_skill_only_for_own_skills(hass: HomeAssistant, api: Any) -> None:
    """A skill runs in the background; unknown ids are refused."""
    init = _init_data()

    status, body = await api.call(
        {"init_data": init, "action": "run_skill", "id": "s1"}
    )
    await hass.async_block_till_done()
    assert (status, body) == (200, {"ok": True})
    assert ("run_skill", (USER_ID, "s1")) in api.runtime.brain.calls

    for bad in ("other", None, 5):
        status, body = await api.call(
            {"init_data": init, "action": "run_skill", "id": bad}
        )
        assert (status, body["error"]) == (400, "bad_request")


async def test_admin_changes_shared_sources_and_plugins(
    hass: HomeAssistant,
    api: Any,
    aioclient_mock: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An admin can pick shared sources and add plugins; both reload the entry."""
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    api.runtime.admins = frozenset({USER_ID})
    api.runtime.entry_id = entry.entry_id
    reloads: list[str] = []
    monkeypatch.setattr(
        hass.config_entries, "async_schedule_reload", reloads.append
    )
    hass.states.async_set("weather.dacha", "sunny", {"friendly_name": "Дача"})
    init = _init_data()

    status, body = await api.call({"init_data": init, "action": "shared"})
    assert status == 200
    assert body["shared"]["weather"]["choices"] == [
        {"id": "weather.dacha", "name": "Дача"}
    ]

    status, body = await api.call(
        {
            "init_data": init,
            "action": "set_shared",
            "key": "weather",
            "value": "weather.nope",
        }
    )
    assert (status, body["error"]) == (400, "bad_value")
    status, _ = await api.call(
        {
            "init_data": init,
            "action": "set_shared",
            "key": "weather",
            "value": "weather.dacha",
        }
    )
    assert status == 200
    assert entry.options["brief_weather"] == "weather.dacha"

    aioclient_mock.get(
        "https://raw.githubusercontent.com/owner/plug/HEAD/connection.json",
        json={
            "id": "plug",
            "name": "Plug",
            "integration": {"domain": "plug"},
            "provides": {"kind": "briefing"},
        },
    )
    api.runtime.connections = ConnectionPlugins(hass, [])
    status, body = await api.call(
        {"init_data": init, "action": "preview_connection", "repo": "owner/plug"}
    )
    plugin = body["plugin"]
    assert (status, plugin["name"], plugin["ready"]) == (200, "Plug", False)
    status, _ = await api.call(
        {"init_data": init, "action": "add_connection", "repo": "owner/plug"}
    )
    assert status == 200
    assert entry.options["connection_repositories"] == ["owner/plug"]
    status, _ = await api.call(
        {"init_data": init, "action": "remove_connection", "repo": "owner/plug"}
    )
    assert entry.options["connection_repositories"] == []
    assert reloads == [entry.entry_id] * 3


async def test_personal_mail_api(api: Any) -> None:
    """Any allowed user connects their own mailbox; errors come back as codes."""
    init = _init_data()
    mail = {"host": "imap.yandex.ru", "username": "o@yandex.ru", "password": "bad"}

    status, body = await api.call(
        {"init_data": init, "action": "set_mail", "mail": mail}
    )
    assert (status, body["error"]) == (400, "mail_auth")

    mail["password"] = "app-pass"
    status, body = await api.call(
        {"init_data": init, "action": "set_mail", "mail": mail}
    )
    assert status == 200
    assert body["mail"] == {
        "host": "imap.yandex.ru",
        "username": "o@yandex.ru",
        "unread": 2,
    }
    assert "app-pass" not in str(body)

    status, _ = await api.call({"init_data": init, "action": "remove_mail"})
    assert status == 200
    assert ("mail_off", USER_ID) in api.runtime.brain.calls


async def test_personal_calendar_api(api: Any) -> None:
    """A calendar link comes back only as its host; errors are codes."""
    init = _init_data()
    status, body = await api.call(
        {"init_data": init, "action": "add_calendar", "name": "W", "url": "http://x"}
    )
    assert (status, body["error"]) == (400, "cal_bad_url")

    secret = "https://cal.example.com/secret-token/basic.ics"
    status, body = await api.call(
        {"init_data": init, "action": "add_calendar", "name": "W", "url": secret}
    )
    assert status == 200
    assert body["calendar"]["host"] == "cal.example.com"
    assert "secret-token" not in str(body)

    status, _ = await api.call(
        {"init_data": init, "action": "remove_calendar", "id": "c1"}
    )
    assert ("cal_off", "c1") in api.runtime.brain.calls


async def test_bot_users_limit_the_mini_app(api: Any) -> None:
    """When bot users are set, others cannot open the Mini App."""
    api.runtime.users = frozenset({1})
    status, body = await api.call({"init_data": _init_data(), "action": "state"})
    assert (status, body["error"]) == (401, "unauthorized")

    api.runtime.users = frozenset({USER_ID})
    status, _ = await api.call({"init_data": _init_data(), "action": "state"})
    assert status == 200
