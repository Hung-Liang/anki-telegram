# anki-telegram

English | [繁體中文](README.zh-TW.md)

Send Japanese words or sentences over Telegram; the bot looks them up in JMdict, translates them, generates audio and adds them to Anki.

## Features

- Send a word or a whole Japanese sentence: it is tokenized with fugashi (unidic-lite), reduced to dictionary form and looked up in [JMdict](https://www.edrdg.org/jmdict/j_jmdict.html); words already in Anki are skipped, and each new word gets its own preview.
- Preview: word (reading), the first 3 senses (translation + English original), a Tatoeba example with its translation; for sentence input, the original sentence (target word in bold) and its translation.
- Buttons: 💾 Save adds the card to Anki (with optional edge-tts audio), 🗑 Discard drops it, ↩️ Undo deletes the card just saved.
- Syncs through a self-hosted Anki sync server (`ANKI_SYNC_ENDPOINT`, required; 2-hour debounce, restarted on every add / delete; `/sync` syncs right away; when the last sync is more than 72 hours old, a database sync runs before the message is processed), so the new cards show up after mobile and desktop Anki sync.
- Only answers a single whitelisted Telegram account.

## Requirements

- Docker (with the compose plugin); verified on x86_64.
- A self-hosted Anki sync server (e.g. `python -m anki.syncserver`, see the [Anki manual](https://docs.ankiweb.net/sync-server.html)) that mobile / desktop Anki also sync with, with the note type and deck already created in desktop Anki (see "Initialization").
- A Telegram bot token (from [@BotFather](https://t.me/BotFather)) and your own Telegram user id.
- Optional: a Gemini or DeepL (untested) API key (translates into English by default, or another language such as Traditional Chinese; without one only the English senses are shown).

## Usage

| Input | Description |
| --- | --- |
| Japanese word or sentence | One preview per new word; replies "No new words" when there are none, and lists words that were not found separately |
| `/sync` | Sync with the sync server now |
| `/stats` | Cards added today, total cards in the deck, last sync time |
| `/help` | Help |

Previews expire after `PENDING_TTL_HOURS` (24 hours by default); Undo has no such limit and still works after a bot restart.

The bot's messages are in English by default; set `BOT_LANGUAGE` to switch (see "UI language and text").

## Quick start

1. Create the note type and deck in desktop Anki as in step 1 of "Initialization", and sync to the sync server.
2. Prepare `.env` and `data/` as in "Deployment", run bootstrap, then `docker compose up -d --build`.
3. Send "食べた" to the bot on Telegram to test.

## Development environment (devcontainer)

1. Copy `.env.example` to `.env.dev` and fill in test values (test bot token, sync server credentials; `ANKI_SYNC_ENDPOINT=http://anki-sync:8080/` for local-sync).
2. Open it with VS Code "Reopen in Container". The container user is `app` (uid 1000) and `/data` is a named volume.
3. Common commands:

| Command | Description |
| --- | --- |
| `make test` | Unit tests (skips `integration`) |
| `make test-integration` | Integration tests (start local-sync first) |
| `make lint` / `make format` | ruff |
| `make verify-sync` | Sync API check against `ANKI_SYNC_ENDPOINT` (point it at local-sync; needs an empty server account) |
| `make verify-sync-readonly` | Sync API check against `ANKI_SYNC_ENDPOINT` (read-only: login, full download, no-change sync) |
| `make run` | Start the bot |
| `make bootstrap` | First-time initialization |
| `make build-tatoeba` | Download Tatoeba and build the example database `$DATA_DIR/tatoeba.db` |

### local-sync (local sync server)

Run on the **host** (there is no docker inside the devcontainer):

```sh
make local-sync-up      # start anki-sync (credentials from SYNC_USER1, default user:pass)
make local-sync-reset   # wipe the server data and restart (needed before `make verify-sync`)
```

Inside the container, connect to `http://anki-sync:8080/`.

## Initialization (bootstrap)

1. In desktop Anki, create the note type `JP Bot Vocab` (fields `Word`, `Reading`, `Meaning`, `MeaningTL`, `Sentence`, `SentenceTL`, `ExampleJP`, `ExampleTL`, `Audio`, `Source`; extra fields are allowed and order does not matter) and the deck (`Japanese::Bot` by default), sync to the sync server, and make a backup first. Paste `front.html`, `back.html` and `style.css` from `templates/jp-bot-vocab/` into the card template's Front Template / Back Template / Styling. Later edits to the template HTML / CSS do not trigger a full sync, but adding, removing, renaming or reordering fields, or adding or removing card types, do.
2. With the bot stopped, run (dry-run first to review, then add `--yes`):
   - First-time initialization: `docker compose run --rm bot python -m ankibot.bootstrap`. Refuses to run when the local collection is not empty.
   - Recovery after a full sync: `docker compose run --rm bot python -m ankibot.bootstrap --force`. Lists local changes that have not been synced (adds / edits list `Word` and `Reading`, deletions only a count); these changes are lost by the download. The old `collection.anki2` (with `-wal` and `-shm`) and `collection.media.db2` are renamed to `backup-<timestamp>-<original name>`; the media folder is left alone. On success the `full_sync_required` marker file is deleted.
3. After the download, the note type, required fields, deck and `DEDUP_EXTRA_SOURCES` are checked; any mismatch is an error and nothing is created automatically.

When `DEDUP_EXTRA_SOURCES` has no `ReadingField`, matching is by written form only, and a kana spelling can match homophones (e.g. saving "かみ" makes 紙／神／髪 all count as existing), so set `ReadingField` whenever possible.

## Deployment (Docker)

1. Prepare `.env` (copy from `.env.example` and fill in the production bot token, whitelisted user id, sync server credentials and `ANKI_SYNC_ENDPOINT`; the bot refuses to start when it is empty). Set `TZ` (e.g. `Asia/Taipei`, default UTC) so that "today" in `/stats` follows your day.
2. Create the data folder and hand it to the container user (uid / gid 1000): `mkdir -p data && sudo chown 1000:1000 data`. Use 1000 even if the host user's uid differs, or rebuild the image with the build args `APP_UID` / `APP_GID`.
3. Run bootstrap as in "Initialization", then `docker compose up -d --build`. Optionally run `docker compose run --rm bot python scripts/build_tatoeba.py` afterwards to build the example database.
4. Check status: `docker compose ps` (includes the healthcheck result), `docker compose logs -f bot`.

Notes:

- **Only one bot container per `data/`**: at startup the bot takes an exclusive lock on `/data/.lock` and exits if it cannot. Bootstrap must also run while the bot is stopped.
- **Healthcheck**: the bot calls Telegram `getMe` every minute and only touches `/tmp/ankibot-heartbeat` on success; `python -m ankibot.healthcheck` checks that the file was updated within 3 minutes. It only guarantees that the event loop is running and can reach the Telegram API; it **does not** guarantee that the polling loop still receives updates, nor that sync works.
- **Graceful shutdown**: `docker compose stop` sends SIGTERM (`stop_grace_period: 60s`). The bot stops receiving updates → cancels Saves still generating audio (shown as "⚠️ Interrupted, press Save again" on the next start) → runs a final sync if there are unsynced changes (at most `SHUTDOWN_SYNC_TIMEOUT_SECONDS`, 45 seconds by default) → closes the collection and writes the preview state. On timeout, or when there is nothing to upload but a sync is still running (e.g. the first media download), it does not wait or close the collection; it writes the preview state and exits immediately (exit code 1). Committed data is kept by the SQLite WAL, and an unfinished sync is redone on the next start.
- **Backups**: `data/collection.anki2` is the important data. To back up, `docker compose stop` first, then copy the whole `data/` (including `collection.anki2-wal` if present); or rely on Anki's built-in backups on another device (desktop).
- **Disk space**: media sync downloads **all** media of the whole collection to `data/collection.media/` (not only the bot's audio). Check the total media size in desktop Anki before deploying and leave room; the first media sync can take a long time, and Saves during it are queued, not lost.
- **Undo deletes the note outright**: even if the card was already synced to other devices and reviewed, its review history goes with it, with no confirmation step.
- **Known full sync limitation**: notes written between a schema change in desktop Anki and the bot detecting the full sync request are lost by the full download of `bootstrap --force` (`--force` lists these changes first). Once detected, the bot pauses all writes.

## Optional features

- **Audio (edge-tts, off by default)**: set `TTS_PROVIDER=edge` to enable it. On Save, `Reading` is spoken with `TTS_VOICE`; the file is stored in `/data/tts/` (named by a hash of `(voice, text)`) and then imported into Anki media. edge-tts is an unofficial endpoint; on failure the card is still created, `Audio` is left empty and "Audio generation failed" is shown.
- **Examples (Tatoeba)**: run `make build-tatoeba` (or `docker compose run --rm bot python scripts/build_tatoeba.py`) to download Japanese sentences and build `/data/tatoeba.db`; without the file, examples are disabled. The index stores fugashi / unidic-lite tokenization results, so **rerun it after upgrading either package**. Source: [Tatoeba](https://tatoeba.org), licensed [CC BY 2.0 FR](https://creativecommons.org/licenses/by/2.0/fr/).
- **Translation (DeepL or Gemini, pick one)**: with `DEEPL_API_KEY` or `GEMINI_API_KEY` set, senses, the original sentence and examples are translated into the target language (English by default); setting both fails at startup. Successful results are cached in `/data/translations.db` (separately for each provider). Without a key only English is shown and no requests are made at all; on failure the preview shows English only plus a "Translation failed" line.
  - DeepL (**untested**: unit tests only, never verified with a real API key): Free keys ending in `:fx` automatically use the Free endpoint.
  - Gemini: default model `gemini-3.5-flash-lite` (change with `GEMINI_MODEL`); when the free quota runs out it returns HTTP 429 and the preview falls back to English only. About 2 requests per new word, no retries; in practice, a long sentence with about 60 new words exceeds the per-minute limit and most previews fail to translate (recovers after about a minute; on resend, translations that succeeded come from the cache). On the free tier, Google may use the input to improve its models.
- **Translation target language**:
  - `TRANSLATION_TARGET`: a DeepL `target_lang` code, `EN-US` by default, e.g. `ZH-HANT`, `ZH-HANS`, `KO`. DeepL does not accept a bare `EN`; use `EN-US` or `EN-GB`.
  - `TRANSLATION_TARGET_NAME`: the language name in the Gemini prompt (e.g. `Korean`). When empty, `ZH-HANT` uses "Traditional Chinese as used in Taiwan" and other languages use the code itself.
  - The cache is kept per target language, so switching languages does not require clearing `translations.db`.
  - `lang="en"` in the card template `back.html` does not follow the setting; change it to the matching language yourself (e.g. `zh-TW` for Traditional Chinese).
- **English target** (`EN`, `EN-US`, `EN-GB`; the default): the JMdict senses are already in English, so the translation API is **not** called for them. The original sentence and examples are Japanese, though, so with an API key set the API **is still** called to translate them into English:

  | Input | Calls the translation API? |
  | --- | --- |
  | Word, no example | No |
  | Word with a Tatoeba example | Yes (the example) |
  | Sentence | Yes (the sentence, and the example if there is one) |

  To make no API calls at all, leave `DEEPL_API_KEY` / `GEMINI_API_KEY` unset (the sentence and examples are then shown in Japanese only).

## UI language and text

Every bot message (statuses, buttons, command replies, notifications) comes from `src/ankibot/locales/<language>.toml`.

- `BOT_LANGUAGE`: the built-in language, `en` by default; `en` and `zh-Hant` are available.
- `BOT_MESSAGES_FILE`: optional path to your own TOML file (e.g. `/data/messages.toml`) holding only the keys you want to change; the rest comes from `BOT_LANGUAGE`. No image rebuild needed.
- Adding a language: copy `en.toml` to `<code>.toml` (e.g. `ja.toml`), translate every value, rebuild, and set `BOT_LANGUAGE=<code>`.
- Format rules: values are plain text (escaped automatically, no HTML); placeholders such as `{words}` and `{time}` must match `en.toml`, and a literal brace is written `{{` / `}}`. Missing keys, unknown keys or mismatched placeholders fail at startup.
- The card templates (`templates/jp-bot-vocab/`) and the bootstrap CLI output are not covered.

## License

[AGPL-3.0-or-later](LICENSE), the same license as the [Anki](https://github.com/ankitects/anki) library this project depends on.

Data used at runtime keeps its own license:

- [JMdict](https://www.edrdg.org/jmdict/j_jmdict.html) (via jamdict-data): property of the [Electronic Dictionary Research and Development Group](https://www.edrdg.org/), used under [CC BY-SA 4.0](https://www.edrdg.org/edrdg/licence.html).
- [Tatoeba](https://tatoeba.org/) sentences (downloaded by `scripts/build_tatoeba.py`): [CC BY 2.0 FR](https://creativecommons.org/licenses/by/2.0/fr/).

## ☕ Support Me

<a href="https://www.buymeacoffee.com/hungliang" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/default-orange.png" alt="Buy Me A Coffee" height="41" width="174"></a>
