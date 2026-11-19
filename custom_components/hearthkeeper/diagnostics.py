"""Diagnostics for Hearthkeeper.

The dump is intentionally made of counts, entity ids and integration names.
It never contains message texts, audio, mail or calendar content, chat ids or
tokens.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant

from .const import CONF_BRIEF_CALENDARS, CONF_BRIEF_MAIL, CONF_BRIEF_TIME

_DEFAULT_OPTIONS_KEYS = (
    "pipeline_id",
    "reply_mode",
    "allow_groups",
    "todo_entity",
    "max_voice_seconds",
    "brief_enabled",
    "brief_time",
    "brief_weather",
    "brief_home",
)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for one config entry."""
    diagnostics: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "version": getattr(entry, "version", None),
            "source": str(getattr(entry, "source", "")),
        },
        "system": {
            "home_assistant": HA_VERSION,
            "language": hass.config.language,
            "time_zone": str(hass.config.time_zone),
        },
        "options": _safe_options(dict(entry.options)),
    }

    runtime = getattr(entry, "runtime_data", None)
    if runtime is None:
        diagnostics["runtime"] = None
        return diagnostics

    pipeline = getattr(runtime, "pipeline", None)
    briefing = getattr(runtime, "briefing", None)
    chats = list(getattr(getattr(runtime, "chats", None), "chats", {}).values())
    transport = getattr(runtime, "transport", None)
    stats = getattr(runtime, "stats", None)

    diagnostics["runtime"] = {
        "pipeline": (
            None
            if pipeline is None
            else {
                "id": pipeline.id,
                "name": pipeline.name,
                "language": pipeline.language,
                "conversation_engine": pipeline.conversation_engine,
                "stt_engine": pipeline.stt_engine,
                "tts_engine": pipeline.tts_engine,
                "tts_voice": pipeline.tts_voice,
            }
        ),
        "briefing": (
            None
            if briefing is None
            else {
                "enabled": bool(getattr(runtime, "brief_enabled", False)),
                "time": getattr(runtime, "brief_time", None),
                "calendars": len(briefing.calendars),
                "weather": briefing.weather is not None,
                "todo": briefing.todo is not None,
                "mail": len(briefing.mail),
                "home": briefing.home,
            }
        ),
        "chats": {
            "count": len(chats),
            "voice_replies": sum(1 for chat in chats if chat.reply_mode == "voice"),
            "agent_overrides": sum(1 for chat in chats if chat.agent_id),
        },
        "transport": {
            "bot_username": getattr(transport, "bot_username", None),
            "allowed_chats": len(getattr(runtime, "allowed_chats", ())),
        },
        "stats": None if stats is None else stats.as_dict(),
    }
    return diagnostics


def _safe_options(options: dict[str, Any]) -> dict[str, Any]:
    """Keep only the options that are useful for support."""
    safe: dict[str, Any] = {}
    for key in _DEFAULT_OPTIONS_KEYS:
        if key in options:
            safe[key] = options[key]
    safe["brief_calendars"] = len(_as_list(options.get(CONF_BRIEF_CALENDARS)))
    safe["brief_mail"] = len(_as_list(options.get(CONF_BRIEF_MAIL)))
    if options.get(CONF_BRIEF_TIME) is not None:
        safe[CONF_BRIEF_TIME] = options[CONF_BRIEF_TIME]
    return safe


def _as_list(value: object) -> list[Any]:
    """Return the value as a list for counting."""
    return list(value) if isinstance(value, (list, tuple)) else []


__all__ = ["async_get_config_entry_diagnostics"]
