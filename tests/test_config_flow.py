"""Tests for the Hearthkeeper config flow."""

from __future__ import annotations

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hearthkeeper import config_flow
from custom_components.hearthkeeper.assist import PipelineSettings
from custom_components.hearthkeeper.const import (
    CONF_ALLOW_GROUPS,
    CONF_BRIEF_CALENDARS,
    CONF_BRIEF_ENABLED,
    CONF_BRIEF_HOME,
    CONF_BRIEF_MAIL,
    CONF_BRIEF_TIME,
    CONF_BRIEF_WEATHER,
    CONF_PIPELINE_ID,
    CONF_REPLY_MODE,
    CONF_TELEGRAM_ENTRY,
    DOMAIN,
)

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


async def test_aborts_without_telegram_bot(hass: HomeAssistant) -> None:
    """The flow needs a Telegram bot to attach to."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_telegram_bot"


async def test_creates_entry_for_telegram_bot(hass: HomeAssistant) -> None:
    """A Telegram bot entry becomes a Hearthkeeper entry."""
    telegram_entry = MockConfigEntry(domain="telegram_bot", title="Home bot", data={})
    telegram_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TELEGRAM_ENTRY: telegram_entry.entry_id}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_TELEGRAM_ENTRY: telegram_entry.entry_id}
    assert result["title"] == "Hearthkeeper (Home bot)"


async def test_second_entry_aborts(hass: HomeAssistant) -> None:
    """One Hearthkeeper entry per Telegram bot."""
    telegram_entry = MockConfigEntry(domain="telegram_bot", title="Home bot", data={})
    telegram_entry.add_to_hass(hass)
    MockConfigEntry(
        domain=DOMAIN,
        title="Hearthkeeper (Home bot)",
        data={CONF_TELEGRAM_ENTRY: telegram_entry.entry_id},
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_pick_pipeline(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The options flow stores the chosen Assist pipeline."""
    monkeypatch.setattr(config_flow, "async_list_pipelines", lambda hass: [PIPELINE])
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_TELEGRAM_ENTRY: "x"})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_PIPELINE_ID: "pipeline-1",
            CONF_REPLY_MODE: "voice",
            CONF_ALLOW_GROUPS: True,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PIPELINE_ID] == "pipeline-1"
    assert entry.options[CONF_REPLY_MODE] == "voice"
    assert entry.options[CONF_ALLOW_GROUPS] is True


async def test_options_pipeline_none_is_stored_as_null(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Choosing "no pipeline" means echo mode, not the literal "none"."""
    monkeypatch.setattr(config_flow, "async_list_pipelines", lambda hass: [])
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_TELEGRAM_ENTRY: "x"})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_PIPELINE_ID: "none", CONF_REPLY_MODE: "text"},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PIPELINE_ID] is None


async def test_options_store_briefing_sources(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The options flow stores the briefing time and the picked entities."""
    monkeypatch.setattr(config_flow, "async_list_pipelines", lambda hass: [PIPELINE])
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_TELEGRAM_ENTRY: "x"})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_PIPELINE_ID: "pipeline-1",
            CONF_REPLY_MODE: "text",
            CONF_BRIEF_ENABLED: True,
            CONF_BRIEF_TIME: "07:30:00",
            CONF_BRIEF_CALENDARS: ["calendar.inbox"],
            CONF_BRIEF_WEATHER: "weather.home",
            CONF_BRIEF_MAIL: ["sensor.mail"],
            CONF_BRIEF_HOME: False,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_BRIEF_ENABLED] is True
    assert entry.options[CONF_BRIEF_TIME] == "07:30:00"
    assert entry.options[CONF_BRIEF_CALENDARS] == ["calendar.inbox"]
    assert entry.options[CONF_BRIEF_WEATHER] == "weather.home"
    assert entry.options[CONF_BRIEF_MAIL] == ["sensor.mail"]
    assert entry.options[CONF_BRIEF_HOME] is False
