"""Tests for the Telegram bus transport."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from custom_components.hearthkeeper.transport import (
    IncomingMessage,
    MessageKind,
    Transport,
    parse_event,
    split_message,
    strip_bot_mention,
)

BOT = {
    "config_entry_id": "entry-1",
    "username": "hearth_bot",
}


def _event(**extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "bot": BOT,
        "chat_id": 42,
        "id": 7,
        "user_id": 100,
    }
    data.update(extra)
    return data


def test_parse_text_event() -> None:
    """Text events become IncomingMessage."""
    message = parse_event(MessageKind.TEXT, _event(text=" hello "))

    assert message is not None
    assert message.kind is MessageKind.TEXT
    assert message.text == "hello"
    assert message.chat_id == 42
    assert message.message_id == 7


def test_parse_command_event() -> None:
    """Commands keep the slash and the argument list."""
    message = parse_event(
        MessageKind.COMMAND,
        _event(command="/voice", args=["on"]),
    )

    assert message is not None
    assert message.command == "/voice"
    assert message.args == ("on",)
    assert message.text == "/voice on"


def test_parse_attachment_event() -> None:
    """Attachments expose the file metadata and the caption."""
    message = parse_event(
        MessageKind.ATTACHMENT,
        _event(
            file_id="file-1",
            file_mime_type="audio/ogg",
            file_size=1024,
            text="caption",
        ),
    )

    assert message is not None
    assert message.attachment is not None
    assert message.attachment.file_id == "file-1"
    assert message.attachment.mime_type == "audio/ogg"
    assert message.text == "caption"


def test_parse_rejects_incomplete_events() -> None:
    """Events without a chat id or required fields are dropped."""
    assert parse_event(MessageKind.TEXT, {"text": "hi"}) is None
    assert parse_event(MessageKind.TEXT, _event()) is None
    assert parse_event(MessageKind.COMMAND, _event()) is None
    assert parse_event(MessageKind.ATTACHMENT, _event()) is None


def test_split_message_keeps_words() -> None:
    """Long replies are split on word boundaries."""
    text = "word " * 1000
    chunks = split_message(text, limit=100)

    assert len(chunks) > 1
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert "".join(chunks).replace(" ", "") == text.replace(" ", "")


def test_strip_bot_mention() -> None:
    """Mentions are removed case-insensitively."""
    assert strip_bot_mention("@Hearth_Bot turn on the light", "hearth_bot") == (
        "turn on the light"
    )
    assert strip_bot_mention("no mention", "hearth_bot") == "no mention"


def _transport(hass: HomeAssistant, **kwargs: Any) -> Transport:
    options = {
        "telegram_entry_id": "entry-1",
        "allowed_chats": set(),
        "allow_groups": False,
    }
    options.update(kwargs)
    return Transport(hass, **options)


def _message(**kwargs: Any) -> IncomingMessage:
    data: dict[str, Any] = {
        "kind": MessageKind.TEXT,
        "chat_id": 42,
        "message_id": 1,
        "text": "hello",
    }
    data.update(kwargs)
    return IncomingMessage(**data)


def test_allowlist_filter(hass: HomeAssistant) -> None:
    """Messages from chats outside the allowlist are ignored."""
    transport = _transport(hass, allowed_chats={42})

    assert transport._accept(_message(chat_id=42)) is True
    assert transport._accept(_message(chat_id=99)) is False


def test_empty_allowlist_accepts_authorised_events(hass: HomeAssistant) -> None:
    """Without chat subentries the core allowlist is the only gate."""
    transport = _transport(hass, allowed_chats=set())

    assert transport._accept(_message(chat_id=99)) is True


def test_group_chats_require_opt_in(hass: HomeAssistant) -> None:
    """Group messages are dropped unless groups are enabled."""
    disabled = _transport(hass, allow_groups=False)
    enabled = _transport(hass, allow_groups=True)
    # The username is learned from the first bus event; set it directly here.
    enabled._bot_username = "hearth_bot"

    assert disabled._accept(_message(chat_id=-100)) is False
    assert enabled._accept(_message(chat_id=-100, text="hello")) is False
    assert enabled._accept(_message(chat_id=-100, text="@hearth_bot hi")) is True
    assert (
        enabled._accept(
            _message(
                kind=MessageKind.COMMAND,
                chat_id=-100,
                command="/help",
                text="/help",
            )
        )
        is True
    )


def test_dedup_cache(hass: HomeAssistant) -> None:
    """The same message id is processed once per chat."""
    transport = _transport(hass)
    message = _message(message_id=5)

    assert transport._is_duplicate(message) is False
    assert transport._is_duplicate(message) is True
    assert transport._is_duplicate(_message(message_id=5, chat_id=43)) is False
