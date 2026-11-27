"""Tests for connection plugins."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.hearthkeeper.connections import (
    ConnectionPlugins,
    PluginError,
    parse_manifest,
    parse_repository,
)

MANIFEST: dict[str, Any] = {
    "id": "nordpool",
    "name": {"ru": "Цена электричества", "en": "Electricity price"},
    "description": "Hourly prices",
    "icon": "bolt",
    "color": "#ff9f0a",
    "integration": {"domain": "nordpool", "hacs": "custom-components/nordpool"},
    "provides": {
        "kind": "briefing",
        "domain": "sensor",
        "line": {"ru": "⚡ {name}: {state} {unit}"},
    },
}
URL = "https://raw.githubusercontent.com/owner/plug/HEAD/connection.json"


def test_parse_repository() -> None:
    """Repositories are written like in HACS, with an optional ref."""
    assert parse_repository("owner/repo") == ("owner", "repo", "HEAD")
    assert parse_repository(" owner/repo@v1 ") == ("owner", "repo", "v1")
    for bad in ("", "owner", "a/b/c", "owner/..", "https://github.com/a/b", None):
        assert parse_repository(bad) is None


def test_parse_manifest() -> None:
    """A valid manifest is read; unsafe or broken fields are refused."""
    plugin = parse_manifest("owner/plug", MANIFEST)
    assert plugin is not None
    assert (plugin.domain, plugin.kind, plugin.hacs) == (
        "nordpool",
        "briefing",
        "custom-components/nordpool",
    )

    def broken(**patch: Any) -> dict[str, Any]:
        return {**MANIFEST, **patch}

    for bad in (
        broken(id="Bad Id"),
        broken(name=""),
        broken(integration={"domain": "../x"}),
        broken(integration={"domain": "x", "hacs": "https://evil.example"}),
        broken(provides={"kind": "shell"}),
        broken(provides={"kind": "briefing", "domain": "a b"}),
        "nope",
    ):
        assert parse_manifest("owner/plug", bad) is None

    loose = parse_manifest("owner/plug", broken(icon="<svg>", color="red"))
    assert loose is not None and (loose.icon, loose.color) == ("", "")


async def test_plugin_briefing_lines(hass: HomeAssistant, aioclient_mock: Any) -> None:
    """A briefing plugin turns its integration's entities into lines."""
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", "nordpool", "price", suggested_object_id="price"
    )
    registry.async_get_or_create("sensor", "other", "x", suggested_object_id="other")
    hass.states.async_set(
        "sensor.price",
        "1.23",
        {"friendly_name": "Цена {state}", "unit_of_measurement": "лей"},
    )
    aioclient_mock.get(URL, json=MANIFEST)
    plugins = ConnectionPlugins(hass, ["owner/plug"])

    assert plugins.briefing_lines("ru") == []  # nothing fetched yet
    [plugin] = await plugins.async_plugins()
    payload = plugin.payload(hass, "ru")
    assert (payload["name"], payload["entities"], payload["ready"]) == (
        "Цена электричества",
        1,
        True,
    )
    # Values are substituted literally: a name cannot inject another field.
    assert plugins.briefing_lines("ru") == ["⚡ Цена {state}: 1.23 лей"]
    assert plugins.integrations("briefing") == {"nordpool"}


async def test_plugins_are_cached_and_failures_kept(
    hass: HomeAssistant, aioclient_mock: Any
) -> None:
    """A repository is read once; a broken one is not retried on every open."""
    aioclient_mock.get(URL, json=MANIFEST)
    aioclient_mock.get(
        "https://raw.githubusercontent.com/owner/gone/HEAD/connection.json", status=404
    )
    plugins = ConnectionPlugins(hass, ["owner/plug", "owner/gone", "bad repo"])

    first = await plugins.async_plugins()
    await plugins.async_plugins()

    assert [plugin.repo for plugin in first] == ["owner/plug"]
    assert aioclient_mock.call_count == 2

    with pytest.raises(PluginError):
        await plugins.async_fetch("owner/gone")
