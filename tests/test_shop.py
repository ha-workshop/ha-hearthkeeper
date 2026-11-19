"""Tests for the /shop skill."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, SupportsResponse

from custom_components.hearthkeeper.shop import ShopSkill
from custom_components.hearthkeeper.transport import IncomingMessage, MessageKind

ITEM_A = {"uid": "uid-a", "summary": "Молоко", "status": "needs_action"}
ITEM_B = {"uid": "uid-b", "summary": "Хлеб", "status": "needs_action"}


class FakeTransport:
    """Record Telegram calls without a real bot."""

    def __init__(self) -> None:
        """Initialise the recorder."""
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.answered: list[str | None] = []

    async def async_send_text(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        reply_to: int | None = None,
        keyboard: list[list[list[str]]] | None = None,
    ) -> int | None:
        """Record a message."""
        self.sent.append({"chat_id": chat_id, "text": text, "keyboard": keyboard})
        return 1

    async def async_edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: list[list[list[str]]] | None = None,
    ) -> bool:
        """Record an edit."""
        self.edited.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return True

    async def async_answer_callback(
        self, callback_id: str | None, text: str = ""
    ) -> None:
        """Record a callback answer."""
        self.answered.append(callback_id)


def _command(*args: str) -> IncomingMessage:
    return IncomingMessage(
        kind=MessageKind.COMMAND,
        chat_id=42,
        message_id=10,
        text=" ".join(("/shop", *args)),
        command="/shop",
        args=args,
    )


def _callback(data: str, message_id: int | None = 10) -> IncomingMessage:
    return IncomingMessage(
        kind=MessageKind.CALLBACK,
        chat_id=42,
        message_id=message_id,
        callback_id="555",
        callback_data=data,
    )


def _todo_services(hass: HomeAssistant, items: list[dict[str, Any]]) -> list[dict]:
    """Register fake todo services and return the recorded calls."""
    calls: list[dict[str, Any]] = []

    async def _get_items(call: Any) -> dict[str, Any]:
        return {"todo.inbox": {"items": items}}

    async def _record(call: Any) -> None:
        calls.append(dict(call.data))

    hass.services.async_register(
        "todo", "get_items", _get_items, supports_response=SupportsResponse.ONLY
    )
    hass.services.async_register("todo", "add_item", _record)
    hass.services.async_register("todo", "remove_item", _record)
    hass.services.async_register("todo", "update_item", _record)
    return calls


def _skill(
    hass: HomeAssistant, transport: FakeTransport, entity: str | None
) -> ShopSkill:
    return ShopSkill(hass, transport, entity)  # type: ignore[arg-type]


async def test_shop_lists_items_with_buttons(hass: HomeAssistant) -> None:
    """The list renders one inline button per item."""
    transport = FakeTransport()
    _todo_services(hass, [ITEM_A, ITEM_B])
    skill = _skill(hass, transport, "todo.inbox")

    await skill.async_handle_command(_command(), "ru")

    assert len(transport.sent) == 1
    keyboard = transport.sent[0]["keyboard"]
    assert keyboard == [
        [["❌ Молоко", "shop_done:uid-a"]],
        [["❌ Хлеб", "shop_done:uid-b"]],
    ]


async def test_shop_empty_list(hass: HomeAssistant) -> None:
    """An empty list says so and has no keyboard."""
    transport = FakeTransport()
    _todo_services(hass, [])
    skill = _skill(hass, transport, "todo.inbox")

    await skill.async_handle_command(_command(), "ru")

    assert "пуст" in transport.sent[0]["text"]
    assert transport.sent[0]["keyboard"] is None


async def test_shop_without_todo_entity(hass: HomeAssistant) -> None:
    """Without a configured todo entity the command explains itself."""
    transport = FakeTransport()
    skill = _skill(hass, transport, None)

    await skill.async_handle_command(_command(), "ru")

    assert "не настроен" in transport.sent[0]["text"]


async def test_shop_add_splits_items(hass: HomeAssistant) -> None:
    """``/shop add`` adds every comma separated item."""
    transport = FakeTransport()
    calls = _todo_services(hass, [])
    skill = _skill(hass, transport, "todo.inbox")

    await skill.async_handle_command(_command("add", "молоко,", "хлеб"), "ru")

    assert [call["item"] for call in calls] == ["молоко", "хлеб"]
    assert "Добавлено" in transport.sent[0]["text"]


async def test_shop_callback_completes_item(hass: HomeAssistant) -> None:
    """A tap marks the item done, answers the query and edits the message."""
    transport = FakeTransport()
    calls = _todo_services(hass, [ITEM_B])
    skill = _skill(hass, transport, "todo.inbox")

    handled = await skill.async_handle_callback(_callback("shop_done:uid-a"), "ru")

    assert handled is True
    assert transport.answered == ["555"]
    assert calls[0]["item"] == "uid-a"
    assert calls[0]["status"] == "completed"
    assert transport.edited[0]["message_id"] == 10
    assert transport.edited[0]["text"].startswith("🛒")


async def test_foreign_callback_is_ignored(hass: HomeAssistant) -> None:
    """Callbacks of other automations are neither answered nor edited."""
    transport = FakeTransport()
    skill = _skill(hass, transport, "todo.inbox")

    handled = await skill.async_handle_callback(_callback("/video_abc"), "ru")

    assert handled is False
    assert transport.answered == []
    assert transport.edited == []


async def test_shop_del_asks_for_confirmation(hass: HomeAssistant) -> None:
    """Deleting is confirmed with inline buttons, not done at once."""
    transport = FakeTransport()
    calls = _todo_services(hass, [ITEM_A])
    skill = _skill(hass, transport, "todo.inbox")

    await skill.async_handle_command(_command("del", "молоко"), "ru")

    assert calls == []
    assert transport.sent[0]["text"] == "Удалить «молоко» из списка?"
    keyboard = transport.sent[0]["keyboard"]
    assert keyboard is not None
    assert keyboard[0][0][1].startswith("shop_del_yes:")
    assert keyboard[0][1][1].startswith("shop_del_no:")


async def test_shop_del_confirmed_removes_item(hass: HomeAssistant) -> None:
    """The yes button removes the item and rewrites the question."""
    transport = FakeTransport()
    calls = _todo_services(hass, [ITEM_A])
    skill = _skill(hass, transport, "todo.inbox")

    await skill.async_handle_command(_command("del", "молоко"), "ru")
    payload = transport.sent[0]["keyboard"][0][0][1]
    handled = await skill.async_handle_callback(_callback(payload), "ru")

    assert handled is True
    assert transport.answered == ["555"]
    assert calls[0]["item"] == "молоко"
    assert transport.edited[0]["text"] == "🗑 Удалено: молоко"


async def test_shop_del_cancelled(hass: HomeAssistant) -> None:
    """The no button keeps the item."""
    transport = FakeTransport()
    calls = _todo_services(hass, [ITEM_A])
    skill = _skill(hass, transport, "todo.inbox")

    await skill.async_handle_command(_command("del", "молоко"), "ru")
    payload = transport.sent[0]["keyboard"][0][1][1]
    await skill.async_handle_callback(_callback(payload), "ru")

    assert calls == []
    assert transport.edited[0]["text"] == "Отменено."


async def test_shop_del_expired_token(hass: HomeAssistant) -> None:
    """A button after a restart cannot delete anything."""
    transport = FakeTransport()
    calls = _todo_services(hass, [ITEM_A])
    skill = _skill(hass, transport, "todo.inbox")

    await skill.async_handle_callback(_callback("shop_del_yes:deadbeef"), "ru")

    assert calls == []
    assert "устарела" in transport.edited[0]["text"]
