"""Telegram Mini App (WebApp) for Hearthkeeper.

Provides an HTTP endpoint at ``/hearthkeeper/webapp/`` that serves an
HTML chat widget. The widget connects to an Assist pipeline over the
Home Assistant WebSocket API and lets the user talk to the same agent
they use in Telegram.
"""

from __future__ import annotations

import logging

from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.helpers.http import HomeAssistantView

_LOGGER = logging.getLogger(__name__)

DOMAIN = "hearthkeeper"

WEBAPP_PATH = f"/{DOMAIN}/webapp"
WEBAPP_NAME = f"{DOMAIN}:webapp"

# Set dynamically on first registration
_WEBAPP_URL: str = ""


@property
def WEBAPP_URL() -> str:
    """Return the public URL of the WebApp, or empty string when unknown."""
    return _WEBAPP_URL


def async_register_webapp(hass: HomeAssistant) -> None:
    """Register the WebApp HTTP endpoint."""
    global _WEBAPP_URL
    _WEBAPP_URL = _build_webapp_url(hass)
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
        html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Hearthkeeper</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: var(--tg-theme-bg-color, #fff);
  --text: var(--tg-theme-text-color, #000);
  --hint: var(--tg-theme-hint-color, #999);
  --link: var(--tg-theme-link-color, #2481cc);
  --btn: var(--tg-theme-button-color, #2481cc);
  --btn-text: var(--tg-theme-button-text-color, #fff);
  --sec-bg: var(--tg-theme-secondary-bg-color, #f0f0f0);
}
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--tg-theme-secondary-bg-color, var(--sec-bg));
  color: var(--text);
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}
#messages {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.msg {
  max-width: 85%;
  padding: 10px 14px;
  border-radius: 12px;
  font-size: 16px;
  line-height: 1.4;
  word-wrap: break-word;
}
.msg.user {
  align-self: flex-end;
  background: var(--tg-theme-button-color, var(--btn));
  color: var(--tg-theme-button-text-color, var(--btn-text));
  border-bottom-right-radius: 4px;
}
.msg.bot {
  align-self: flex-start;
  background: var(--tg-theme-bg-color, var(--bg));
  border-bottom-left-radius: 4px;
  box-shadow: 0 1px 2px rgba(0,0,0,0.1);
}
#input-bar {
  display: flex;
  gap: 8px;
  padding: 12px;
  background: var(--tg-theme-bg-color, var(--bg));
  border-top: 1px solid var(--tg-theme-hint-color, #ddd);
}
#input-bar input {
  flex: 1;
  border: none;
  border-radius: 20px;
  padding: 10px 16px;
  font-size: 16px;
  background: var(--tg-theme-secondary-bg-color, var(--sec-bg));
  color: var(--text);
  outline: none;
}
#input-bar button {
  width: 44px;
  height: 44px;
  border: none;
  border-radius: 50%;
  background: var(--tg-theme-button-color, var(--btn));
  color: var(--tg-theme-button-text-color, var(--btn-text));
  font-size: 20px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}
#input-bar button:disabled {
  opacity: 0.5;
}
#status {
  text-align: center;
  padding: 6px;
  font-size: 13px;
  color: var(--hint);
}
</style>
</head>
<body>
<div id="messages"><div class="msg bot">Connecting…</div></div>
<div id="status">connecting</div>
<div id="input-bar">
  <input id="text-input" type="text" placeholder="Message…" disabled>
  <button id="send-btn" disabled>➤</button>
</div>
<script>
(function() {
  const tg = window.Telegram.WebApp;
  tg.ready();
  tg.expand();

  const messagesEl = document.getElementById('messages');
  const statusEl = document.getElementById('status');
  const input = document.getElementById('text-input');
  const sendBtn = document.getElementById('send-btn');

  let ws = null;
  let conversationId = null;
  let agentId = null;

  function addMessage(text, role) {
    const div = document.createElement('div');
    div.className = 'msg ' + role;
    div.textContent = text;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function setStatus(text) {
    statusEl.textContent = text;
  }

  function setConnected(connected) {
    input.disabled = !connected;
    sendBtn.disabled = !connected;
    setStatus(connected ? 'connected' : 'disconnected');
  }

  // Determine HA WebSocket URL
  const loc = window.location;
  const wsProto = loc.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = wsProto + '//' + loc.host + '/api/conversation/ws';

  function connect() {
    ws = new WebSocket(wsUrl);
    ws.onopen = () => {
      setConnected(true);
      setStatus('connected');
      // Authenticate via Telegram initData
      const initData = tg.initData;
      if (initData) {
        ws.send(JSON.stringify({ type: 'auth', access_token: initData }));
      }
      addMessage('Connected. Ask me anything about your home.', 'bot');
    };
    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === 'conversation' && msg.result) {
          const reply = msg.result.response?.speech?.plain?.speech || '';
          if (reply) {
            addMessage(reply, 'bot');
          }
          if (msg.result.conversation_id) {
            conversationId = msg.result.conversation_id;
          }
          if (msg.result.agent_id) {
            agentId = msg.result.agent_id;
          }
        }
      } catch (err) {
        // ignore parse errors
      }
    };
    ws.onclose = () => {
      setConnected(false);
      setStatus('disconnected – reconnecting…');
      setTimeout(connect, 3000);
    };
    ws.onerror = () => {
      ws.close();
    };
  }

  function send() {
    const text = input.value.trim();
    if (!text || !ws) return;
    input.value = '';
    addMessage(text, 'user');
    setStatus('processing…');
    ws.send(JSON.stringify({
      type: 'conversation',
      text: text,
      conversation_id: conversationId,
      agent_id: agentId,
    }));
  }

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') send();
  });
  sendBtn.addEventListener('click', send);

  connect();
})();
</script>
</body>
</html>"""
        return web.Response(
            text=html,
            content_type="text/html; charset=utf-8",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "X-Frame-Options": "ALLOW-FROM https://t.me",
            },
        )