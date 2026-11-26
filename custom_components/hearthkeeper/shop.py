"""Shopping list skill: the ``/shop`` command and its inline buttons.

The list itself lives in a ``todo`` entity, so it is shared with the dashboard
and survives restarts. Hearthkeeper only renders it in Telegram.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .transport import CALLBACK_DATA_LIMIT, IncomingMessage, Transport

_LOGGER = logging.getLogger(__name__)

COMMAND: str = "shop"
CALLBACK_DONE: str = "shop_done:"
CALLBACK_DELETE_YES: str = "shop_del_yes:"
CALLBACK_DELETE_NO: str = "shop_del_no:"

# A confirmation button stays valid for ten minutes.
CONFIRM_TTL = timedelta(minutes=10)

_ADD_WORDS = frozenset(("add", "добавить", "+"))
_DEL_WORDS = frozenset(("del", "delete", "remove", "удалить", "-"))
_HELP_WORDS = frozenset(("help", "помощь", "info", "?"))

TEXTS: dict[str, dict[str, str]] = {
    "ru": {
        "header": "🛒 Список покупок",
        "hint": "Нажмите на товар, чтобы отметить его купленным.",
        "empty": "🎉 Список покупок пуст.",
        "added": "✅ Добавлено: {items}",
        "removed": "🗑 Удалено: {item}",
        "marked": "Готово: {item}",
        "need_items": "Не понял, что добавить. Пример: /shop add молоко, хлеб",
        "need_item": "Не понял, что удалить. Пример: /shop del молоко",
        "confirm_remove": "Удалить «{item}» из списка?",
        "confirm_yes": "✓ Удалить",
        "confirm_no": "✗ Отмена",
        "cancelled": "Отменено.",
        "expired": "Кнопка устарела, повторите /shop del.",
        "not_configured": (
            "Список покупок не настроен: выберите todo-список "
            "в настройках Hearthkeeper."
        ),
        "failed": "Не получилось обратиться к списку покупок.",
        "help": (
            "🛒 Команды списка покупок:\n"
            "/shop — показать список\n"
            "/shop add молоко, хлеб — добавить\n"
            "/shop del молоко — удалить"
        ),
    },
    "en": {
        "header": "🛒 Shopping list",
        "hint": "Tap an item to mark it as bought.",
        "empty": "🎉 The shopping list is empty.",
        "added": "✅ Added: {items}",
        "removed": "🗑 Removed: {item}",
        "marked": "Done: {item}",
        "need_items": "Nothing to add. Example: /shop add milk, bread",
        "need_item": "Nothing to remove. Example: /shop del milk",
        "confirm_remove": "Remove “{item}” from the list?",
        "confirm_yes": "🗑 Remove",
        "confirm_no": "✗ Cancel",
        "cancelled": "Cancelled.",
        "expired": "This button is stale, run /shop del again.",
        "not_configured": (
            "The shopping list is not configured: pick a todo list "
            "in the Hearthkeeper options."
        ),
        "failed": "Could not reach the shopping list.",
        "help": (
            "🛒 Shopping list commands:\n"
            "/shop — show the list\n"
            "/shop add milk, bread — add items\n"
            "/shop del milk — remove an item"
        ),
    },
}


def _clean(text: str) -> str:
    """Collapse whitespace so a todo item fits on one button."""
    return " ".join(text.split())


class ShopSkill:
    """Render and edit a todo list from Telegram."""

    def __init__(
        self,
        hass: HomeAssistant,
        transport: Transport,
        todo_entity: str | None,
    ) -> None:
        """Initialise the skill."""
        self.hass = hass
        self._transport = transport
        self._todo_entity = todo_entity
        # token -> (chat_id, item, created); buttons carry only the token.
        self._pending: dict[str, tuple[int, str, datetime]] = {}

    async def async_handle_command(
        self, message: IncomingMessage, language: str
    ) -> None:
        """Handle ``/shop`` and its subcommands."""
        texts = TEXTS.get(language, TEXTS["ru"])
        if not self._todo_entity:
            await self._reply(message, texts["not_configured"], language)
            return

        args = message.args
        subcommand = args[0].lower() if args else "list"
        payload = _clean(" ".join(args[1:])) if len(args) > 1 else ""

        if subcommand in _HELP_WORDS:
            await self._reply(message, texts["help"], language)
            return

        if subcommand in _ADD_WORDS:
            items = [_clean(part) for part in payload.split(",") if _clean(part)]
            if not items:
                await self._reply(message, texts["need_items"], language)
                return
            await self._async_add(message, items, language)
            return

        if subcommand in _DEL_WORDS:
            if not payload:
                await self._reply(message, texts["need_item"], language)
                return
            await self._async_confirm_remove(message, payload, language)
            return

        await self._async_show(message, language)

    async def async_handle_callback(
        self, message: IncomingMessage, language: str
    ) -> bool:
        """Handle a tap on a shopping list button."""
        data = message.callback_data or ""
        texts = TEXTS.get(language, TEXTS["ru"])

        if data.startswith(CALLBACK_DONE):
            # Answer first: Telegram stops the button spinner and other
            # automations that own their own callbacks stay untouched.
            await self._transport.async_answer_callback(message.callback_id)
            uid = data[len(CALLBACK_DONE) :]
            if not self._todo_entity or not uid:
                return True

            try:
                await self.hass.services.async_call(
                    "todo",
                    "update_item",
                    {
                        "entity_id": self._todo_entity,
                        "item": uid,
                        "status": "completed",
                    },
                    blocking=True,
                )
            except HomeAssistantError as err:
                _LOGGER.warning(
                    "Could not complete a todo item (%s)", type(err).__name__
                )
                return True

            await self._async_refresh_message(message, texts)
            return True

        if data.startswith(CALLBACK_DELETE_YES):
            await self._transport.async_answer_callback(message.callback_id)
            await self._async_remove_confirmed(
                message, data[len(CALLBACK_DELETE_YES) :], texts
            )
            return True

        if data.startswith(CALLBACK_DELETE_NO):
            await self._transport.async_answer_callback(message.callback_id)
            self._pending.pop(data[len(CALLBACK_DELETE_NO) :], None)
            if message.message_id is not None:
                await self._transport.async_edit_message(
                    message.chat_id, message.message_id, texts["cancelled"]
                )
            return True

        return False

    async def _async_add(
        self, message: IncomingMessage, items: list[str], language: str
    ) -> None:
        """Add items and confirm."""
        texts = TEXTS.get(language, TEXTS["ru"])
        for item in items:
            try:
                await self.hass.services.async_call(
                    "todo",
                    "add_item",
                    {"entity_id": self._todo_entity, "item": item},
                    blocking=True,
                )
            except HomeAssistantError as err:
                _LOGGER.warning("Could not add a todo item (%s)", type(err).__name__)
                await self._reply(message, texts["failed"], language)
                return
        await self._reply(
            message, texts["added"].format(items=", ".join(items)), language
        )

    async def _async_confirm_remove(
        self, message: IncomingMessage, item: str, language: str
    ) -> None:
        """Ask for confirmation before deleting an item."""
        texts = TEXTS.get(language, TEXTS["ru"])
        self._prune_pending()
        token = uuid.uuid4().hex[:12]
        self._pending[token] = (message.chat_id, item, dt_util.utcnow())
        keyboard = [
            [
                [texts["confirm_yes"], f"{CALLBACK_DELETE_YES}{token}"],
                [texts["confirm_no"], f"{CALLBACK_DELETE_NO}{token}"],
            ]
        ]
        await self._transport.async_send_text(
            message.chat_id,
            texts["confirm_remove"].format(item=item),
            thread_id=message.thread_id,
            reply_to=message.message_id if message.chat_id < 0 else None,
            keyboard=keyboard,
        )

    async def _async_remove_confirmed(
        self, message: IncomingMessage, token: str, texts: dict[str, str]
    ) -> None:
        """Delete the item after the user tapped "remove"."""
        self._prune_pending()
        pending = self._pending.pop(token, None)
        if pending is None or pending[0] != message.chat_id:
            if message.message_id is not None:
                await self._transport.async_edit_message(
                    message.chat_id, message.message_id, texts["expired"]
                )
            return

        item = pending[1]
        try:
            await self.hass.services.async_call(
                "todo",
                "remove_item",
                {"entity_id": self._todo_entity, "item": item},
                blocking=True,
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Could not remove a todo item (%s)", type(err).__name__)
            if message.message_id is not None:
                await self._transport.async_edit_message(
                    message.chat_id, message.message_id, texts["failed"]
                )
            return

        if message.message_id is not None:
            await self._transport.async_edit_message(
                message.chat_id,
                message.message_id,
                texts["removed"].format(item=item),
            )

    def _prune_pending(self) -> None:
        """Drop expired confirmation tokens."""
        cutoff = dt_util.utcnow() - CONFIRM_TTL
        for token, (_, _, created) in list(self._pending.items()):
            if created < cutoff:
                self._pending.pop(token, None)

    async def _async_show(self, message: IncomingMessage, language: str) -> None:
        """Send the list with one button per item."""
        texts = TEXTS.get(language, TEXTS["ru"])
        items = await self._async_items()
        if items is None:
            await self._reply(message, texts["failed"], language)
            return

        keyboard = self._keyboard(items)
        if not keyboard:
            await self._reply(message, texts["empty"], language)
            return

        text = f"{texts['header']}\n{texts['hint']}"
        await self._transport.async_send_text(
            message.chat_id,
            text,
            thread_id=message.thread_id,
            reply_to=message.message_id if message.chat_id < 0 else None,
            keyboard=keyboard,
        )

    async def _async_refresh_message(
        self, message: IncomingMessage, texts: dict[str, str]
    ) -> None:
        """Rewrite the message the button belongs to."""
        if message.message_id is None:
            return
        items = await self._async_items()
        if items is None:
            return
        keyboard = self._keyboard(items)
        text = f"{texts['header']}\n{texts['hint']}" if keyboard else texts["empty"]
        await self._transport.async_edit_message(
            message.chat_id, message.message_id, text, keyboard or None
        )

    async def _async_items(self) -> list[dict[str, Any]] | None:
        """Return the open items of the todo list."""
        if not self._todo_entity:
            return None
        try:
            result = await self.hass.services.async_call(
                "todo",
                "get_items",
                {"entity_id": self._todo_entity, "status": "needs_action"},
                blocking=True,
                return_response=True,
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Could not read the todo list (%s)", type(err).__name__)
            return None
        if not isinstance(result, dict):
            return None
        entity_result = result.get(self._todo_entity)
        if not isinstance(entity_result, dict):
            return None
        items = entity_result.get("items")
        if not isinstance(items, list):
            return None
        return [item for item in items if isinstance(item, dict)]

    @staticmethod
    def _keyboard(items: list[dict[str, Any]]) -> list[list[list[str]]]:
        """Build an inline keyboard with one button per item."""
        keyboard: list[list[list[str]]] = []
        for item in items:
            summary = item.get("summary")
            uid = item.get("uid")
            if not isinstance(summary, str) or not isinstance(uid, str):
                continue
            payload = f"{CALLBACK_DONE}{uid}"
            if len(payload.encode()) > CALLBACK_DATA_LIMIT:
                # Telegram would reject the whole keyboard; show the item
                # without a button instead of breaking the list.
                continue
            keyboard.append([[f"☐ {_clean(summary)}"[:64], payload]])
        return keyboard

    async def _reply(self, message: IncomingMessage, text: str, language: str) -> None:
        """Reply with plain text."""
        await self._transport.async_send_text(
            message.chat_id,
            text,
            thread_id=message.thread_id,
            reply_to=message.message_id if message.chat_id < 0 else None,
        )
