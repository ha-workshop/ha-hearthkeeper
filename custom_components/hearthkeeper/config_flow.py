"""Config flow for Hearthkeeper."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.translation import async_get_translations

from .assist import async_list_pipelines
from .const import (
    CONF_ADMINS,
    CONF_ALLOW_CLOUD_FOR_MAIL,
    CONF_ALLOW_GROUPS,
    CONF_BRIEF_CALENDARS,
    CONF_BRIEF_MAIL,
    CONF_KEEP_AUDIO,
    CONF_MAX_VOICE_SECONDS,
    CONF_PIPELINE_ID,
    CONF_TELEGRAM_ENTRY,
    CONF_TODO_ENTITY,
    CONF_USERS,
    CONF_WEBAPP_URL,
    DEFAULT_OPTIONS,
    DOMAIN,
    NAME,
    PIPELINE_NONE,
    TELEGRAM_DOMAIN,
)
from .webapp import clean_webapp_url

_LOGGER = logging.getLogger(__name__)


class HearthkeeperConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pick the Telegram bot that Hearthkeeper should listen to."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        telegram_entries = self.hass.config_entries.async_entries(TELEGRAM_DOMAIN)
        if not telegram_entries:
            return self.async_abort(reason="no_telegram_bot")

        configured = {
            entry.data.get(CONF_TELEGRAM_ENTRY)
            for entry in self._async_current_entries()
        }
        available = [
            entry for entry in telegram_entries if entry.entry_id not in configured
        ]
        if not available:
            return self.async_abort(reason="already_configured")

        if user_input is not None:
            entry_id = user_input.get(CONF_TELEGRAM_ENTRY)
            telegram_entry = (
                self.hass.config_entries.async_get_entry(entry_id)
                if isinstance(entry_id, str)
                else None
            )
            if telegram_entry is None or telegram_entry.domain != TELEGRAM_DOMAIN:
                errors["base"] = "invalid_telegram_entry"
            else:
                await self.async_set_unique_id(f"telegram:{telegram_entry.entry_id}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"{NAME} ({telegram_entry.title})",
                    data={CONF_TELEGRAM_ENTRY: telegram_entry.entry_id},
                    options=dict(DEFAULT_OPTIONS),
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_TELEGRAM_ENTRY): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=entry.entry_id, label=entry.title)
                            for entry in available
                        ],
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HearthkeeperOptionsFlow:
        """Return the options flow."""
        return HearthkeeperOptionsFlow()


class HearthkeeperOptionsFlow(OptionsFlowWithReload):
    """Pick the Assist pipeline and the reply behaviour."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the options step."""
        if user_input is not None:
            try:
                user_input[CONF_WEBAPP_URL] = clean_webapp_url(
                    user_input.get(CONF_WEBAPP_URL)
                )
            except ValueError:
                raw = user_input.get(CONF_WEBAPP_URL)
                return self.async_show_form(
                    step_id="init",
                    data_schema=await self._async_schema(
                        webapp_url=raw if isinstance(raw, str) and raw else None
                    ),
                    errors={"base": "invalid_webapp_url"},
                )

            pipeline_id = user_input.get(CONF_PIPELINE_ID)
            user_input[CONF_PIPELINE_ID] = (
                None if pipeline_id in (None, "", PIPELINE_NONE) else pipeline_id
            )
            # Fields moved to the Mini App are no longer in the form; keep
            # their saved values instead of wiping them on every save.
            return self.async_create_entry(
                data={**self.config_entry.options, **user_input}
            )

        return self.async_show_form(
            step_id="init", data_schema=await self._async_schema()
        )

    async def _async_schema(self, webapp_url: str | None = None) -> vol.Schema:
        """Build the options form; ``webapp_url`` keeps a rejected value."""
        options = dict(DEFAULT_OPTIONS) | dict(self.config_entry.options)
        pipelines = async_list_pipelines(self.hass)
        selected = options.get(CONF_PIPELINE_ID)
        if selected and selected not in {pipeline.id for pipeline in pipelines}:
            # The pipeline was deleted in Assist; show "none" instead of a
            # value the user cannot see in the list.
            selected = None
        none_label = await self._async_none_label()

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_PIPELINE_ID, default=selected or PIPELINE_NONE
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=pipeline.id, label=pipeline.label)
                            for pipeline in pipelines
                        ]
                        + [SelectOptionDict(value=PIPELINE_NONE, label=none_label)],
                        mode=SelectSelectorMode.LIST,
                        translation_key="pipeline",
                    )
                ),
                vol.Optional(
                    CONF_USERS,
                    description={
                        "suggested_value": [
                            str(cid) for cid in options.get(CONF_USERS) or ()
                        ]
                    },
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": str(cid), "label": str(cid)}
                            for cid in sorted(self._known_private_chats())
                        ],
                        multiple=True,
                        mode=SelectSelectorMode.DROPDOWN,
                        custom_value=True,
                    )
                ),
                vol.Optional(
                    CONF_ALLOW_GROUPS,
                    default=options.get(
                        CONF_ALLOW_GROUPS, DEFAULT_OPTIONS[CONF_ALLOW_GROUPS]
                    ),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_WEBAPP_URL,
                    description={
                        "suggested_value": webapp_url
                        if webapp_url is not None
                        else options.get(CONF_WEBAPP_URL)
                    },
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
                vol.Optional(
                    CONF_TODO_ENTITY,
                    description={"suggested_value": options.get(CONF_TODO_ENTITY)},
                ): EntitySelector(EntitySelectorConfig(domain="todo")),
                vol.Optional(
                    CONF_MAX_VOICE_SECONDS,
                    default=options.get(
                        CONF_MAX_VOICE_SECONDS,
                        DEFAULT_OPTIONS[CONF_MAX_VOICE_SECONDS],
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=10,
                        max=600,
                        step=10,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                ),
                vol.Optional(
                    CONF_KEEP_AUDIO,
                    default=options.get(
                        CONF_KEEP_AUDIO, DEFAULT_OPTIONS[CONF_KEEP_AUDIO]
                    ),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_BRIEF_CALENDARS,
                    description={
                        "suggested_value": list(options.get(CONF_BRIEF_CALENDARS) or ())
                    },
                ): EntitySelector(
                    EntitySelectorConfig(domain="calendar", multiple=True)
                ),
                vol.Optional(
                    CONF_BRIEF_MAIL,
                    description={
                        "suggested_value": list(options.get(CONF_BRIEF_MAIL) or ())
                    },
                ): EntitySelector(
                    EntitySelectorConfig(
                        domain="sensor", integration="imap", multiple=True
                    )
                ),
                vol.Optional(
                    CONF_ALLOW_CLOUD_FOR_MAIL,
                    default=options.get(
                        CONF_ALLOW_CLOUD_FOR_MAIL,
                        DEFAULT_OPTIONS[CONF_ALLOW_CLOUD_FOR_MAIL],
                    ),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_ADMINS,
                    description={
                        "suggested_value": [
                            str(cid) for cid in options.get(CONF_ADMINS) or ()
                        ]
                    },
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": str(cid), "label": str(cid)}
                            for cid in sorted(self._known_private_chats())
                        ],
                        multiple=True,
                        mode=SelectSelectorMode.DROPDOWN,
                        custom_value=True,
                    )
                ),
            }
        )
        return schema

    def _known_private_chats(self) -> set[int]:
        """Return private chat IDs the bot has seen (from store)."""
        try:
            from .store import ChatStore

            store = ChatStore(self.hass)
            return {
                chat.chat_id
                for chat in store.chats.values()
                if chat.chat_id > 0
            }
        except Exception:
            return set()

    async def _async_none_label(self) -> str:
        """Return the localised label for the "no pipeline" option."""
        translations = await async_get_translations(
            self.hass, self.hass.config.language, "selector", [DOMAIN]
        )
        return translations.get(
            f"selector.pipeline.options.{PIPELINE_NONE}",
            "No pipeline (echo replies)",
        )
