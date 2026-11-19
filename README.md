# Hearthkeeper

Telegram voice assistant for Home Assistant — local STT/TTS, calendar, mail and
house status, with the conversation agent you already use.

Hearthkeeper does not implement its own Telegram client. The core
`telegram_bot` integration owns the connection and the chat allowlist;
Hearthkeeper listens to its bus events, talks to a `conversation` agent and
replies through `telegram_bot` services.

## Status

| Milestone | Scope | State |
|---|---|---|
| M0 | config flow, transport, text replies, `/help`, `/new`, `/voice` | ✅ |
| M1 | voice input: OGG → PCM → STT | ⏳ |
| M2 | voice output, groups, dedup across restarts | ⏳ |
| M3 | skills: calendar, mail, home, brief, todo | ⏳ |
| M4 | `/model`, inline buttons, diagnostics, packaging | ⏳ |

## Requirements

- Home Assistant 2026.11 or newer
- A `telegram_bot` config entry in polling mode with at least one allowed chat
- A `conversation` agent; without one Hearthkeeper echoes messages back

## Installation

1. HACS → ⋮ → Custom repositories → add
   `https://github.com/ha-workshop/ha-hearthkeeper` as an **Integration**.
2. Install **Hearthkeeper**, restart Home Assistant.
3. Settings → Devices & services → Add integration → **Hearthkeeper**.
4. Pick the Telegram bot, then open **Configure** to choose the conversation
   agent, language and reply mode.

## Commands

| Command | Effect |
|---|---|
| `/help` | List commands |
| `/new` | Start a new conversation |
| `/voice on` | Voice replies (arrives in M2) |
| `/voice off` | Text replies |
| `/voice both` | Text and voice |
| `/brief` | Daily briefing (M3) |
| `/model` | Switch the agent (M3) |

## Services

```yaml
action: hearthkeeper.say
data:
  chat_id: 123456789
  text: "The garage door is still open"
```

- `hearthkeeper.say` — send text to a chat
- `hearthkeeper.reset` — forget the conversation context (one chat or all)
- `hearthkeeper.brief` — send the briefing now (M3)

## Privacy

- Only control data is stored: chat id, conversation id, agent, reply mode,
  language, timestamps. Message texts and mail/calendar content never touch
  disk.
- Nothing leaves the house except Telegram itself. Cloud agents are opt-in via
  the agent you choose.
- Source audio and mail content are only logged at `DEBUG`, and never with
  content.

## License

MIT
