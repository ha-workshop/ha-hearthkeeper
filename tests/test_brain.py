"""Tests for the conversation brain."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, SupportsResponse

from custom_components.hearthkeeper.assist import PipelineSettings
from custom_components.hearthkeeper.brain import Brain, extract_speech
from custom_components.hearthkeeper.store import ChatStore
from custom_components.hearthkeeper.transport import IncomingMessage, MessageKind

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
        self.actions: list[tuple[int, str]] = []

    async def async_send_text(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        reply_to: int | None = None,
    ) -> int | None:
        """Record a reply."""
        self.sent.append((chat_id, text))
        return 1

    async def async_send_chat_action(
        self, chat_id: int, action: str = "typing", *, thread_id: int | None = None
    ) -> None:
        """Record a chat action."""
        self.actions.append((chat_id, action))


def _message(text: str, **kwargs: Any) -> IncomingMessage:
    data: dict[str, Any] = {
        "kind": MessageKind.TEXT,
        "chat_id": 42,
        "message_id": 1,
        "text": text,
    }
    data.update(kwargs)
    return IncomingMessage(**data)


def _brain(
    hass: HomeAssistant,
    transport: FakeTransport,
    pipeline: PipelineSettings | None = PIPELINE,
) -> Brain:
    return Brain(
        hass,  # type: ignore[arg-type]
        transport,  # type: ignore[arg-type]
        ChatStore(hass),
        pipeline=pipeline,
        reply_mode="text",
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
