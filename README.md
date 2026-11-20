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
| `/mail` | Recent mail: unread counters and senders/subjects |
| `/remind 20m text` | Reminder in 20 minutes; also `/remind 20:30 text`, `/remind list`, `/remind cancel` |
| `/model` | Switch the conversation agent for this chat (buttons or `/model <name>`) |
| `/web` | Open the Mini App: settings and quick actions (private chat, needs an HTTPS URL of Home Assistant) |

## Skills

Before a text reaches the conversation agent, Hearthkeeper matches a few short
phrases itself. This works even with a local agent that knows nothing about
Home Assistant:

| Phrase (RU / EN) | Answer |
|---|---|
| «сводка», «брифинг» / "briefing", "daily summary" | The same briefing as `/brief` |
| «что у меня сегодня», «планы на завтра» / "what is on today", "tomorrow's plans" | Calendar for the requested day |
| «что дома», «статус дома» / "home status", "anything on at home" | Lights, open doors and windows, climate |

The same phrases are registered as Assist intents and written to
`custom_sentences/` so the default Assist agent understands them too. Anything
else goes to the conversation agent as before.

## Briefing

Open **Configure** and pick the sources you already use in Home Assistant:

- **Calendars** — any number of `calendar.*` entities (Today and Tomorrow);
- **Weather entity** — a `weather.*` entity;
- **Shopping list** — the same `todo.*` list as `/shop`;
- **Mail sensors** — `sensor.*` entities created by the IMAP integration with
  unread counters. Recent senders and subjects come from `imap_content`
  events and stay in memory only. Enable **AI summary for mail** to ask your
  preferred AI Task for a 3–5 line digest; it is off by default;
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

Natural phrases work too: “напомни через 20 минут …” and “remind me in 20
minutes to …”. Reminders are kept in memory and also create a persistent
notification, so a Home Assistant restart clears the pending ones.

Add `mode: voice` (or `both`) to speak the message with the pipeline voice;
without it the chat's own reply mode is used.

- `hearthkeeper.say` — send text to a chat
- `hearthkeeper.reset` — forget the conversation context (one chat or all)
- `hearthkeeper.brief` — send the briefing now to one chat or to every known private chat

## Privacy

- Only control data is stored: chat id, conversation id, agent, reply mode,
  language, timestamps. Message texts, reminders and mail/calendar content
  never touch disk; pending reminders live in memory only.
- Nothing leaves the house except Telegram itself. Cloud agents are opt-in via
  the agent you choose, and mail summaries are a separate opt-in
  (**AI summary for mail**).
- Source audio and mail content are only logged at `DEBUG`, and never with
  content.

## Diagnostics

**Settings → Devices & services → Hearthkeeper → ⋮ → Download diagnostics**
returns counters, entity ids and settings — never message texts, chat ids,
audio, mail or calendar contents. It is the fastest way to attach context to a
bug report.

## License

MIT
