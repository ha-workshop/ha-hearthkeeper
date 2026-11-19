"""Tests for the conversation brain."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError

from custom_components.hearthkeeper import brain as brain_module
from custom_components.hearthkeeper.assist import PipelineSettings
from custom_components.hearthkeeper.brain import Brain, extract_speech
from custom_components.hearthkeeper.store import ChatStore
from custom_components.hearthkeeper.transport import (
    Attachment,
    IncomingMessage,
    MessageKind,
)
from custom_components.hearthkeeper.voice import PipelineReply, VoiceTooLongError

PIPELINE = PipelineSettings(
    id="pipeline-1",
    name="Home",
    language="ru",
    conversation_engine="conversation.home",
    conversation_language="ru-RU",
    stt_engine="stt.faster_whisper",
    stt_language="ru",
    tts_engine="tts.piper",
    tts_language="ru",
    tts_voice="ru_RU-denis-medium",
)


class FakeTransport:
    """Record what the brain sends without touching Telegram."""

    bot_username = "hearth_bot"

    def __init__(self) -> None:
        """Initialise the recorder."""
        self.sent: list[tuple[int, str]] = []
        self.keyboards: list[list[list[list[str]]] | None] = []
        self.actions: list[tuple[int, str]] = []
        self.downloaded: list[tuple[str, str]] = []
        self.voices: list[tuple[int, str]] = []
        self.edited: list[tuple[int, int, str]] = []
        self.answered: list[tuple[str | None, str]] = []

    async def async_send_text(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        reply_to: int | None = None,
        keyboard: list[list[list[str]]] | None = None,
    ) -> int | None:
        """Record a reply."""
        self.sent.append((chat_id, text))
        self.keyboards.append(keyboard)
        return 1

    async def async_send_chat_action(
        self, chat_id: int, action: str = "typing", *, thread_id: int | None = None
    ) -> None:
        """Record a chat action."""
        self.actions.append((chat_id, action))

    async def async_download_file(self, file_id: str, *, file_name: str) -> str | None:
        """Pretend to download a voice note."""
        self.downloaded.append((file_id, file_name))
        return f"/config/telegram_bot/{file_name}"

    async def async_send_voice(
        self,
        chat_id: int,
        file_path: str,
        *,
        thread_id: int | None = None,
        reply_to: int | None = None,
    ) -> bool:
        """Record a voice note."""
        self.voices.append((chat_id, file_path))
        return True

    async def async_edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: list[list[list[str]]] | None = None,
    ) -> bool:
        """Record an edit."""
        self.edited.append((chat_id, message_id, text))
        return True

    async def async_answer_callback(
        self, callback_id: str | None, text: str = ""
    ) -> None:
        """Record an answered callback."""
        self.answered.append((callback_id, text))


def _message(text: str, **kwargs: Any) -> IncomingMessage:
    data: dict[str, Any] = {
        "kind": MessageKind.TEXT,
        "chat_id": 42,
        "message_id": 1,
        "text": text,
    }
    data.update(kwargs)
    return IncomingMessage(**data)


def _voice(**kwargs: Any) -> IncomingMessage:
    data: dict[str, Any] = {
        "kind": MessageKind.ATTACHMENT,
        "chat_id": 42,
        "message_id": 7,
        "attachment": Attachment(
            file_id="file-1", mime_type="audio/ogg", file_size=1024
        ),
    }
    data.update(kwargs)
    return IncomingMessage(**data)


def _brain(
    hass: HomeAssistant,
    transport: FakeTransport,
    pipeline: PipelineSettings | None = PIPELINE,
    reply_mode: str = "text",
) -> Brain:
    return Brain(
        hass,  # type: ignore[arg-type]
        transport,  # type: ignore[arg-type]
        ChatStore(hass),
        pipeline=pipeline,
        reply_mode=reply_mode,
    )


def test_extract_plain_speech() -> None:
    """The plain speech is returned from a conversation response."""
    response = {
        "response": {"speech": {"plain": {"speech": " Light is on "}}},
        "conversation_id": "tg:1",
    }

    assert extract_speech(response) == "Light is on"


def test_extract_speech_handles_missing_parts() -> None:
    """Malformed responses do not raise."""
    assert extract_speech(None) == ""
    assert extract_speech({}) == ""
    assert extract_speech({"response": {"speech": {}}}) == ""
    assert extract_speech({"response": {"speech": {"plain": {}}}}) == ""


async def test_echo_without_pipeline(hass: HomeAssistant) -> None:
    """Without a pipeline the brain echoes the text back."""
    transport = FakeTransport()
    brain = _brain(hass, transport, pipeline=None)

    await brain.async_handle(_message("hello"))

    assert transport.sent == [(42, "Echo: hello")]


async def test_text_goes_to_pipeline_agent(hass: HomeAssistant) -> None:
    """Text is sent to the agent of the selected pipeline."""
    calls: list[dict[str, Any]] = []

    async def _process(call: Any) -> dict[str, Any]:
        calls.append(dict(call.data))
        return {
            "response": {"speech": {"plain": {"speech": "Light is on"}}},
            "conversation_id": "tg:42:agent",
        }

    hass.services.async_register(
        "conversation", "process", _process, supports_response=SupportsResponse.ONLY
    )
    transport = FakeTransport()
    brain = _brain(hass, transport)
    chats = brain._chats

    await brain.async_handle(_message("turn on the light"))

    assert transport.sent == [(42, "Light is on")]
    assert calls[0]["agent_id"] == "conversation.home"
    assert calls[0]["language"] == "ru-RU"
    assert calls[0]["conversation_id"] == "tg:42"
    assert chats.get(42).conversation_id == "tg:42:agent"


async def test_voice_command_changes_reply_mode(hass: HomeAssistant) -> None:
    """``/voice on`` stores the per-chat reply mode."""
    transport = FakeTransport()
    brain = _brain(hass, transport)
    chats = brain._chats

    await brain.async_handle(
        _message(
            "/voice on",
            kind=MessageKind.COMMAND,
            command="/voice",
            args=("on",),
        )
    )

    assert chats.get(42).reply_mode == "voice"
    assert transport.sent[0][1].startswith("Режим ответа: voice")


async def test_foreign_command_is_ignored(hass: HomeAssistant) -> None:
    """Commands owned by other automations do not get a reply."""
    transport = FakeTransport()
    brain = _brain(hass, transport)

    await brain.async_handle(
        _message("/video", kind=MessageKind.COMMAND, command="/video", args=())
    )

    assert transport.sent == []


NO_STT_PIPELINE = PipelineSettings(
    id="pipeline-2",
    name="Text only",
    language="ru",
    conversation_engine="conversation.home",
    conversation_language="ru",
    stt_engine=None,
    stt_language=None,
    tts_engine="tts.piper",
    tts_language="ru",
    tts_voice=None,
)

NO_TTS_PIPELINE = PipelineSettings(
    id="pipeline-3",
    name="No tts",
    language="ru",
    conversation_engine="conversation.home",
    conversation_language="ru",
    stt_engine="stt.faster_whisper",
    stt_language="ru",
    tts_engine=None,
    tts_language=None,
    tts_voice=None,
)


async def test_voice_message_goes_through_pipeline(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A voice note is downloaded, decoded and answered as text."""
    seen: dict[str, Any] = {}

    async def _transcode(
        hass_: HomeAssistant, path: Path, *, max_seconds: int
    ) -> bytes:
        seen["path"] = path
        seen["max_seconds"] = max_seconds
        return b"RIFF"

    async def _pipeline(hass_: HomeAssistant, **kwargs: Any) -> PipelineReply:
        seen["pipeline"] = kwargs
        return PipelineReply(text="Свет включён", conversation_id="tg:42:voice")

    monkeypatch.setattr(brain_module, "async_transcode_to_wav", _transcode)
    monkeypatch.setattr(brain_module, "async_run_audio_pipeline", _pipeline)

    transport = FakeTransport()
    brain = _brain(hass, transport)

    await brain.async_handle(_voice())

    assert transport.downloaded == [("file-1", "hk_42_7.ogg")]
    assert seen["path"] == Path("/config/telegram_bot/hk_42_7.ogg")
    assert seen["max_seconds"] == 120
    assert seen["pipeline"]["conversation_id"] == "tg:42"
    assert seen["pipeline"]["language"] == "ru"
    assert transport.sent == [(42, "Свет включён")]
    assert brain._chats.get(42).conversation_id == "tg:42:voice"


async def test_long_voice_message_is_refused(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A voice note over the limit gets a clear answer instead of silence."""

    async def _transcode(
        hass_: HomeAssistant, path: Path, *, max_seconds: int
    ) -> bytes:
        raise VoiceTooLongError("too long")

    monkeypatch.setattr(brain_module, "async_transcode_to_wav", _transcode)

    transport = FakeTransport()
    brain = _brain(hass, transport)

    await brain.async_handle(_voice())

    assert "слишком длинное" in transport.sent[0][1]
    assert transport.sent[0][1].endswith("120 с.")


async def test_voice_message_without_stt(hass: HomeAssistant) -> None:
    """Without speech-to-text Hearthkeeper explains what to configure."""
    transport = FakeTransport()
    brain = _brain(hass, transport, pipeline=NO_STT_PIPELINE)

    await brain.async_handle(_voice())

    assert "распознаванием речи" in transport.sent[0][1]
    assert transport.downloaded == []


async def test_non_audio_attachment(hass: HomeAssistant) -> None:
    """Photos still get the explanation, not speech recognition."""
    transport = FakeTransport()
    brain = _brain(hass, transport)

    await brain.async_handle(
        _voice(
            attachment=Attachment(
                file_id="photo-1", mime_type="image/jpeg", file_size=2048
            )
        )
    )

    assert "отправьте текст" in transport.sent[0][1]
    assert transport.downloaded == []


async def test_answer_is_spoken_in_voice_mode(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Voice mode synthesizes the answer and sends it as a voice note."""
    discarded: list[Path] = []

    async def _process(call: Any) -> dict[str, Any]:
        return {
            "response": {"speech": {"plain": {"speech": "Готово"}}},
            "conversation_id": "tg:42",
        }

    async def _stage(hass_: HomeAssistant, pipeline: Any, text: str) -> Path:
        return Path("/media/hearthkeeper/answer.ogg")

    async def _discard(hass_: HomeAssistant, path: Path) -> None:
        discarded.append(path)

    hass.services.async_register(
        "conversation", "process", _process, supports_response=SupportsResponse.ONLY
    )
    monkeypatch.setattr(brain_module, "async_stage_voice_reply", _stage)
    monkeypatch.setattr(brain_module, "async_discard_file", _discard)

    transport = FakeTransport()
    brain = _brain(hass, transport, reply_mode="voice")

    await brain.async_handle(_message("как дела"))

    assert transport.voices == [(42, "/media/hearthkeeper/answer.ogg")]
    assert transport.sent == []
    assert discarded == [Path("/media/hearthkeeper/answer.ogg")]
    assert "record_voice" in [action for _, action in transport.actions]


async def test_tts_failure_falls_back_to_text(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken TTS engine must not swallow the answer."""

    async def _process(call: Any) -> dict[str, Any]:
        return {
            "response": {"speech": {"plain": {"speech": "Готово"}}},
            "conversation_id": "tg:42",
        }

    async def _stage(hass_: HomeAssistant, pipeline: Any, text: str) -> Path:
        raise HomeAssistantError("tts broke")

    hass.services.async_register(
        "conversation", "process", _process, supports_response=SupportsResponse.ONLY
    )
    monkeypatch.setattr(brain_module, "async_stage_voice_reply", _stage)

    transport = FakeTransport()
    brain = _brain(hass, transport, reply_mode="voice")

    await brain.async_handle(_message("как дела"))

    assert transport.voices == []
    assert transport.sent == [(42, "Готово")]


async def test_voice_command_requires_tts(hass: HomeAssistant) -> None:
    """``/voice on`` explains a pipeline without text-to-speech."""
    transport = FakeTransport()
    brain = _brain(hass, transport, pipeline=NO_TTS_PIPELINE)
    chats = brain._chats

    await brain.async_handle(
        _message("/voice on", kind=MessageKind.COMMAND, command="/voice", args=("on",))
    )

    assert "нет синтеза речи" in transport.sent[0][1]
    assert chats.get(42).reply_mode is None


async def test_both_mode_sends_text_first(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``both`` delivers text immediately and then the voice note."""

    async def _process(call: Any) -> dict[str, Any]:
        return {
            "response": {"speech": {"plain": {"speech": "Готово"}}},
            "conversation_id": "tg:42",
        }

    async def _stage(hass_: HomeAssistant, pipeline: Any, text: str) -> Path:
        assert transport.sent == [(42, "Готово")]
        return Path("/media/hearthkeeper/answer.ogg")

    async def _discard(hass_: HomeAssistant, path: Path) -> None:
        return None

    hass.services.async_register(
        "conversation", "process", _process, supports_response=SupportsResponse.ONLY
    )
    monkeypatch.setattr(brain_module, "async_stage_voice_reply", _stage)
    monkeypatch.setattr(brain_module, "async_discard_file", _discard)

    transport = FakeTransport()
    brain = _brain(hass, transport, reply_mode="both")

    await brain.async_handle(_message("как дела"))

    assert transport.sent == [(42, "Готово")]
    assert transport.voices == [(42, "/media/hearthkeeper/answer.ogg")]


async def test_brief_command_builds_the_briefing(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``/brief`` answers with the generated summary."""

    async def _brief(hass_: HomeAssistant, sources: Any, language: str) -> str:
        assert language == "ru"
        return "Сводка дня"

    monkeypatch.setattr(brain_module, "async_build_briefing", _brief)

    transport = FakeTransport()
    brain = _brain(hass, transport)

    await brain.async_handle(
        _message("/brief", kind=MessageKind.COMMAND, command="/brief", args=())
    )

    assert transport.sent == [(42, "Сводка дня")]


async def test_scheduled_briefing_skips_groups(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daily briefing goes to private chats only."""

    async def _brief(hass_: HomeAssistant, sources: Any, language: str) -> str:
        return "Сводка дня"

    monkeypatch.setattr(brain_module, "async_build_briefing", _brief)

    transport = FakeTransport()
    brain = _brain(hass, transport)
    brain._chats.get(42)
    brain._chats.get(-100500)

    await brain.async_send_briefing()

    assert transport.sent == [(42, "Сводка дня")]


def _callback(data: str, message_id: int = 99) -> IncomingMessage:
    return IncomingMessage(
        kind=MessageKind.CALLBACK,
        chat_id=42,
        message_id=message_id,
        callback_id="777",
        callback_data=data,
    )


def _agents(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "conversation.home", "unknown", {"friendly_name": "Home Assistant"}
    )
    hass.states.async_set("conversation.qwen", "unknown", {"friendly_name": "Qwen"})
    hass.states.async_set("conversation.off", "unavailable", {"friendly_name": "Off"})


async def test_model_lists_agents_with_buttons(hass: HomeAssistant) -> None:
    """``/model`` shows every agent and marks the current one."""
    _agents(hass)
    transport = FakeTransport()
    brain = _brain(hass, transport)

    await brain.async_handle(
        _message("/model", kind=MessageKind.COMMAND, command="/model", args=())
    )

    assert "Сейчас: Home Assistant" in transport.sent[0][1]
    keyboard = transport.keyboards[0]
    assert keyboard is not None
    assert keyboard[0][0] == ["✅ Home Assistant", "hk_agent:0"]
    assert keyboard[1][0] == ["Qwen", "hk_agent:1"]
    assert keyboard[2][0][1] == "hk_agent:default"


async def test_model_callback_switches_agent(hass: HomeAssistant) -> None:
    """A tap switches the agent and starts a new conversation."""
    _agents(hass)
    transport = FakeTransport()
    brain = _brain(hass, transport)
    chat = brain._chats.get(42)
    chat.conversation_id = "old-context"

    await brain.async_handle(
        _message("/model", kind=MessageKind.COMMAND, command="/model", args=())
    )
    await brain.async_handle(_callback("hk_agent:1"))

    assert transport.answered == [("777", "")]
    assert transport.edited == [(42, 99, "Агент: Qwen. Начал новый разговор.")]
    assert chat.agent_id == "conversation.qwen"
    assert chat.conversation_id == "tg:42"


async def test_model_callback_default(hass: HomeAssistant) -> None:
    """The default row clears the per-chat override."""
    _agents(hass)
    transport = FakeTransport()
    brain = _brain(hass, transport)
    brain._chats.get(42).agent_id = "conversation.qwen"

    await brain.async_handle(_callback("hk_agent:default"))

    assert brain._chats.get(42).agent_id is None
    assert transport.edited[0][2].startswith("Вернул агента")


async def test_model_callback_is_stale(hass: HomeAssistant) -> None:
    """A button from an old session explains itself instead of failing."""
    transport = FakeTransport()
    brain = _brain(hass, transport)

    await brain.async_handle(_callback("hk_agent:5"))

    assert transport.answered == [("777", "Список устарел, откройте /model заново.")]
    assert transport.edited == []


async def test_model_switch_by_name(hass: HomeAssistant) -> None:
    """``/model <name>`` works without buttons."""
    _agents(hass)
    transport = FakeTransport()
    brain = _brain(hass, transport)

    await brain.async_handle(
        _message(
            "/model Qwen",
            kind=MessageKind.COMMAND,
            command="/model",
            args=("Qwen",),
        )
    )

    assert brain._chats.get(42).agent_id == "conversation.qwen"
    assert "Агент: Qwen" in transport.sent[0][1]


async def test_model_unknown_name(hass: HomeAssistant) -> None:
    """An unknown name keeps the current agent."""
    _agents(hass)
    transport = FakeTransport()
    brain = _brain(hass, transport)

    await brain.async_handle(
        _message(
            "/model nope",
            kind=MessageKind.COMMAND,
            command="/model",
            args=("nope",),
        )
    )

    assert brain._chats.get(42).agent_id is None
    assert "Не понял" in transport.sent[0][1]


async def test_say_service_defaults_to_chat_mode(hass: HomeAssistant) -> None:
    """``hearthkeeper.say`` without a mode follows the chat."""
    transport = FakeTransport()
    brain = _brain(hass, transport, reply_mode="text")

    await brain.async_say(42, "Проверка")

    assert transport.sent == [(42, "Проверка")]
    assert transport.voices == []


async def test_say_service_can_speak(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mode: voice`` speaks even when the chat prefers text."""

    async def _stage(hass_: HomeAssistant, pipeline: Any, text: str) -> Path:
        return Path("/media/hearthkeeper/say.ogg")

    discarded: list[Path] = []

    async def _discard(hass_: HomeAssistant, path: Path) -> None:
        discarded.append(path)

    monkeypatch.setattr(brain_module, "async_stage_voice_reply", _stage)
    monkeypatch.setattr(brain_module, "async_discard_file", _discard)

    transport = FakeTransport()
    brain = _brain(hass, transport, reply_mode="text")

    await brain.async_say(42, "Проверка", "voice")

    assert transport.voices == [(42, "/media/hearthkeeper/say.ogg")]
    assert transport.sent == []
    assert discarded == [Path("/media/hearthkeeper/say.ogg")]
