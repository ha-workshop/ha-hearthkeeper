"""Connection plugins: what an integration gives Hearthkeeper, read from GitHub.

It works the way HACS adds a repository: a plugin repository keeps one
``connection.json`` at its root and an admin adds it as ``owner/repo`` (or
``owner/repo@ref``). A plugin holds no code. It names the Home Assistant
integration to install, the entities that integration creates, and what
Hearthkeeper does with them::

    {
      "id": "nordpool",
      "name": {"ru": "Цена электричества", "en": "Electricity price"},
      "description": {"ru": "…", "en": "…"},
      "icon": "bolt",
      "color": "#ff9f0a",
      "integration": {"domain": "nordpool", "hacs": "custom-components/nordpool"},
      "provides": {
        "kind": "briefing",
        "domain": "sensor",
        "line": {"ru": "⚡ {name}: {state} {unit}", "en": "⚡ {name}: {state} {unit}"}
      }
    }

``kind`` is ``briefing`` (each entity becomes a line in the morning
briefing), or ``calendar``, ``mail``, ``weather`` or ``todo`` (the
integration's entities become choices for that source). Text fields are a
string or a ``{language: text}`` map. Links are built from ``hacs`` and
``domain`` only, never taken from the file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)

MANIFEST: Final = "connection.json"
KINDS: Final = ("briefing", "calendar", "mail", "weather", "todo")
_RAW_URL: Final = "https://raw.githubusercontent.com/{owner}/{repo}/{ref}/" + MANIFEST
_REPO: Final = re.compile(
    r"^(?P<owner>[A-Za-z0-9_.-]{1,39})/(?P<repo>[A-Za-z0-9_.-]{1,100})"
    r"(?:@(?P<ref>[A-Za-z0-9_./-]{1,100}))?$"
)
_ID: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_DOMAIN: Final = re.compile(r"^[a-z0-9_]{1,64}$")
_ICON: Final = re.compile(r"^[a-z]{1,16}$")
_COLOR: Final = re.compile(r"^#[0-9a-fA-F]{6}$")
_MAX_BYTES: Final = 64 * 1024
_TTL: Final = 6 * 3600
# A repository that failed is retried after this long, not on every page open.
_RETRY: Final = 15 * 60
_FETCH_TIMEOUT: Final = 10
_MAX_LINES: Final = 5
_UNKNOWN: Final = ("unknown", "unavailable", "")


def parse_repository(value: object) -> tuple[str, str, str] | None:
    """Return ``(owner, repo, ref)`` for ``owner/repo`` or ``owner/repo@ref``."""
    if not isinstance(value, str):
        return None
    match = _REPO.match(value.strip())
    if match is None or match.group("repo") in (".", ".."):
        return None
    return match.group("owner"), match.group("repo"), match.group("ref") or "HEAD"


@dataclass(frozen=True, slots=True)
class ConnectionPlugin:
    """One plugin read from a repository's ``connection.json``."""

    repo: str
    id: str
    name: Mapping[str, str]
    description: Mapping[str, str]
    icon: str
    color: str
    domain: str
    hacs: str
    kind: str
    entity_domain: str
    line: Mapping[str, str]

    def entities(self, hass: HomeAssistant) -> list[str]:
        """Return the entities the plugin's integration created."""
        registry = er.async_get(hass)
        return sorted(
            entry.entity_id
            for entry in registry.entities.values()
            if entry.platform == self.domain
            and (not self.entity_domain or entry.domain == self.entity_domain)
            and entry.disabled_by is None
        )

    def payload(self, hass: HomeAssistant, language: str) -> dict[str, Any]:
        """Return what the page shows for this plugin."""
        entities = self.entities(hass)
        return {
            "repo": self.repo,
            "id": self.id,
            "name": _pick(self.name, language),
            "description": _pick(self.description, language),
            "icon": self.icon,
            "color": self.color,
            "domain": self.domain,
            "hacs": self.hacs,
            "kind": self.kind,
            "entities": len(entities),
            "ready": bool(entities),
        }

    def briefing_lines(self, hass: HomeAssistant, language: str) -> list[str]:
        """Return the briefing lines of a ``briefing`` plugin."""
        if self.kind != "briefing":
            return []
        template = _pick(self.line, language) or "{name}: {state} {unit}"
        lines = []
        for entity_id in self.entities(hass)[:_MAX_LINES]:
            state = hass.states.get(entity_id)
            if state is None or state.state in _UNKNOWN:
                continue
            name = state.attributes.get("friendly_name") or entity_id
            unit = state.attributes.get("unit_of_measurement") or ""
            lines.append(
                _fill(template, name=str(name), state=state.state, unit=str(unit))
            )
        return lines


_FIELD: Final = re.compile(r"\{(name|state|unit)\}")


def _fill(template: str, **values: str) -> str:
    """Substitute ``{name}``-style fields in one pass.

    Never ``str.format`` on file text, and never a second pass, so a value
    such as an entity name cannot inject another field.
    """
    text = _FIELD.sub(lambda match: values.get(match.group(1), ""), template)
    return " ".join(text.split())


def parse_manifest(repo: str, data: object) -> ConnectionPlugin | None:
    """Return the plugin a ``connection.json`` describes, or None if invalid."""
    if not isinstance(data, Mapping):
        return None
    plugin_id = data.get("id")
    if not isinstance(plugin_id, str) or not _ID.match(plugin_id):
        return None
    name = _texts(data.get("name"), 40)
    if not name:
        return None
    integration = data.get("integration")
    provides = data.get("provides")
    if not isinstance(integration, Mapping) or not isinstance(provides, Mapping):
        return None
    domain = integration.get("domain")
    if not isinstance(domain, str) or not _DOMAIN.match(domain):
        return None
    hacs = integration.get("hacs") or ""
    if hacs and (
        not isinstance(hacs, str) or parse_repository(hacs) is None or "@" in hacs
    ):
        return None
    kind = provides.get("kind")
    if kind not in KINDS:
        return None
    entity_domain = provides.get("domain") or ""
    if entity_domain and (
        not isinstance(entity_domain, str) or not _DOMAIN.match(entity_domain)
    ):
        return None
    icon = data.get("icon") or ""
    color = data.get("color") or ""
    return ConnectionPlugin(
        repo=repo,
        id=plugin_id,
        name=name,
        description=_texts(data.get("description"), 200) or {},
        icon=icon if isinstance(icon, str) and _ICON.match(icon) else "",
        color=color if isinstance(color, str) and _COLOR.match(color) else "",
        domain=domain,
        hacs=hacs,
        kind=kind,
        entity_domain=entity_domain,
        line=_texts(provides.get("line"), 80) or {},
    )


def _texts(value: object, limit: int) -> dict[str, str] | None:
    """Return ``{language: text}`` from a string or a map, or None."""
    if isinstance(value, str):
        value = {"": value}
    if not isinstance(value, Mapping):
        return None
    texts = {
        str(lang).lower(): text.strip()
        for lang, text in value.items()
        if isinstance(text, str) and 0 < len(text.strip()) <= limit
    }
    return texts or None


def _pick(texts: Mapping[str, str], language: str) -> str:
    """Return the text in ``language``, else English, else any."""
    for key in (language, "en", ""):
        if key in texts:
            return texts[key]
    return next(iter(texts.values()), "")


class PluginError(Exception):
    """A repository has no valid ``connection.json``."""


class ConnectionPlugins:
    """Fetch, cache and use the connection plugins an admin added."""

    def __init__(self, hass: HomeAssistant, repositories: Iterable[str]) -> None:
        self.hass = hass
        self.repositories = [
            repo for repo in dict.fromkeys(repositories) if parse_repository(repo)
        ]
        self._cache: dict[str, tuple[float, ConnectionPlugin | None]] = {}
        self._lock = asyncio.Lock()

    async def async_plugins(self) -> list[ConnectionPlugin]:
        """Return the plugins of every repository that has a valid manifest."""
        async with self._lock:
            plugins = []
            for repo in self.repositories:
                plugin = await self._async_cached(repo)
                if plugin is not None:
                    plugins.append(plugin)
        return plugins

    def plugins(self) -> list[ConnectionPlugin]:
        """Return the plugins already fetched, without network access."""
        return [
            cached[1]
            for repo in self.repositories
            if (cached := self._cache.get(repo)) is not None and cached[1] is not None
        ]

    def briefing_lines(self, language: str) -> list[str]:
        """Return the briefing lines of every fetched ``briefing`` plugin."""
        lines: list[str] = []
        for plugin in self.plugins():
            lines.extend(plugin.briefing_lines(self.hass, language))
        return lines

    def integrations(self, kind: str) -> set[str]:
        """Return the integrations whose entities feed a source of ``kind``."""
        return {plugin.domain for plugin in self.plugins() if plugin.kind == kind}

    async def async_fetch(self, repo: str) -> ConnectionPlugin:
        """Read one repository now; raise ``PluginError`` when it is unusable."""
        parsed = parse_repository(repo)
        if parsed is None:
            raise PluginError("repo")
        owner, name, ref = parsed
        url = _RAW_URL.format(owner=owner, repo=name, ref=ref)
        try:
            session = async_get_clientsession(self.hass)
            async with asyncio.timeout(_FETCH_TIMEOUT):
                async with session.get(url) as response:
                    if response.status != 200:
                        raise PluginError(f"HTTP {response.status}")
                    body = await response.content.read(_MAX_BYTES + 1)
        except (ClientError, TimeoutError) as err:
            raise PluginError(type(err).__name__) from err
        if len(body) > _MAX_BYTES:
            raise PluginError("too large")
        try:
            plugin = parse_manifest(repo.strip(), json.loads(body))
        except ValueError as err:
            raise PluginError("json") from err
        if plugin is None:
            raise PluginError("manifest")
        return plugin

    async def _async_cached(self, repo: str) -> ConnectionPlugin | None:
        cached = self._cache.get(repo)
        if cached is not None and time.monotonic() - cached[0] < _TTL:
            return cached[1]
        try:
            plugin = await self.async_fetch(repo)
        except PluginError as err:
            _LOGGER.warning("Connection plugin %s is unavailable (%s)", repo, err)
            kept = cached[1] if cached else None
            self._cache[repo] = (time.monotonic() - _TTL + _RETRY, kept)
            return kept
        self._cache[repo] = (time.monotonic(), plugin)
        return plugin
