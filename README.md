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
| M5 | Mini App: personal settings, briefing and quiet hours | ✅ |

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
   (agent, STT, TTS and voice come from it), the **Bot users** (empty means
   everyone the Telegram integration lets through), the **Mini App admins**
   and the calendars, mailboxes and shopping list the family may use.
   Briefings, quiet hours, the reply mode and weather are set by each user in
   the Mini App.

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

Replies are sent with `parse_mode=html`: Markdown from the agent (`**bold**`,
`*` bullets, code spans, links) is converted to the small HTML subset Telegram
renders, and everything else is escaped, so no literal asterisks reach the chat.

### Custom skills

In the Mini App, **Skills → My skills** holds requests you write once: a name,
the text sent to your conversation agent, an optional time and weekdays. A
skill with a time runs on those days (quiet hours skip it); any skill runs
with ▶. Each skill talks to the agent in its own conversation, and the answer
arrives in the chat as `✨ Name`. The agent runs it with the same rights as in
the chat.

### Calendar alerts

**Settings → Calendar** can remind about events 5–60 minutes before they
start, and announce today's birthdays (entries such as "День рождения Маши"
or "Anna's birthday") at a chosen time with a greeting drafted by the agent.
Both read the calendars of the user's briefing.

### Connections and plugins

**House → Connections** shows what Hearthkeeper uses from Home Assistant.
Users pick their own weather, calendars from those the options share, and mailboxes; Telegram users listed
under *Mini App admins* in the options can also change the shared sources
there, which reloads the integration.

Mail is personal: each user connects their own mailbox in **House →
Connections → Mail** with an e-mail address and an app password. Hearthkeeper
checks the login before saving, keeps the password in its own private store
(never sent back to the page, logged or included in diagnostics), and reads
only the headers of unread messages over IMAP with TLS, without marking them
as read. The shared IMAP sensors from the options remain a fallback.

Calendars can be personal too: in **House → Connections → Calendars** a
user picks from the Home Assistant calendars the options share and can add up to five of their own
by a secret iCal link (Google, Yandex, iCloud, Outlook). Those are read only
for that user — in their briefing, calendar answers, event reminders and
birthdays — and never become Home Assistant entities. Only `https` (or `webcal`) links
are fetched — a calendar served from the home network works too — and a link
is saved only when the reply really is an iCalendar. It is kept in a private
store and shown back only as its host.

Connection plugins extend the list the way HACS adds repositories. A plugin is
a GitHub repository with a `connection.json` at its root and no code; admins
add it in the Mini App (or under *Connection plugins* in the options) as
`owner/repo` or `owner/repo@branch`:

```json
{
  "id": "nordpool",
  "name": {"ru": "Цена электричества", "en": "Electricity price"},
  "description": {"en": "Hourly prices in the morning briefing"},
  "icon": "bolt",
  "color": "#ff9f0a",
  "integration": {"domain": "nordpool", "hacs": "custom-components/nordpool"},
  "provides": {
    "kind": "briefing",
    "domain": "sensor",
    "line": {"ru": "⚡ {name}: {state} {unit}", "en": "⚡ {name}: {state} {unit}"}
  }
}
```

`kind` is `briefing` (each entity of the integration becomes a line in the
morning briefing; `line` may use `{name}`, `{state}` and `{unit}`), or
`calendar`, `mail`, `weather` or `todo` (the integration's entities become
choices for that source). Until the integration is installed, the plugin page
links to it in HACS (`hacs`) or to its setup in Home Assistant (`domain`).
Hearthkeeper builds those links itself and never runs anything from the
repository. Plugins are cached for six hours.

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

Each user sets up their briefings — morning, evening or mail, at their own
times — in the Mini App. Installs that had the household morning briefing
turned on before keep it for users who have not set up their own.
`/brief` and `hearthkeeper.brief` build the same summary on demand; an empty or
broken source only removes its own section.

## Mini App

`/web` sends a button that opens Hearthkeeper inside Telegram. The Mini App is
a settings dashboard, not a second chat:

- **Home** — the agent, quick actions and active reminders;
- **Skills** — your own skills, ready-made ones and the built-in phrases;
- **House** — connections: what Hearthkeeper reads (agent, weather, calendars,
  mail, shopping list, voice, home status, plugins);
- **Settings** — how the bot behaves: reply mode, briefings and their times,
  calendar alerts, quiet hours, the helper and the bot account. Quick actions (`/brief`, `/shop`, `/mail`, `/remind`) send
  the command to the chat and the answer arrives as a normal message.

Personal settings belong to the Telegram account that opened the app and are
kept with its private chat; values saved in the integration options earlier
stay the household defaults. Calendars and mailboxes can only be
picked from the pool an administrator already enabled.

The page is served at `/api/hearthkeeper/webapp` and carries no Home Assistant
data. Every call carries Telegram `initData`; the signature is checked against
the bot token, the user must be in the bot's allowed chats, and only that
user's own private chat can be changed.

Telegram opens Mini Apps only over HTTPS. Hearthkeeper uses the external URL or
the Home Assistant Cloud URL automatically. If neither is reachable by Telegram
clients, set **Mini App URL** in **Configure** to the public HTTPS address (an
existing tunnel or reverse proxy); the page path is appended for you.

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
  language, timestamps and the personal briefing/quiet-hour choices from the
  Mini App. Message texts, reminders and mail/calendar content never touch
  disk; pending reminders live in memory only.
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
