"""Telegram Mini App (WebApp) for Hearthkeeper.

Provides an HTTP endpoint at ``/hearthkeeper/webapp/`` that serves an
HTML chat widget. The widget connects to an Assist pipeline over the
Home Assistant WebSocket API and lets the user talk to the same agent
they use in Telegram.

The HTML template is loaded from ``webapp.html`` in the same package
directory, and minimal substitutions are applied at render time.
"""

from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.helpers.http import HomeAssistantView

_LOGGER = logging.getLogger(__name__)

DOMAIN = "hearthkeeper"

WEBAPP_PATH = f"/api/{DOMAIN}/webapp"
WEBAPP_NAME = f"{DOMAIN}:webapp"
_TEMPLATE: str = ""

# Set dynamically on first registration
_WEBAPP_URL: str = ""


@property
def WEBAPP_URL() -> str:
    """Return the public URL of the WebApp, or empty string when unknown."""
    return _WEBAPP_URL


def _load_template() -> str:
    """Read the HTML template from the package directory."""
    path = Path(__file__).parent / "webapp.html"
    return path.read_text(encoding="utf-8")


def _get_template() -> str:
    """Return the cached template, loading it on first access."""
    global _TEMPLATE
    if not _TEMPLATE:
        _TEMPLATE = _load_template()
    return _TEMPLATE


async def async_register_webapp(hass: HomeAssistant) -> None:
    """Register the WebApp HTTP endpoint.

    The template is loaded in the executor to avoid blocking the event loop.
    """
    global _WEBAPP_URL
    _WEBAPP_URL = _build_webapp_url(hass)
    await hass.async_add_executor_job(_get_template)
    hass.http.register_view(WebAppView())


def _build_webapp_url(hass: HomeAssistant) -> str:
    """Build the public WebApp URL from the HA external URL."""
    external_url = hass.config.external_url or hass.config.url
    if not external_url:
        return ""
    return external_url.rstrip("/") + WEBAPP_PATH


class WebAppView(HomeAssistantView):
    """Serve the Mini App HTML page."""

    url = WEBAPP_PATH
    name = WEBAPP_NAME
    requires_auth = False  # Auth is handled via Telegram initData

    async def get(self, request: web.Request) -> web.Response:
        """Return the WebApp HTML."""
        return web.Response(
            text=_TEMPLATE,
            content_type="text/html; charset=utf-8",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "X-Frame-Options": "ALLOW-FROM https://t.me",
            },
        )