"""Hearthkeeper: a Telegram assistant for Home Assistant."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import (
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)

from .assist import PipelineSettings, async_get_pipeline_settings
from .brain import Brain
from .const import (
    ATTR_CHAT_ID,
    ATTR_TEXT,
    CONF_ALLOW_GROUPS,
    CONF_PIPELINE_ID,
    CONF_REPLY_MODE,
    CONF_TELEGRAM_ENTRY,
    DEFAULT_ALLOW_GROUPS,
    DEFAULT_OPTIONS,
    DEFAULT_REPLY_MODE,
    DOMAIN,
    SERVICE_BRIEF,
    SERVICE_RESET,
    SERVICE_SAY,
    TELEGRAM_DOMAIN,
    TELEGRAM_SUBENTRY_ALLOWED_CHATS,
)
from .store import ChatStore
from .transport import Transport

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class HearthkeeperRuntime:
    """Objects that live as long as the config entry."""

    telegram_entry_id: str
    allowed_chats: set[int]
    pipeline: PipelineSettings | None
    transport: Transport
    brain: Brain
    chats: ChatStore

    def handles_chat(self, chat_id: int) -> bool:
        """Return True when the chat belongs to this entry."""
        return not self.allowed_chats or chat_id in self.allowed_chats


type HearthkeeperConfigEntry = ConfigEntry[HearthkeeperRuntime]


async def async_setup_entry(
    hass: HomeAssistant, entry: HearthkeeperConfigEntry
) -> bool:
    """Set up Hearthkeeper from a config entry."""
    telegram_entry_id = entry.data.get(CONF_TELEGRAM_ENTRY)
    telegram_entry = (
        hass.config_entries.async_get_entry(telegram_entry_id)
        if isinstance(telegram_entry_id, str)
        else None
    )
    if telegram_entry is None or telegram_entry.domain != TELEGRAM_DOMAIN:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="telegram_entry_missing"
        )
    if telegram_entry.state is not ConfigEntryState.LOADED:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="telegram_entry_not_loaded"
        )

    allowed_chats = _allowed_chats(telegram_entry)
    if not allowed_chats:
        _LOGGER.warning(
            "No allowed chats found in %s; Hearthkeeper will accept only "
            "messages the core integration already authorised",
            telegram_entry.title,
        )

    options = dict(DEFAULT_OPTIONS) | dict(entry.options)
    pipeline = _resolve_pipeline(hass, options.get(CONF_PIPELINE_ID))
    if pipeline is None:
        _LOGGER.info(
            "No Assist pipeline selected; Hearthkeeper will echo messages back"
        )

    chats = ChatStore(hass)
    await chats.async_load()

    transport = Transport(
        hass,
        telegram_entry_id=telegram_entry.entry_id,
        allowed_chats=allowed_chats,
        allow_groups=bool(options.get(CONF_ALLOW_GROUPS, DEFAULT_ALLOW_GROUPS)),
    )
    brain = Brain(
        hass,
        transport,
        chats,
        pipeline=pipeline,
        reply_mode=_str_or_none(options.get(CONF_REPLY_MODE)) or DEFAULT_REPLY_MODE,
    )
    transport.set_handler(brain.async_handle)

    entry.runtime_data = HearthkeeperRuntime(
        telegram_entry_id=telegram_entry.entry_id,
        allowed_chats=allowed_chats,
        pipeline=pipeline,
        transport=transport,
        brain=brain,
        chats=chats,
    )

    transport.async_start()
    entry.async_on_unload(transport.async_stop)
    _async_register_services(hass)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: HearthkeeperConfigEntry
) -> bool:
    """Unload a config entry."""
    return True


def _resolve_pipeline(
    hass: HomeAssistant, raw_pipeline_id: object
) -> PipelineSettings | None:
    """Resolve the selected Assist pipeline."""
    pipeline_id = _str_or_none(raw_pipeline_id)
    if pipeline_id is None:
        return None

    from homeassistant.components.assist_pipeline import PipelineNotFound

    try:
        return async_get_pipeline_settings(hass, pipeline_id)
    except KeyError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="pipeline_not_ready"
        ) from err
    except PipelineNotFound as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="pipeline_not_found",
            translation_placeholders={"pipeline_id": pipeline_id},
        ) from err


def _allowed_chats(telegram_entry: ConfigEntry) -> set[int]:
    """Read the chat allowlist from the core telegram_bot subentries."""
    chats: set[int] = set()
    for subentry in telegram_entry.subentries.values():
        if subentry.subentry_type != TELEGRAM_SUBENTRY_ALLOWED_CHATS:
            continue
        chat_id = subentry.data.get("chat_id")
        if isinstance(chat_id, int):
            chats.add(chat_id)
    return chats


def _str_or_none(value: object) -> str | None:
    """Return the value when it is a non-empty string."""
    return value if isinstance(value, str) and value else None


def _as_int(value: object) -> int | None:
    """Coerce a service argument to an integer chat id."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lstrip("-").isdigit():
            return int(stripped)
    return None


def _loaded_runtimes(hass: HomeAssistant) -> list[HearthkeeperRuntime]:
    """Return runtimes of all loaded Hearthkeeper entries."""
    return [
        entry.runtime_data for entry in hass.config_entries.async_loaded_entries(DOMAIN)
    ]


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register component services once."""
    handlers: dict[str, Callable[[ServiceCall], Awaitable[None]]] = {
        SERVICE_SAY: _async_handle_say,
        SERVICE_RESET: _async_handle_reset,
        SERVICE_BRIEF: _async_handle_brief,
    }
    for service, handler in handlers.items():
        if not hass.services.has_service(DOMAIN, service):
            hass.services.async_register(DOMAIN, service, handler)


async def _async_handle_say(call: ServiceCall) -> None:
    """Send a message to a chat."""
    chat_id = _as_int(call.data.get(ATTR_CHAT_ID))
    text = call.data.get(ATTR_TEXT)
    if chat_id is None or not isinstance(text, str) or not text.strip():
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_service_data"
        )

    for runtime in _loaded_runtimes(call.hass):
        if runtime.handles_chat(chat_id):
            await runtime.transport.async_send_text(chat_id, text)
            return

    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="unknown_chat",
        translation_placeholders={"chat_id": str(chat_id)},
    )


async def _async_handle_reset(call: ServiceCall) -> None:
    """Forget the conversation context of one or every chat."""
    raw_chat_id = call.data.get(ATTR_CHAT_ID)
    chat_id = _as_int(raw_chat_id) if raw_chat_id is not None else None
    if raw_chat_id is not None and chat_id is None:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_service_data"
        )

    runtimes = _loaded_runtimes(call.hass)
    if not runtimes:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_entries"
        )

    for runtime in runtimes:
        if chat_id is None or runtime.handles_chat(chat_id):
            await runtime.chats.async_reset(chat_id)


async def _async_handle_brief(call: ServiceCall) -> None:
    """Send the daily briefing. Implemented in M3."""
    raise HomeAssistantError(
        translation_domain=DOMAIN, translation_key="not_implemented"
    )
