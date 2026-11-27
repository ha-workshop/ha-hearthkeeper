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
    CONF_USERS,
    CONF_WEBAPP_URL,
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
            CONF_ALLOW_GROUPS: True,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PIPELINE_ID] == "pipeline-1"
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
        {CONF_PIPELINE_ID: "none"},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PIPELINE_ID] is None


async def test_options_store_webapp_url(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tunnel address is normalised and stored, so /web can use it."""
    monkeypatch.setattr(config_flow, "async_list_pipelines", lambda hass: [])
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_TELEGRAM_ENTRY: "x"})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_PIPELINE_ID: "none",
            CONF_WEBAPP_URL: "https://tunnel.example.org/",
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_WEBAPP_URL] == "https://tunnel.example.org"


async def test_options_reject_non_https_webapp_url(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telegram cannot open an HTTP Mini App, so the form asks again."""
    monkeypatch.setattr(config_flow, "async_list_pipelines", lambda hass: [])
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_TELEGRAM_ENTRY: "x"})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_PIPELINE_ID: "none",
            CONF_WEBAPP_URL: "http://tunnel.example.org",
        },
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_webapp_url"}
    assert CONF_WEBAPP_URL not in entry.options


async def test_options_store_briefing_sources(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The form stores the shared sources and keeps fields moved to the app."""
    monkeypatch.setattr(config_flow, "async_list_pipelines", lambda hass: [PIPELINE])
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_TELEGRAM_ENTRY: "x"},
        options={
            CONF_REPLY_MODE: "voice",
            CONF_BRIEF_ENABLED: True,
            CONF_BRIEF_TIME: "07:30:00",
            CONF_BRIEF_WEATHER: "weather.home",
            CONF_BRIEF_HOME: False,
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_PIPELINE_ID: "pipeline-1",
            CONF_BRIEF_CALENDARS: ["calendar.inbox"],
            CONF_BRIEF_MAIL: ["sensor.mail"],
            CONF_USERS: ["42"],
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_BRIEF_CALENDARS] == ["calendar.inbox"]
    assert entry.options[CONF_BRIEF_MAIL] == ["sensor.mail"]
    assert entry.options[CONF_USERS] == ["42"]
    # Values set before the fields moved to the Mini App survive a save.
    assert entry.options[CONF_REPLY_MODE] == "voice"
    assert entry.options[CONF_BRIEF_ENABLED] is True
    assert entry.options[CONF_BRIEF_TIME] == "07:30:00"
    assert entry.options[CONF_BRIEF_WEATHER] == "weather.home"
    assert entry.options[CONF_BRIEF_HOME] is False
