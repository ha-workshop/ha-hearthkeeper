"""Tests for the diagnostics dump."""

from __future__ import annotations

from types import SimpleNamespace

from homeassistant.core import HomeAssistant

from custom_components.hearthkeeper.assist import PipelineSettings
from custom_components.hearthkeeper.brief import BriefingSources
from custom_components.hearthkeeper.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.hearthkeeper.stats import RuntimeStats
from custom_components.hearthkeeper.store import ChatStore

PIPELINE = PipelineSettings(
    id="pipeline-1",
    name="Home",
    language="ru",
    conversation_engine="conversation.home",
    conversation_language="ru",
    stt_engine="stt.faster_whisper",
    stt_language="ru",
    tts_engine="tts.piper",
    tts_language="ru",
    tts_voice="ru_RU-denis-medium",
)

OPTIONS = {
    "pipeline_id": "pipeline-1",
    "reply_mode": "voice",
    "brief_time": "08:00",
    "brief_calendars": ["calendar.inbox", "calendar.work"],
    "brief_mail": ["sensor.mail"],
}


async def test_diagnostics_without_runtime(hass: HomeAssistant) -> None:
    """A failed setup still returns a safe, useful dump."""
    entry = SimpleNamespace(
        title="Hearthkeeper",
        version=1,
        source="user",
        options=OPTIONS,
        runtime_data=None,
    )

    dump = await async_get_config_entry_diagnostics(hass, entry)

    assert dump["runtime"] is None
    assert dump["options"]["pipeline_id"] == "pipeline-1"
    assert dump["options"]["brief_calendars"] == 2
    assert dump["system"]["home_assistant"]


async def test_diagnostics_has_counts_but_no_chat_ids(
    hass: HomeAssistant,
) -> None:
    """The dump reports counters rather than conversations."""
    chats = ChatStore(hass)
    chats.get(987654321).reply_mode = "voice"
    chats.get(-100555).agent_id = "conversation.qwen"

    stats = RuntimeStats(text=3, voice_in=1, briefings=2)
    stats.record_error("HomeAssistantError")

    runtime = SimpleNamespace(
        pipeline=PIPELINE,
        briefing=BriefingSources(
            calendars=("calendar.inbox",),
            weather="weather.home",
            todo="todo.inbox",
            mail=("sensor.mail",),
        ),
        chats=chats,
        transport=SimpleNamespace(bot_username="hearth_bot"),
        allowed_chats={987654321},
        brief_enabled=True,
        brief_time="08:00",
        stats=stats,
    )
    entry = SimpleNamespace(
        title="Hearthkeeper",
        version=1,
        source="user",
        options=OPTIONS,
        runtime_data=runtime,
    )

    dump = await async_get_config_entry_diagnostics(hass, entry)

    assert dump["runtime"]["chats"] == {
        "count": 2,
        "voice_replies": 1,
        "agent_overrides": 1,
    }
    assert dump["runtime"]["briefing"]["calendars"] == 1
    assert dump["runtime"]["briefing"]["mail"] == 1
    assert dump["runtime"]["stats"]["errors"] == 1
    assert dump["runtime"]["stats"]["last_error"] == "HomeAssistantError"
    assert dump["runtime"]["transport"]["allowed_chats"] == 1

    rendered = repr(dump)
    assert "987654321" not in rendered
    assert "100555" not in rendered
