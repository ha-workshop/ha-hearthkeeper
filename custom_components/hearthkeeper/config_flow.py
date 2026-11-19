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
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)
from homeassistant.helpers.translation import async_get_translations

from .assist import async_list_pipelines
from .const import (
    CONF_ALLOW_GROUPS,
    CONF_PIPELINE_ID,
    CONF_REPLY_MODE,
    CONF_TELEGRAM_ENTRY,
    CONF_TODO_ENTITY,
    DEFAULT_OPTIONS,
    DOMAIN,
    NAME,
    PIPELINE_NONE,
    REPLY_MODES,
    TELEGRAM_DOMAIN,
)

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
            pipeline_id = user_input.get(CONF_PIPELINE_ID)
            user_input[CONF_PIPELINE_ID] = (
                None if pipeline_id in (None, "", PIPELINE_NONE) else pipeline_id
            )
            return self.async_create_entry(data=user_input)

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
                    CONF_REPLY_MODE,
                    default=options.get(
                        CONF_REPLY_MODE, DEFAULT_OPTIONS[CONF_REPLY_MODE]
                    ),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=list(REPLY_MODES), translation_key="reply_mode"
                    )
                ),
                vol.Optional(
                    CONF_ALLOW_GROUPS,
                    default=options.get(
                        CONF_ALLOW_GROUPS, DEFAULT_OPTIONS[CONF_ALLOW_GROUPS]
                    ),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_TODO_ENTITY,
                    description={"suggested_value": options.get(CONF_TODO_ENTITY)},
                ): EntitySelector(EntitySelectorConfig(domain="todo")),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

    async def _async_none_label(self) -> str:
        """Return the localised label for the "no pipeline" option."""
        translations = await async_get_translations(
            self.hass, self.hass.config.language, "selector", [DOMAIN]
        )
        return translations.get(
            f"selector.pipeline.options.{PIPELINE_NONE}",
            "No pipeline (echo replies)",
        )
