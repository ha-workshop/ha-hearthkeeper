"""Telegram Mini App (WebApp) for Hearthkeeper.

Two endpoints live under ``/api/hearthkeeper/webapp``:

* ``GET  /api/hearthkeeper/webapp``      the static page (``webapp.html``);
* ``POST /api/hearthkeeper/webapp/api``  a small JSON API the page calls.

Settings changed here are personal: they belong to the Telegram account that
opened the page and are stored with that user's private chat.

The page itself carries no data, so it is served without Home Assistant
authentication. Every API call carries the Telegram ``initData`` string; it is
checked against the bot token with the HMAC scheme Telegram documents, and the
user it names must be in the chat allowlist of the ``telegram_bot`` entry.
Only then is the call mapped to that user's private chat.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .connections import PluginError, parse_repository
from .const import (
    CONF_BRIEF_CALENDARS,
    CONF_BRIEF_MAIL,
    CONF_BRIEF_WEATHER,
    CONF_CONNECTION_REPOS,
    CONF_TODO_ENTITY,
    DOMAIN,
    REPLY_MODES,
)
from .personal_calendar import CalendarError
from .personal_mail import MailboxError
from .settings import InvalidPreference

if TYPE_CHECKING:
    from . import HearthkeeperRuntime

_LOGGER = logging.getLogger(__name__)

WEBAPP_PATH = f"/api/{DOMAIN}/webapp"
WEBAPP_API_PATH = f"{WEBAPP_PATH}/api"

# initData older than this is refused. Telegram signs it when the Mini App
# opens, so a day covers a page that stays open for a long time.
INIT_DATA_MAX_AGE = 24 * 60 * 60

# Commands the page may run on the user's behalf. Each one answers in the chat,
# exactly as if the user had typed it.
WEBAPP_COMMANDS = frozenset(("brief", "mail", "shop", "help", "remind"))

_DATA_REGISTERED = f"{DOMAIN}_webapp_registered"

_SECURITY_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    # Telegram Web opens Mini Apps in an iframe; the mobile apps use a webview.
    "Content-Security-Policy": (
        "frame-ancestors https://web.telegram.org https://*.telegram.org"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


@dataclass(frozen=True, slots=True)
class WebAppUser:
    """The Telegram user a verified ``initData`` names."""

    id: int
    first_name: str
    language_code: str | None
    username: str | None = None
    last_name: str | None = None


def validate_init_data(
    init_data: str,
    bot_token: str,
    *,
    max_age: int = INIT_DATA_MAX_AGE,
    now: float | None = None,
) -> WebAppUser | None:
    """Return the user when ``init_data`` is signed by ``bot_token``.

    See https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data or not bot_token:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    fields = dict(pairs)
    if len(fields) != len(pairs):
        return None
    received_hash = fields.pop("hash", None)
    if not received_hash:
        return None

    check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        return None

    try:
        auth_date = int(fields.get("auth_date", ""))
    except ValueError:
        return None
    current = time.time() if now is None else now
    if auth_date > current + 60 or current - auth_date > max_age:
        return None

    try:
        user = json.loads(fields.get("user", ""))
    except ValueError:
        return None
    if not isinstance(user, dict):
        return None
    user_id = user.get("id")
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        return None
    first_name = user.get("first_name")
    return WebAppUser(
        id=user_id,
        first_name=first_name if isinstance(first_name, str) else "",
        language_code=_str_or_none(user.get("language_code")),
        username=_str_or_none(user.get("username")),
        last_name=_str_or_none(user.get("last_name")),
    )


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def clean_webapp_url(value: object) -> str | None:
    """Return a base HTTPS URL for the Mini App, or ``None`` when empty.

    The value is the address Telegram clients use to reach Home Assistant,
    without the page path. A full Mini App URL is accepted and reduced to its
    base so the path is never appended twice.

    Raises ``ValueError`` when the value cannot be used: Telegram opens Mini
    Apps over HTTPS only, and a query or fragment would break the page path.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("webapp_url is not a string")
    text = value.strip()
    if not text:
        return None
    parts = urlsplit(text)
    if parts.scheme != "https" or not parts.netloc or parts.query or parts.fragment:
        raise ValueError("webapp_url is not an HTTPS base URL")
    base = text.rstrip("/")
    if base.endswith(WEBAPP_PATH):
        base = base[: -len(WEBAPP_PATH)].rstrip("/")
    return base


def get_webapp_url(hass: HomeAssistant, override: object = None) -> str | None:
    """Return the HTTPS URL of the Mini App, or ``None`` when there is none.

    Telegram opens Mini Apps only over HTTPS, so a plain HTTP address is not
    good enough. ``override`` is the address configured in the integration
    options; without it Home Assistant's external or cloud URL is used.
    """
    if override is not None:
        try:
            base = clean_webapp_url(override)
        except ValueError:
            return None
        return None if base is None else base + WEBAPP_PATH
    try:
        base = get_url(hass, require_ssl=True, prefer_external=True)
    except NoURLAvailableError:
        return None
    return base.rstrip("/") + WEBAPP_PATH


def _load_template() -> str:
    """Read the HTML template from the package directory."""
    return (Path(__file__).parent / "webapp.html").read_text(encoding="utf-8")


async def async_register_webapp(hass: HomeAssistant) -> None:
    """Register the Mini App views once per Home Assistant run.

    Views cannot be removed from aiohttp, so they stay registered when an entry
    unloads; they look up loaded entries on every request instead.
    """
    if hass.data.get(_DATA_REGISTERED):
        return
    template = await hass.async_add_executor_job(_load_template)
    hass.http.register_view(WebAppPageView(template))
    hass.http.register_view(WebAppApiView())
    hass.data[_DATA_REGISTERED] = True
    _LOGGER.debug("Mini App registered at %s", WEBAPP_PATH)


class WebAppPageView(HomeAssistantView):
    """Serve the Mini App page."""

    url = WEBAPP_PATH
    name = f"api:{DOMAIN}:webapp"
    requires_auth = False

    def __init__(self, template: str) -> None:
        """Keep the page in memory."""
        self._template = template

    async def get(self, request: web.Request) -> web.Response:
        """Return the page."""
        return web.Response(
            text=self._template,
            content_type="text/html",
            charset="utf-8",
            headers=_SECURITY_HEADERS,
        )


class WebAppApiView(HomeAssistantView):
    """JSON API for the Mini App, authorised by Telegram ``initData``."""

    url = WEBAPP_API_PATH
    name = f"api:{DOMAIN}:webapp:api"
    requires_auth = False

    async def post(self, request: web.Request) -> web.Response:
        """Handle one call from the page."""
        hass: HomeAssistant = request.app["hass"]
        try:
            body = await request.json()
        except ValueError:
            return _error(HTTPStatus.BAD_REQUEST, "bad_request")
        if not isinstance(body, dict):
            return _error(HTTPStatus.BAD_REQUEST, "bad_request")

        init_data = body.get("init_data")
        if not isinstance(init_data, str):
            return _error(HTTPStatus.UNAUTHORIZED, "unauthorized")
        found = _authorise(hass, init_data)
        if found is None:
            return _error(HTTPStatus.UNAUTHORIZED, "unauthorized")
        runtime, user = found

        try:
            result = await _async_dispatch(hass, runtime, user, body)
        except _ApiError as err:
            return _error(err.status, err.code)
        return web.json_response(result, headers=_SECURITY_HEADERS)


class _ApiError(Exception):
    """A refused API call."""

    def __init__(self, status: HTTPStatus, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _error(status: HTTPStatus, code: str) -> web.Response:
    return web.json_response(
        {"ok": False, "error": code}, status=status, headers=_SECURITY_HEADERS
    )


@callback
def _authorise(
    hass: HomeAssistant, init_data: str
) -> tuple[HearthkeeperRuntime, WebAppUser] | None:
    """Find the entry whose bot signed ``init_data`` and allows its user.

    A Mini App is a private conversation with the bot, so the user id is the
    chat id. Unlike plain messages, an empty allowlist does not mean "anyone".
    """
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        runtime: HearthkeeperRuntime = entry.runtime_data
        if not runtime.bot_token:
            continue
        user = validate_init_data(init_data, runtime.bot_token)
        if user is None:
            continue
        users = getattr(runtime, "users", frozenset())
        if user.id in runtime.allowed_chats and (not users or user.id in users):
            return runtime, user
        _LOGGER.debug("Mini App call from a user outside the allowlist")
        return None
    return None


async def _async_dispatch(
    hass: HomeAssistant,
    runtime: HearthkeeperRuntime,
    user: WebAppUser,
    body: Mapping[str, Any],
) -> dict[str, Any]:
    """Run one API action for the user's private chat."""
    action = body.get("action")
    brain = runtime.brain
    chat_id = user.id

    if action == "state":
        return {"ok": True, "state": _state(brain, user, runtime)}

    if action in ("preview_connection", "add_connection", "remove_connection"):
        if user.id not in getattr(runtime, "admins", ()):
            raise _ApiError(HTTPStatus.FORBIDDEN, "forbidden")
        repo = body.get("repo")
        if not isinstance(repo, str) or parse_repository(repo) is None:
            raise _ApiError(HTTPStatus.BAD_REQUEST, "bad_repo")
        repo = repo.strip()
        entry = hass.config_entries.async_get_entry(getattr(runtime, "entry_id", ""))
        if entry is None:
            raise _ApiError(HTTPStatus.CONFLICT, "not_loaded")
        repos = [r for r in entry.options.get(CONF_CONNECTION_REPOS) or () if r != repo]
        if action == "remove_connection":
            _LOGGER.info("Mini App: Telegram user %s removed plugin %s", user.id, repo)
        else:
            try:
                plugin = await runtime.connections.async_fetch(repo)
            except PluginError as err:
                raise _ApiError(HTTPStatus.BAD_REQUEST, "bad_plugin") from err
            if action == "preview_connection":
                language = brain.chat_language(chat_id)
                return {"ok": True, "plugin": plugin.payload(hass, language)}
            repos.append(repo)
            _LOGGER.info("Mini App: Telegram user %s added plugin %s", user.id, repo)
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_CONNECTION_REPOS: repos}
        )
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return {"ok": True, "reloading": True}

    if action in ("shared", "set_shared"):
        if user.id not in getattr(runtime, "admins", ()):
            raise _ApiError(HTTPStatus.FORBIDDEN, "forbidden")
        entry = hass.config_entries.async_get_entry(getattr(runtime, "entry_id", ""))
        if entry is None:
            raise _ApiError(HTTPStatus.CONFLICT, "not_loaded")
        mail_plugins = runtime.connections.integrations("mail")
        if action == "shared":
            return {
                "ok": True,
                "shared": shared_sources(hass, entry.options, mail_plugins),
            }
        key, value = body.get("key"), body.get("value")
        try:
            options = apply_shared_source(
                hass, entry.options, key, value, mail_plugins
            )
        except ValueError as err:
            raise _ApiError(HTTPStatus.BAD_REQUEST, "bad_value") from err
        _LOGGER.info(
            "Mini App: Telegram user %s changed the shared %s", user.id, key
        )
        hass.config_entries.async_update_entry(entry, options=options)
        # The runtime is built from the options; reloading applies them.
        hass.config_entries.async_schedule_reload(entry.entry_id)
        return {"ok": True, "reloading": True}

    if action == "set_mail":
        try:
            box = await brain.async_set_personal_mail(chat_id, body.get("mail"))
        except MailboxError as err:
            raise _ApiError(HTTPStatus.BAD_REQUEST, f"mail_{err.code}") from err
        return {"ok": True, "mail": box, "state": _state(brain, user, runtime)}

    if action == "add_calendar":
        try:
            item = await brain.async_add_personal_calendar(
                chat_id, body.get("name"), body.get("url")
            )
        except CalendarError as err:
            raise _ApiError(HTTPStatus.BAD_REQUEST, f"cal_{err.code}") from err
        return {"ok": True, "calendar": item, "state": _state(brain, user, runtime)}

    if action == "remove_calendar":
        await brain.async_remove_personal_calendar(chat_id, body.get("id"))
        return {"ok": True, "state": _state(brain, user, runtime)}

    if action == "remove_mail":
        await brain.async_remove_personal_mail(chat_id)
        return {"ok": True, "state": _state(brain, user, runtime)}

    if action == "connections":
        plugins = await runtime.connections.async_plugins()
        language = brain.chat_language(chat_id)
        return {
            "ok": True,
            "connections": [plugin.payload(hass, language) for plugin in plugins],
        }

    if action == "run_skill":
        skill_id = body.get("id")
        if not isinstance(skill_id, str) or not brain.has_custom_skill(
            chat_id, skill_id
        ):
            raise _ApiError(HTTPStatus.BAD_REQUEST, "bad_request")
        # The agent may take a minute; the answer arrives in the chat.
        brain.hass.async_create_background_task(
            brain.async_run_custom_skill(chat_id, skill_id),
            f"{DOMAIN} custom skill",
        )
        return {"ok": True}

    if action == "set_reply_mode":
        mode = body.get("mode")
        if mode not in REPLY_MODES:
            raise _ApiError(HTTPStatus.BAD_REQUEST, "bad_request")
        if not await brain.async_set_reply_mode(chat_id, mode):
            raise _ApiError(HTTPStatus.CONFLICT, "no_tts")
    elif action == "set_agent":
        agent_id = body.get("agent_id")
        if agent_id is not None and not isinstance(agent_id, str):
            raise _ApiError(HTTPStatus.BAD_REQUEST, "bad_request")
        if not await brain.async_set_agent(chat_id, agent_id):
            raise _ApiError(HTTPStatus.BAD_REQUEST, "unknown_agent")
    elif action == "new_conversation":
        await brain.async_new_conversation(chat_id)
    elif action == "cancel_reminders":
        brain.cancel_reminders(chat_id)
    elif action == "set_prefs":
        try:
            changed = await brain.async_update_preferences(chat_id, body.get("prefs"))
        except InvalidPreference as err:
            raise _ApiError(HTTPStatus.BAD_REQUEST, "bad_value") from err
        _LOGGER.info(
            "Mini App: Telegram user %s changed their settings: %s",
            user.id,
            ", ".join(changed),
        )
    elif action == "command":
        command = body.get("command")
        args = body.get("args") or []
        if (
            command not in WEBAPP_COMMANDS
            or not isinstance(args, list)
            or not all(isinstance(arg, str) for arg in args)
            or len(args) > 8
        ):
            raise _ApiError(HTTPStatus.BAD_REQUEST, "bad_request")
        runtime.transport.async_inject_command(chat_id, user.id, command, args)
        return {"ok": True}
    else:
        raise _ApiError(HTTPStatus.BAD_REQUEST, "bad_request")

    return {"ok": True, "state": _state(brain, user, runtime)}


# Shared sources an admin may change from the page, and their options keys.
_SHARED_KEYS: dict[str, str] = {
    "weather": CONF_BRIEF_WEATHER,
    "calendars": CONF_BRIEF_CALENDARS,
    "mail": CONF_BRIEF_MAIL,
    "todo": CONF_TODO_ENTITY,
}
_MULTI = frozenset(("calendars", "mail"))


def _candidates(
    hass: HomeAssistant, mail_integrations: set[str] | None = None
) -> dict[str, list[str]]:
    """Return every entity that can feed each shared source.

    Mail comes from IMAP sensors and from the integrations of mail plugins.
    """
    registry = er.async_get(hass)
    platforms = {"imap", *(mail_integrations or ())}
    mail = sorted(
        entry.entity_id
        for entry in registry.entities.values()
        if entry.platform in platforms and entry.domain == "sensor"
    )
    return {
        "weather": sorted(hass.states.async_entity_ids("weather")),
        "calendars": sorted(hass.states.async_entity_ids("calendar")),
        "mail": mail,
        "todo": sorted(hass.states.async_entity_ids("todo")),
    }


def shared_sources(
    hass: HomeAssistant,
    options: Mapping[str, Any],
    mail_integrations: set[str] | None = None,
) -> dict[str, Any]:
    """Return the shared sources and what an admin may pick for each."""
    def name(entity_id: str) -> str:
        state = hass.states.get(entity_id)
        friendly = state.attributes.get("friendly_name") if state else None
        return friendly if isinstance(friendly, str) and friendly else entity_id

    result: dict[str, Any] = {}
    for key, entity_ids in _candidates(hass, mail_integrations).items():
        current = options.get(_SHARED_KEYS[key])
        if key in _MULTI:
            selected = [item for item in current or () if isinstance(item, str)]
        else:
            selected = [current] if isinstance(current, str) and current else []
        result[key] = {
            "multiple": key in _MULTI,
            "selected": selected,
            "choices": [{"id": item, "name": name(item)} for item in entity_ids],
        }
    return result


def apply_shared_source(
    hass: HomeAssistant,
    options: Mapping[str, Any],
    key: object,
    value: object,
    mail_integrations: set[str] | None = None,
) -> dict[str, Any]:
    """Return new options with one shared source changed; ValueError if bad."""
    if not isinstance(key, str) or key not in _SHARED_KEYS:
        raise ValueError("key")
    allowed = set(_candidates(hass, mail_integrations)[key])
    if key in _MULTI:
        if not isinstance(value, list) or any(
            not isinstance(item, str) or item not in allowed for item in value
        ):
            raise ValueError(key)
        new_value: Any = list(dict.fromkeys(value))
    elif value in (None, ""):
        new_value = None
    elif isinstance(value, str) and value in allowed:
        new_value = value
    else:
        raise ValueError(key)
    return {**options, _SHARED_KEYS[key]: new_value}


def _state(brain: Any, user: WebAppUser, runtime: Any = None) -> dict[str, Any]:
    """Return the page state with the account the page was opened from."""
    state = dict(brain.webapp_state(user.id, user.first_name))
    state["is_admin"] = user.id in getattr(runtime, "admins", ())
    state["account"] = {
        "id": user.id,
        "name": " ".join(filter(None, (user.first_name, user.last_name))),
        "username": user.username,
    }
    return state
