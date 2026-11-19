<p align="center"><img src="https://raw.githubusercontent.com/ha-workshop/ha-hearthkeeper/main/docs/icon.png" width="128" height="128" alt="Hearthkeeper"></p>

# Hearthkeeper — Telegram assistant for Home Assistant

**English** · [Русский](README.ru.md)

Telegram voice assistant for Home Assistant — local STT/TTS, calendar, mail and
house status, with the conversation agent you already use.

Hearthkeeper does not implement its own Telegram client. The core
`telegram_bot` integration owns the connection and the chat allowlist;
Hearthkeeper listens to its bus events, talks to a `conversation` agent and
replies through `telegram_bot` services.

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Release](https://img.shields.io/github/v/release/ha-workshop/ha-hearthkeeper)](https://github.com/ha-workshop/ha-hearthkeeper/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## Status

| Milestone | Scope | State |
|---|---|---|
| M0 | config flow, transport, text replies, `/help`, `/new`, `/voice` | ✅ |
| M1 | voice input: Telegram OGG → WAV → Assist pipeline | ✅ |
| M2 | voice output, groups, dedup across restarts | ✅ |
| M3 | briefing from calendar, weather, mail, todo and house state | ✅ |
| M4 | `/model`, inline buttons, diagnostics, packaging | ✅ |

## Requirements

- Home Assistant 2026.11 or newer
- A `telegram_bot` config entry in polling mode with at least one allowed chat
- A `conversation` agent; without one Hearthkeeper echoes messages back

## Install

1. Open the repository in HACS:

   [![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ha-workshop&repository=ha-hearthkeeper&category=integration)

   Or by hand: HACS → ⋮ → **Custom repositories** →
   `https://github.com/ha-workshop/ha-hearthkeeper`, type **Integration**.
2. Download **Hearthkeeper**, restart Home Assistant.
3. Add the integration:

   [![Add integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=hearthkeeper)

   (*Settings → Devices & services → Add integration → Hearthkeeper*.)
4. Pick the Telegram bot, then open **Configure** to choose the Assist pipeline
   (agent, STT, TTS and voice come from it) and the reply mode.

## Commands

| Command | Effect |
|---|---|
| `/help` | List commands |
| `/new` | Start a new conversation |
| `/voice on` | Voice replies (needs a pipeline with TTS) |
| `/voice off` | Text replies |
| `/voice both` | Text and voice |
| `/shop` | Shopping list from a `todo` entity (tap to tick off, `/shop del` asks first) |
| `/brief` | Daily briefing: weather, calendar, house, shopping list, mail |
| `/model` | Switch the conversation agent for this chat (buttons or `/model <name>`) |

## Briefing

Open **Configure** and pick the sources you already use in Home Assistant:

- **Calendars** — any number of `calendar.*` entities (Today and Tomorrow);
- **Weather entity** — a `weather.*` entity;
- **Shopping list** — the same `todo.*` list as `/shop`;
- **Mail sensors** — `sensor.*` entities created by the IMAP integration with
  unread counters;
- **Home status** — lights on, open doors and windows, active climate.

Set **Briefing time** (default 08:00) and turn on **Morning briefing**. The
daily message goes to every private chat the bot has already talked in.
`/brief` and `hearthkeeper.brief` build the same summary on demand; an empty or
broken source only removes its own section.

## Services

```yaml
action: hearthkeeper.say
data:
  chat_id: 123456789
  text: "The garage door is still open"
```

Add `mode: voice` (or `both`) to speak the message with the pipeline voice;
without it the chat's own reply mode is used.

- `hearthkeeper.say` — send text to a chat
- `hearthkeeper.reset` — forget the conversation context (one chat or all)
- `hearthkeeper.brief` — send the briefing now to one chat or to every known private chat

## Privacy

- Only control data is stored: chat id, conversation id, agent, reply mode,
  language, timestamps. Message texts and mail/calendar content never touch
  disk.
- Nothing leaves the house except Telegram itself. Cloud agents are opt-in via
  the agent you choose.
- Source audio and mail content are only logged at `DEBUG`, and never with
  content.

## Diagnostics

**Settings → Devices & services → Hearthkeeper → ⋮ → Download diagnostics**
returns counters, entity ids and settings — never message texts, chat ids,
audio, mail or calendar contents. It is the fastest way to attach context to a
bug report.

## License

MIT
