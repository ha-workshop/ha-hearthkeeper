# ТЗ: Hearthkeeper — Telegram-ассистент для Home Assistant

**Проект:** `ha-workshop/ha-hearthkeeper`
**Домен интеграции:** `hearthkeeper`
**Тип:** custom integration для Home Assistant, распространяется через HACS
**Версия ТЗ:** 1.0 · ноябрь 2026
**Статус:** черновик к реализации

---

## 1. Цель и не-цели

### 1.1 Цель

Ассистент в Telegram, который:

1. принимает **голосовые сообщения** и текст;
2. понимает команды дому («включи свет на кухне», «какая температура на улице»);
3. отвечает **голосом или текстом** — на выбор, по чату;
4. собирает данные из **календаря, почты, задач и состояния дома** и присылает сводки;
5. работает **локально**: STT, TTS, LLM и данные не покидают дом, кроме самого Telegram.

### 1.2 Не-цели (осознанно вне рамок)

| Не делаем | Почему |
|---|---|
| Свой Telegram-клиент (long polling, webhooks) | это уже делает ядро `telegram_bot`, включая allowlist чатов |
| Web-UI, отдельный сервер, docker-контейнер | всё живёт внутри HA, обновляется через HACS |
| Мультитенантность, роли, биллинг | личный ассистент для одного дома |
| Отправку писем и удаление событий календаря в MVP | необратимые действия — только после отдельного этапа с подтверждением |
| Замену Assist-сателлитов (ESP32, атомы) | у них другой транспорт, наш — Telegram |
| Собственный LLM | используем conversation-агента HA (локальный или облачный, per-chat) |

### 1.3 Отличие от `Shulyaka/telegram_bot_conversation`

Тот проект — универсальный чат с агентом в Telegram. Наш — **ассистент дома**: skills (сводка дня, календарь, почта, задачи), локальный STT/TTS по умолчанию, привязка к HA-пользователю, утренний брифинг по расписанию. Чужой код можно изучить и переиспользовать идеи, но зависимости от него нет.

---

## 2. Пользовательские сценарии

| ID | Сценарий | Приоритет |
|---|---|---|
| US-1 | Пишу боту «включи свет в гостиной» текстом → свет включается, приходит короткое подтверждение | MVP |
| US-2 | Записываю голосовое «какая погода завтра» → бот распознаёт, отвечает голосом | MVP |
| US-3 | Голосом «сделай свет потеплее» в чате, где включены голосовые ответы | MVP |
| US-4 | «Что у меня завтра» → список событий календаря с временем и местами | MVP |
| US-5 | `/brief` или «сводка» → календарь на день + важные письма + состояние дома | M3 |
| US-6 | «Что важного в почте» → суммаризация непрочитанных через AI Task | M3 |
| US-7 | Каждое утро в 08:00 бот сам присылает брифинг | M3 |
| US-8 | В групповом чате бот отвечает только на `@bot` и на команды | M2 |
| US-9 | Переключаю чат на текстовые ответы командой `/voice off` | M2 |
| US-10 | Прошу «напомни выключить духовку через 20 минут» → таймер/напоминание в HA | M3 |

---

## 3. Архитектура

```
Telegram ──polling──> core telegram_bot (config entry, allowlist)
                            │  bus events: telegram_text / telegram_command / telegram_attachment
                            ▼
                   ┌──────────────────────────┐
                   │ hearthkeeper              │
                   │  transport.py  (фильтры)  │
                   │  voice.py   (OGG ↔ PCM)   │
                   │  brain.py   (conversation)│
                   │  skills/    (tools)       │
                   │  store.py   (настройки)   │
                   └──────────────────────────┘
                        │            │
                 stt.<engine>   tts.<engine>
                        │            │
                        └──> response ──> telegram_bot.send_message / send_voice
```

### 3.1 Транспорт — только ядро

Своих сетевых вызовов к Telegram нет. Ядро `telegram_bot` в режиме **polling** (наружу ничего не открываем) уже:

- держит токен и соединение;
- ведёт allowlist chat ID через subentries;
- отдаёт скачивание файлов действием `telegram_bot.download_file`;
- умеет `send_message`, `send_voice`, `send_chat_action`.

Компонент подписывается на шину HA:

| Событие | Когда | Что берём |
|---|---|---|
| `telegram_text` | текст | `text`, `chat_id`, `id`, `user_id`, `message_thread_id` |
| `telegram_command` | `/brief`, `/voice` | `command` (со слэшем), `args` (список) |
| `telegram_attachment` | файл, в т.ч. голосовое | `file_id`, `file_mime_type`, `file_size`, `chat_id` |
| `telegram_callback` | inline-кнопки `/shop` | `data`, `chat_id`, `id` (id запроса), `message.message_id` |

Факты, на которых держится решение (проверено по ядру HA 2026.11):

- события объявлены в `homeassistant/components/telegram_bot/const.py` (`TelegramBotEvent`: `telegram_text`, `telegram_command`, `telegram_attachment`, `telegram_callback`, `telegram_sent`);
- id входящего сообщения приходит в поле **`id`**, а не `message_id` (`message_id` есть только у события `telegram_sent`); `command` содержит слэш (`/voice`), аргументы — список `args`;
- в данных есть `bot` с `config_entry_id` и `username` — по нему фильтруем события нужного бота;
- голосовое приходит как `telegram_attachment` с `file_mime_type = "audio/ogg"` (Telegram шлёт OGG/Opus);
- в данных есть `file_id` и `file_size`;
- скачивание: `telegram_bot.download_file` (`file_id`, `directory_path`, `file_name`);
- **действий `stt.transcribe` и `assist_pipeline.run` в ядре нет** — поэтому распознавание вызываем через API сущности STT, а не сервисом.

Резерв: если шинные события изменят, переключаемся на event-сущность `telegram_bot` (`event.*_update`, типы `attachment`/`text`/`command`). Обе точки входа держим за одним интерфейсом `Transport`.

### 3.2 Голос на входе

1. `telegram_attachment` с `audio/*` (голосовое — `audio/ogg`); если MIME пустой или `octet-stream`, расширение файла всё равно распознаём (`.ogg`, `.opus`, `.mp3`, `.m4a`, `.wav`).
2. `telegram_bot.download_file` → каталог интеграции по умолчанию `/config/telegram_bot/hk_<chat_id>_<id>.<ext>`. Путь по умолчанию не проверяется по `allowlist_external_dirs`, поэтому ничего добавлять в конфиг не нужно. Файл удаляется сразу после декодирования.
3. Декодирование через **ffmpeg** (всегда есть в официальных образах HA): `-ac 1 -ar 16000 -c:a pcm_s16le -f wav pipe:1`. Своих pip-зависимостей нет: у `pyogg` нет Linux-колёс, а `ffmpeg` уже используется ядром. Длительность проверяется `ffprobe`, лимит — `max_voice_seconds` (120 с), размер — 20 МБ (лимит Bot API).
4. Распознавание и ответ одним вызовом пайплайна, без ручного дёргания STT:

   ```python
   await assist_pipeline.async_pipeline_from_audio_stream(
       hass,
       context=Context(),
       event_callback=on_event,          # stt-end, intent-end, error
       stt_metadata=SpeechMetadata(...), # WAV, PCM, 16 bit, 16 kHz, mono
       stt_stream=chunks,
       pipeline_id=pipeline.id,
       conversation_id=f"tg:{chat_id}",
       start_stage=PipelineStage.STT,
       end_stage=PipelineStage.INTENT,
   )
   ```

   Пайплайн сам берёт свои STT, агента и язык — второй копии настроек нет. `SpeechMetadata` требует все шесть полей (`format`, `codec`, `bit_rate`, `sample_rate`, `channel`, `language`); WAV/PCM/16 кГц/mono принимают и wyoming, и Google STT.

Провайдер по умолчанию — STT выбранного пайплайна (у автора — `stt.faster_whisper_2`). `keep_audio` (M4) сохранит исходник; по умолчанию он удаляется.

### 3.3 Мозг

`conversation.process` (сервис) или напрямую `conversation.async_converse` с агентом из настроек:

```python
result = await hass.services.async_call(
    "conversation",
    "process",
    {"text": text, "agent_id": agent_id, "conversation_id": f"tg:{chat_id}"},
    blocking=True,
    return_response=True,
)
reply = result["response"]["speech"]["plain"]["speech"]
```

(Форма ответа: `response.speech.plain.speech`; `conversation_id` возвращается рядом — его и сохраняем, а не собираем вручную.)

- `conversation_id` = `tg:<chat_id>` — контекст на чат, отдельно для групп.
- Неизвестные команды не перехватываем: `/video` и подобные обрабатывают автоматизации пользователя, поэтому Hearthkeeper на них молчит (только `DEBUG`-запись), а не отвечает «не знаю такую команду».
- `/shop` — наш скилл (`shop.py`): список берётся из `todo.*`, кнопки `shop_done:<uid>` отвечают только на свой префикс; чужие callback (`/video_*`) не трогаем и не подтверждаем, чтобы не сломать автоматизации.
- По умолчанию агент локальный (`conversation.qwen_2_5_3b_conversation`), `prefer_local_intents`.
- Переключение агента командой `/model` (M3).
- Если агент вернул `speech` с `ssml`/`plain` — берём `plain`.
- Таймаут 60 с; по истечении — текстовая ошибка, диалог не рвём.

### 3.4 Голос на выходе

1. TTS из выбранного пайплайна (`tts.piper`, голос `ru_RU-denis-medium`, язык `ru_RU`).
2. `tts.async_create_stream(engine=..., language=..., options={"voice": ...})` + `async_set_message(text)` + `async_stream_result()` → родной формат движка (Piper отдаёт WAV). Это тот же стрим, которым пользуется сам пайплайн, поэтому кэш и конвертация ядра работают как обычно.
3. Перекодирование в **OGG/Opus** через ffmpeg (`libopus`, 48 кГц mono, 32 kbps, `-application voip`). Это обязательное условие: mp3/wav Telegram покажет файлом, а не голосовым сообщением.
4. Файл кладётся в первый media-каталог (`/media/hearthkeeper/answer_<random>.ogg`). Media-каталоги по умолчанию в `allowlist_external_dirs` и, в отличие от `www`, не отдаются по HTTP. `telegram_bot.send_voice(file=..., chat_id=..., reply_to_message_id=...)`, затем файл удаляется сразу после отправки.
5. В режиме `both` текст уходит первым, пока синтез ещё идёт. Если TTS упал, ответ всё равно уходит текстом. Дольше `TTS_TIMEOUT` (60 с) синтез не ждём.

Пока идёт обработка — `telegram_bot.send_chat_action` с `record_voice` (или `typing` для текстового режима), чтобы пользователь видел прогресс.

Режимы ответа на чат: `text` · `voice` · `both` (по умолчанию `text`, переключается `/voice`).

### 3.5 Skills (наши «скиллы»)

Skill — это функция с JSON-схемой параметров, которую может вызвать агент или которая срабатывает по интенту/команде.

```python
class Skill(Protocol):
    name: str  # "calendar_today"
    title: str  # "Что сегодня в календаре"
    description: str  # для LLM и для справки
    parameters: dict  # JSON Schema
    requires_cloud: bool  # честный флаг: уводит данные наружу?

    async def run(self, hass, args: dict, ctx: ChatContext) -> str: ...
```

Реестр `skills/registry.py` собирает включённые скиллы. Способы вызова:

1. **Интенты** — регистрируем `intent.async_register_intent` + sentences (`«что в почте»`, `«сводка»`, `«что завтра»`) с переводами ru/en. Работает с любым агентом, включая локальный Assist.
2. **Инструменты LLM** — если агент поддерживает tools (AI Task/облачные), скиллы отдаются как функции. Это M4, не MVP.

Скиллы MVP:

| Skill | Параметры | Источник данных | Что отдаёт |
|---|---|---|---|
| `calendar_events` | `range: today\|tomorrow\|week` | `calendar.get_events` по выбранным календарям | список «09:30 — Встреча, место» |
| `mail_digest` | `unread: bool = true`, `limit: int = 5` | IMAP-интеграция (сенсоры), AI Task для суммаризации | 3–5 строк «кто / тема / важность» |
| `home_status` | `rooms: list[str] \| None` | состояния HA (климат, окна, свет, батареи, энергия) | короткая сводка |
| `brief` | — | три выше | брифинг дня, до 15 строк |
| `todo_items` | `list: str`, `add: str \| None` | `todo.get_items` / `todo.add_item` | список или подтверждение |

Правила:

- Скилл **никогда** не печатает в лог содержимое почты/календаря выше `DEBUG`.
- `mail_digest` помечен `requires_cloud` при использовании облачного AI Task: в брифинге пишем «почта: сводка через облако» и уважаем настройку `allow_cloud_for_mail`.
- Ошибка одного скилла не ломает брифинг: секция заменяется на «почта: недоступна».

### 3.6 Утренний брифинг

Автоматизация внутри компонента (не в YAML пользователя): `time` из настроек (по умолчанию 08:00), `skills = [calendar_events, mail_digest, home_status]`, тихие часы, отправка в выбранные чаты. Если к 08:00 данных нет — отправляем то, что есть, с пометкой.

---

## 4. Функциональные требования

| ID | Требование | Приоритет |
|---|---|---|
| FR-1 | Принимать текст из allowlist-чатов и отвечать текстом | MVP |
| FR-2 | Принимать голосовые (OGG/Opus), распознавать локальным STT | MVP |
| FR-3 | Отвечать голосом (OGG/Opus) по настройке чата | MVP |
| FR-4 | Контекст диалога на чат, `/new` сбрасывает | MVP |
| FR-5 | Настройка через UI: бот, агент, STT, TTS+голос, язык, режим ответа | MVP |
| FR-6 | Allowlist чатов и пользователей; чужие сообщения игнорируются | MVP |
| FR-7 | Индикатор «печатает/записывает голосовое» во время обработки | MVP |
| FR-8 | Таймаут и текстовая ошибка вместо зависания | MVP |
| FR-9 | Скиллы `calendar_events`, `home_status`, `brief` | MVP |
| FR-10 | Скилл `mail_digest` с опциональной облачной суммаризацией | M3 |
| FR-11 | `/brief`, `/voice on|off`, `/model`, `/new`, `/help`, `/shop` | M0–M3 |
| FR-12 | Группы: ответ только на `@bot`, `telegram_command` и reply на сообщение бота | M2 |
| FR-13 | Утренний брифинг по расписанию | M3 |
| FR-14 | Инлайн-кнопки для подтверждений (создать событие, добавить задачу) | M4 |
| FR-15 | Диагностика: `diagnostics.py` без секретов, счётчики и последние ошибки | M4 |

---

## 5. Конфигурация

### 5.1 Config flow (первый запуск)

1. Выбрать существующий config entry `telegram_bot` (polling).
2. Выбрать чат(ы) — подтягиваем из allowlist ядра; иначе просим прислать `/start` боту и подсказываем chat_id.
3. Выбрать Assist-пайплайн («Настройки → Голосовые ассистенты», `/config/voice-assistants/assistants`). Агент, STT, TTS, голос и язык берутся из него, второй копии настроек не заводим.
4. Режим ответа (`text` / `voice` / `both`). Язык служебных ответов — язык пайплайна, иначе язык HA.

### 5.2 Options flow

| Параметр | По умолчанию | Комментарий |
|---|---|---|
| `pipeline_id` | нет (эхо) | пайплайн Assist; в нём же агент, STT, TTS, голос, язык |
| `reply_mode` | `text` | per-chat переопределяется `/voice` |
| `allow_groups` | `false` | требует username бота |
| `todo_entity` | нет | `todo.*` для `/shop`; без него команда отвечает «не настроено» |
| `keep_audio` | `false` | хранить исходные OGG |
| `allow_cloud_for_mail` | `false` | разрешить AI Task в облаке для почты |
| `brief_time` | `08:00` | `null` — выключено |
| `brief_chats` | первый allowlist-чат | |
| `max_voice_seconds` | `120` | защита от «лекций» |

`agent_id`, `stt_engine`, `tts_engine`, `tts_voice` и `language` в options **не дублируются**: их единственный источник — пайплайн Assist. Per-chat переопределения (`/model`, язык) живут в `ChatContext` (§6), а не в options.

### 5.3 Сервисы компонента

```yaml
hearthkeeper.brief:            # отправить брифинг сейчас (чат или все)
hearthkeeper.reset:            # сбросить контекст чата
hearthkeeper.say:              # отправить текст/голос в чат (для автоматизаций)
```

---

## 6. Хранилище

`homeassistant.helpers.storage.Store`, один файл в `.storage/`:

```jsonc
// hearthkeeper.chats
{
  "version": 1,
  "data": {
    "123456789": {
      "conversation_id": "tg:123456789",
      "agent_id": "conversation.qwen_2_5_3b_conversation",
      "reply_mode": "voice",
      "language": "ru",
      "last_seen": "2026-11-19T09:12:00+03:00",
      "last_agent": "local"
    }
  }
}
```

Второго файла (`settings`) нет осознанно: глобальные настройки — это options config entry, и дублировать их в Store значит гарантированно получить расхождение. Store хранит только то, что меняется в рантайме и не является настройкой записи: состояние чата.

Никаких текстов сообщений и содержимого почты в хранилище. История — только внутри агента (`conversation_id`) с его собственным TTL.

---

## 7. Нефункциональные требования

| Категория | Требование |
|---|---|
| Локальность | По умолчанию ни один байт не уходит в облако, кроме Telegram. Облако — только явный opt-in (`allow_cloud_for_mail`, `/model`) |
| Приватность | Исходные OGG удаляются после распознавания; почта/календарь не логируются; `diagnostics` не содержит токенов и текстов |
| Производительность | Голосовое 5 с → текстовый ответ ≤ 8 с на локальном стеке (faster-whisper small + piper); голосовой ответ ≤ 12 с |
| Надёжность | Очередь на чат (`asyncio.Lock`), таймаут 60 с, повтор отправки 1 раз при сетевой ошибке Telegram |
| Идемпотентность | Дедуп по `(chat_id, message_id)` — после рестарта HA одно сообщение не обрабатывается дважды |
| Безопасность | Работаем только с allowlist; скачанные файлы — только внутри `.storage/hearthkeeper/`; проверка пути при чтении/удалении |
| Совместимость | HA 2026.11+, Python 3.13; `requires_auth` не ослабляем, своих HTTP-вью не добавляем |
| i18n | Строки в `strings.json` + `translations/{en,ru}.json`; sentences на двух языках |
| Качество | hassfest + HACS action зелёные; pytest с покрытием ядра ≥ 70% |

---

## 8. Структура репозитория

```
ha-hearthkeeper/
├── custom_components/hearthkeeper/
│   ├── __init__.py           # setup, services, брифинг-таймер
│   ├── manifest.json
│   ├── config_flow.py
│   ├── const.py
│   ├── transport.py          # bus-события, фильтры, дедуп
│   ├── voice.py              # ffmpeg decode/encode, Assist pipeline, TTS
│   ├── brain.py              # conversation, режимы ответа
│   ├── skills/
│   │   ├── base.py           # Skill protocol, реестр
│   │   ├── calendar.py
│   │   ├── mail.py
│   │   ├── home.py
│   │   ├── brief.py
│   │   └── todo.py
│   ├── store.py
│   ├── diagnostics.py
│   ├── services.yaml
│   ├── icons.json
│   ├── strings.json
│   ├── translations/{en,ru}.json
│   └── brand/{icon.png,logo.png}
├── tests/
│   ├── conftest.py           # hass fixture, фейковый transport
│   ├── test_transport.py
│   ├── test_voice.py
│   ├── test_skills.py
│   └── test_config_flow.py
├── .github/workflows/{hassfest.yml,hacs.yml,test.yml}
├── hacs.json
├── README.md · README.ru.md
└── LICENSE (MIT)
```

`manifest.json` (стартовая версия):

```json
{
  "domain": "hearthkeeper",
  "name": "Hearthkeeper",
  "codeowners": ["@mZer0ne"],
  "config_flow": true,
  "dependencies": ["conversation", "telegram_bot"],
  "after_dependencies": ["stt", "tts", "calendar", "imap", "todo"],
  "documentation": "https://github.com/ha-workshop/ha-hearthkeeper",
  "integration_type": "service",
  "iot_class": "local_push",
  "issue_tracker": "https://github.com/ha-workshop/ha-hearthkeeper/issues",
  "requirements": ["pyogg==0.6.14a1"],
  "version": "0.1.0"
}
```

`hacs.json`:

```json
{ "name": "Hearthkeeper", "homeassistant": "2026.11.0", "render_readme": true }
```

---

## 9. Этапы

| Этап | Объём | Результат | Оценка |
|---|---|---|---|
| **M0. Каркас** | manifest, config flow, transport, эхо-ответ | текст из Telegram → ответ текстом | 2 дня |
| **M1. Голос на вход** | download_file, ffmpeg → WAV, `async_pipeline_from_audio_stream`, ошибки | голосовое → распознанный текст → ответ | 2–3 дня |
| **M2. Голос на выход** | TTS-стрим, OGG/Opus, `/voice`, группы, дедуп | US-2, US-3, US-8, US-9 | 2 дня |
| **M3. Skills** | календарь, дом, брифинг, почта, todo, `/brief` | US-4…US-7, US-10 | 3–4 дня |
| **M4. Полировка** | `/model`, кнопки, diagnostics, HACS-пакет, доки | публикация | 2–3 дня |

**Definition of Done для MVP (M0–M3):**

- [ ] голосовое «какая погода» → голосовой ответ, оба файла удалены;
- [ ] «включи свет» исполняется локальным агентом;
- [ ] `/brief` отдаёт календарь, почту и дом;
- [ ] чужой chat_id не получает ничего;
- [ ] после рестарта HA дублей нет;
- [ ] hassfest и HACS action зелёные;
- [ ] README (en/ru) с установкой и скриншотами.

---

## 10. Тестирование

- **Юнит:** `voice.py` (OGG→PCM на фикстуре 3 с, PCM→OGG, проверка сигнатуры `OggS`), дедуп, парсинг событий.
- **Интеграция:** `pytest-homeassistant-custom-component`, фейковый transport вместо Telegram, фейковые STT/TTS-провайдеры.
- **Ручные чек-листы:** 5 голосовых разной длины; шумное помещение; русский и английский; группа; `/voice off`; отключённый интернет (Telegram недоступен — ошибка без падения HA).
- **Нагрузка:** 20 сообщений подряд в один чат — порядок сохранён, ответы не перемешиваются.

---

## 11. Риски

| Риск | Вероятность | Что делаем |
|---|---|---|
| Изменение формата bus-событий `telegram_bot` | средняя | изолировано в `transport.py`, есть резерв через event-сущность |
| `pyogg` без wheel под нужную архитектуру | низкая | fallback на `ffmpeg` (он есть в HA OS), выбор в настройках |
| Piper отдаёт WAV — Telegram не покажет «голосовое» | высокая, если пропустить | обязательное перекодирование в OGG/Opus, тест на сигнатуру |
| Локальная 3B-модель плохо понимает свободную речь | средняя | `prefer_local_intents`, короткие фразы в подсказках, `/model` для сложных вопросов |
| Почта в облаке | — | `allow_cloud_for_mail=false` по умолчанию, честная пометка в брифинге |
| Telegram блокируется в сети | низкая | polling уже работает у пользователя; ничего не добавляем |

---

## 12. Открытые вопросы (решить на M0)

1. ~~**Имя и домен:** `telegram_assistant` — или короче `tg_assistant`?~~ **Решено:** домен `hearthkeeper`, репозиторий `ha-workshop/ha-hearthkeeper`, display name `Hearthkeeper`. Бренд занят сторонними проектами (home automation hub, домашний AI-сервис), но в HA/HACS домен свободен; находимость обеспечиваем описанием «Telegram assistant for Home Assistant» и topics, а не самим именем.
2. **Skills как интенты или как tools агента?** Предложение: интенты в MVP, tools — в M4.
3. **Хранение контекста:** полагаться на TTL агента или держать свой короткий буфер (последние 6 реплик) для локальной модели?
4. **Почта:** IMAP-сенсоры HA или отдельный IMAP-клиент в компоненте? Предложение: только сенсоры HA, без своих соединений.
5. **Публикация:** сразу в HACS как custom repository, или сначала месяц личного использования?

---

## Приложение A. Лимиты и мелочи Telegram

- Скачивание ботом — до 20 МБ; отправка — до 50 МБ.
- Голосовое сообщение = OGG/Opus; иначе уходит как аудиофайл.
- `send_chat_action`: `typing`, `record_voice`, `upload_voice` — используем как индикатор.
- В группах бот по умолчанию не видит обычные сообщения (privacy mode) — нужен `@bot` или отключение privacy в BotFather.
- `message_thread_id` пробрасываем, чтобы работать в топиках форумов.
- После рестарта HA core сам восстанавливает offset polling — дубли возможны, поэтому дедуп обязателен.
