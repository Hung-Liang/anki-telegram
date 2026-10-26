import asyncio
import threading
from pathlib import Path

import pytest
from anki_helpers import DECK, NOTETYPE, PLAIN, add_raw_note, make_collection
from telegram import Update
from telegram.ext import PicklePersistence
from telegram_fake import ALLOWED_USER, OTHER_USER, FakeTelegram
from test_tts import FakeCommunicate

from ankibot.anki_service import AnkiService
from ankibot.bot import BotHandlers, build_application
from ankibot.config import Settings
from ankibot.i18n import load_texts
from ankibot.pipeline import Lookup, Pipeline
from ankibot.state import PreviewStatus, get_previews
from ankibot.sync import DbSyncResult, FullSyncFlag, SyncManager
from ankibot.threads import DedicatedThread

TTL_HOURS = 24
T = load_texts()
EXPIRED = T.answer_expired


class StubBackend:
    """No network; `gate` (threading.Event) can hold a database sync on the Anki thread."""

    def __init__(self):
        self.gate = None
        self.entered = threading.Event()
        self.syncs = 0
        self.required = "NO_CHANGES"

    def login(self):
        from anki.sync import SyncAuth

        return SyncAuth(hkey="k")

    def sync_collection(self, auth):
        self.syncs += 1
        self.entered.set()
        if self.gate is not None:
            assert self.gate.wait(10)
        return DbSyncResult(self.required, None)

    def start_media_sync(self, auth):
        return None

    def media_sync_active(self):
        return False

    def has_local_changes(self):
        return False

    def rebuild_known_words(self):
        return None


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


class Env:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.settings = Settings(
            tg_token="123:ABC",
            tg_allowed_user_id=ALLOWED_USER,
            anki_user="user",
            anki_password="pass",
            anki_sync_endpoint="http://anki-sync:8080/",
            anki_deck=DECK,
            anki_notetype=NOTETYPE,
            dedup_extra_sources="",
            data_dir=tmp_path,
            pending_ttl_hours=TTL_HOURS,
        )
        self.fake = FakeTelegram()
        self.clock = Clock()
        self.sleeps = []
        self.backend = StubBackend()
        self.flag = FullSyncFlag(self.settings.full_sync_marker_path)
        self.anki = DedicatedThread("anki-bot-test")
        self.lookup_thread = DedicatedThread("lookup-bot-test")
        self.app = None

    async def sleep(self, seconds):
        self.sleeps.append(seconds)

    async def open(self):
        make_collection(self.tmp_path)
        self.service = await self.anki.run(AnkiService.open, self.settings, self.flag.is_set)
        self.lookup = await self.lookup_thread.run(Lookup.create, False)
        self.sync = SyncManager(
            self.anki,
            self.service,
            self.backend,
            self.flag,
            debounce_seconds=3600,
            stale_hours=6,
            pre_sync_timeout_seconds=15,
            retries=0,
        )
        # Fresh enough that messages do not trigger a pre-sync.
        self.sync.last_sync_at = self.sync._now()

    async def start_bot(self, tts=None, translator=None):
        pipeline = Pipeline(
            self.lookup_thread, self.lookup, self.service.known_words, self.sync.ensure_fresh, translator
        )

        async def stats():
            return await self.anki.run(self.service.stats)

        self.handlers = BotHandlers(
            self.settings, pipeline, self.sync, stats, tts, now=self.clock, sleep=self.sleep
        )
        persistence = PicklePersistence(self.tmp_path / "bot_state.pickle")
        self.app = build_application(self.settings, self.handlers, request=self.fake, persistence=persistence)
        await self.app.initialize()
        await self.handlers.recover(self.app)
        return self.app

    async def restart(self, **kwargs):
        await self.app.shutdown()
        return await self.start_bot(**kwargs)

    async def send(self, text, uid=ALLOWED_USER):
        await self.app.process_update(self.fake.text_update(self.app.bot, text, uid))

    async def press(self, message_id, data, uid=ALLOWED_USER, with_message=True):
        update = self.fake.callback_update(self.app.bot, message_id, data, uid, with_message)
        await self.app.process_update(update)

    def previews(self):
        return [m for m in self.fake.sent() if "save" in self.fake.buttons(ALLOWED_USER, m["message_id"])]

    def text(self, message_id):
        return self.fake.messages[(ALLOWED_USER, message_id)]["text"]

    def buttons(self, message_id):
        return self.fake.buttons(ALLOWED_USER, message_id)

    def user_previews(self):
        return get_previews(self.app.user_data[ALLOWED_USER])

    async def words(self):
        def read():
            col = self.service.collection
            return sorted(col.get_note(n)["Word"] for n in col.find_notes(f'"note:{NOTETYPE}"'))

        return await self.anki.run(read)

    async def close(self):
        if self.app is not None:
            await self.app.shutdown()
        await self.sync.shutdown(5)
        await self.lookup_thread.run(self.lookup.close)
        await self.anki.run(self.service.close)
        self.anki.shutdown()
        self.lookup_thread.shutdown()


@pytest.fixture
async def env(tmp_path):
    e = Env(tmp_path)
    await e.open()
    await e.start_bot()
    try:
        yield e
    finally:
        await e.close()


def last_line(text):
    return text.rsplit("\n", 1)[-1]


async def save_first(env, text="食べた"):
    await env.send(text)
    mid = env.previews()[-1]["message_id"]
    await env.press(mid, "save")
    return mid


# --- previews -------------------------------------------------------------------


async def test_each_candidate_gets_its_own_preview_in_order(env):
    await env.send("猫が魚を食べた。")
    previews = env.previews()
    assert [p["text"].splitlines()[1] for p in previews] == ["猫（ねこ）", "魚（さかな）", "食べる（たべる）"]
    for p in previews:
        assert p["text"].splitlines()[0] == "猫が魚を食べた。"
        assert env.buttons(p["message_id"]) == ["save", "discard"]
    assert set(env.user_previews()) == {p["message_id"] for p in previews}


async def test_no_new_words_lists_existing(env):
    await save_first(env, "猫")
    await env.send("猫")
    assert env.fake.sent()[-1]["text"] == "No new words\nAlready in Anki: 猫"


async def test_not_found_words_are_listed_last(env):
    await env.send("ﾌｶﾞﾎｹﾞが来た。")
    sent = env.fake.sent()
    assert sent[-1]["text"] == "Not found: ﾌｶﾞﾎｹﾞ"
    assert [p["text"].splitlines()[1] for p in env.previews()] == ["来る（くる）"]


async def test_very_long_sentence_preview_is_sent(env):
    await env.send("あ" * 400 + "猫が好き。" + "い" * 3000)
    previews = env.previews()
    assert previews
    assert any("猫" in p["text"].splitlines()[0] for p in previews)


async def test_rate_limit_is_retried_in_order(env):
    env.fake.fail("sendMessage", env.fake.retry_after(3))
    await env.send("猫が魚を食べた。")
    assert env.sleeps == [3]
    assert [p["text"].splitlines()[1] for p in env.previews()] == [
        "猫（ねこ）",
        "魚（さかな）",
        "食べる（たべる）",
    ]


# --- save / discard -------------------------------------------------------------


async def test_save_writes_only_that_candidate_and_offers_undo(env):
    await env.send("猫が魚を食べた。")
    cat, fish, _ = (p["message_id"] for p in env.previews())
    await env.press(fish, "save")
    assert await env.words() == ["魚"]
    assert last_line(env.text(fish)) == T.status_saved
    assert env.buttons(fish)[0].startswith("undo:")
    assert fish not in env.user_previews() and cat in env.user_previews()
    assert env.sync.has_pending_changes


async def test_saved_card_fields(env):
    await env.send("<猫> & 犬")
    cat = next(p for p in env.previews() if p["text"].splitlines()[1] == "猫（ねこ）")
    await env.press(cat["message_id"], "save")

    def note():
        col = env.service.collection
        return col.get_note(col.find_notes('"Word:猫"')[0])

    n = await env.anki.run(note)
    assert n["Sentence"] == "&lt;<b>猫</b>&gt; &amp; 犬"
    assert n["Source"] == "&lt;猫&gt; &amp; 犬"
    assert n["Audio"] == ""


async def test_discard_writes_nothing(env):
    await env.send("食べた")
    mid = env.previews()[0]["message_id"]
    await env.press(mid, "discard")
    assert await env.words() == []
    assert last_line(env.text(mid)) == T.status_discarded
    assert env.buttons(mid) == []
    assert mid not in env.user_previews()
    await env.press(mid, "save")
    assert env.fake.answers()[-1] == EXPIRED
    assert await env.words() == []


async def test_double_save_adds_one_note(env):
    await env.send("食べた")
    mid = env.previews()[0]["message_id"]
    await asyncio.gather(env.press(mid, "save"), env.press(mid, "save"))
    assert await env.words() == ["食べる"]
    assert EXPIRED in env.fake.answers()


async def test_expired_preview_cannot_be_saved(env):
    await env.send("食べた")
    mid = env.previews()[0]["message_id"]
    env.clock.t += TTL_HOURS * 3600
    await env.press(mid, "save")
    assert env.fake.answers()[-1] == EXPIRED
    assert await env.words() == []


async def test_cleanup_removes_expired_and_persists(env, tmp_path):
    await env.send("食べた")
    env.clock.t += TTL_HOURS * 3600
    assert len(await env.handlers.cleanup_once(env.app)) == 1
    assert env.user_previews() == {}
    data = await PicklePersistence(tmp_path / "bot_state.pickle").get_user_data()
    assert data[ALLOWED_USER]["previews"] == {}


async def test_existing_word_on_save(env):
    await env.send("食べた")
    mid = env.previews()[0]["message_id"]

    def add_elsewhere():
        add_raw_note(env.service.collection, NOTETYPE, Word="食べる", Reading="たべる")

    await env.anki.run(add_elsewhere)
    await env.press(mid, "save")
    assert last_line(env.text(mid)) == T.status_exists
    assert env.buttons(mid) == []
    assert await env.words() == ["食べる"]


async def test_full_sync_required_keeps_buttons(env):
    await env.send("食べた")
    mid = env.previews()[0]["message_id"]
    env.flag.set()
    await env.press(mid, "save")
    assert last_line(env.text(mid)) == T.status_blocked
    assert env.buttons(mid) == ["save", "discard"]
    assert env.user_previews()[mid].status is PreviewStatus.PENDING
    assert await env.words() == []


async def test_save_failure_restores_buttons(env, monkeypatch):
    await env.send("食べた")
    mid = env.previews()[0]["message_id"]

    async def boom(card):
        raise RuntimeError("disk full")

    monkeypatch.setattr(env.sync, "add_note", boom)
    await env.press(mid, "save")
    assert last_line(env.text(mid)) == T.status_failed
    assert env.buttons(mid) == ["save", "discard"]
    assert env.user_previews()[mid].status is PreviewStatus.PENDING


async def test_save_during_sync_waits_and_then_saves(env):
    await env.send("食べた")
    mid = env.previews()[0]["message_id"]
    env.backend.gate = threading.Event()
    sync_task = asyncio.create_task(env.sync.sync_now())
    await asyncio.to_thread(env.backend.entered.wait, 5)
    save_task = asyncio.create_task(env.press(mid, "save"))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if env.fake.count("editMessageText"):
            break
    assert last_line(env.text(mid)) == T.status_queued
    assert not save_task.done()
    env.backend.gate.set()
    await sync_task
    await save_task
    assert last_line(env.text(mid)) == T.status_saved
    assert await env.words() == ["食べる"]


async def test_saved_edit_failure_sends_new_message_with_undo(env):
    await env.send("食べた")
    mid = env.previews()[0]["message_id"]
    # First edit (⏳) works, the final one fails as if the message had been deleted.
    original = env.fake._handle

    def handle(name, params):
        if name == "editMessageText" and T.status_saved in params["text"]:
            raise KeyError("gone")
        return original(name, params)

    env.fake._handle = handle
    await env.press(mid, "save")
    assert await env.words() == ["食べる"]
    reply = env.fake.sent()[-1]
    assert reply["text"] == "食べる（たべる）\n" + T.status_saved
    assert env.buttons(reply["message_id"])[0].startswith("undo:")
    assert mid not in env.user_previews()


# --- undo -----------------------------------------------------------------------


async def test_undo_deletes_only_that_note(env):
    await env.send("猫が魚を食べた。")
    cat, fish, _ = (p["message_id"] for p in env.previews())
    await env.press(cat, "save")
    await env.press(fish, "save")
    await env.press(cat, env.buttons(cat)[0])
    assert await env.words() == ["魚"]
    assert last_line(env.text(cat)) == T.status_undone
    assert env.buttons(cat) == []
    assert not env.service.known_words().contains("猫", "ねこ")


async def test_double_undo_deletes_once(env):
    mid = await save_first(env)
    undo = env.buttons(mid)[0]
    await env.press(mid, undo)
    # The second press still carries the old message with its Undo button.
    env.fake.messages[(ALLOWED_USER, mid)]["reply_markup"] = {
        "inline_keyboard": [[{"text": "↩️ Undo", "callback_data": undo}]]
    }
    await env.press(mid, undo)
    assert env.fake.answers()[-1] == "The card no longer exists"
    assert await env.words() == []


async def test_undo_works_without_user_data(env):
    mid = await save_first(env)
    env.app.user_data[ALLOWED_USER].clear()
    await env.press(mid, env.buttons(mid)[0])
    assert await env.words() == []
    assert last_line(env.text(mid)) == T.status_undone


async def test_undo_rejects_other_notetype(env):
    def add():
        return add_raw_note(env.service.collection, PLAIN, Front="x", Back="y")

    nid = await env.anki.run(add)
    mid = await save_first(env)
    await env.press(mid, f"undo:{nid}")
    assert env.fake.answers()[-1] == "This card is no longer the bot's note type; not deleted"
    assert env.buttons(mid) == []
    assert await env.anki.run(lambda: env.service.collection.note_count()) == 2


async def test_undo_keeps_bold_and_escaped_text(env):
    await env.send("a<b>&c 猫が好き")
    cat = next(p for p in env.previews() if p["text"].splitlines()[1] == "猫（ねこ）")
    mid = cat["message_id"]
    await env.press(mid, "save")
    await env.press(mid, env.buttons(mid)[0])
    msg = env.fake.messages[(ALLOWED_USER, mid)]
    assert msg["text"].splitlines()[0] == "a<b>&c 猫が好き"
    assert last_line(msg["text"]) == T.status_undone
    bold = [e for e in msg["entities"] if e["type"] == "bold"]
    assert {"type": "bold", "offset": 7, "length": 1} in bold


async def test_undo_without_message_only_answers(env):
    mid = await save_first(env)
    await env.press(mid, env.buttons(mid)[0], with_message=False)
    assert await env.words() == []
    assert env.fake.answers()[-1] == T.status_undone


async def test_undo_blocked_by_full_sync_keeps_button(env):
    mid = await save_first(env)
    env.flag.set()
    await env.press(mid, env.buttons(mid)[0])
    assert env.fake.answers()[-1] == "Full sync required; undo is paused"
    assert env.buttons(mid)[0].startswith("undo:")
    assert await env.words() == ["食べる"]


# --- persistence / restart ------------------------------------------------------


async def test_pending_preview_survives_restart(env):
    await env.send("食べた")
    mid = env.previews()[0]["message_id"]
    await env.restart()
    await env.press(mid, "save")
    assert await env.words() == ["食べる"]
    assert last_line(env.text(mid)) == T.status_saved


async def test_saving_state_is_recovered_on_startup(env):
    await env.send("食べた")
    mid = env.previews()[0]["message_id"]
    previews = env.user_previews()
    previews[mid] = previews[mid].with_status(PreviewStatus.SAVING)
    env.app.mark_data_for_update_persistence(user_ids=[ALLOWED_USER])
    await env.restart()
    assert env.user_previews()[mid].status is PreviewStatus.PENDING
    assert last_line(env.text(mid)) == T.status_interrupted
    assert env.buttons(mid) == ["save", "discard"]
    data = await PicklePersistence(env.tmp_path / "bot_state.pickle").get_user_data()
    assert data[ALLOWED_USER]["previews"][mid].status is PreviewStatus.PENDING
    await env.press(mid, "save")
    await env.press(mid, "save")
    assert await env.words() == ["食べる"]


async def test_saving_recovery_after_note_was_written_reports_existing(env):
    await env.send("食べた")
    mid = env.previews()[0]["message_id"]
    previews = env.user_previews()
    await env.sync.add_note(previews[mid].card)
    previews[mid] = previews[mid].with_status(PreviewStatus.SAVING)
    env.app.mark_data_for_update_persistence(user_ids=[ALLOWED_USER])
    await env.restart()
    await env.press(mid, "save")
    assert last_line(env.text(mid)) == T.status_exists
    assert await env.words() == ["食べる"]


async def test_corrupt_persistence_starts_empty(env):
    await env.app.shutdown()
    (env.tmp_path / "bot_state.pickle").write_bytes(b"garbage")
    from ankibot.bot import prepare_persistence

    persistence = prepare_persistence(env.tmp_path / "bot_state.pickle")
    assert await persistence.get_user_data() == {}
    assert list(env.tmp_path.glob("bot_state.pickle.corrupt-*"))
    await env.start_bot()


# --- whitelist and commands -----------------------------------------------------


async def test_other_users_are_ignored(env):
    await env.send("食べた")
    mid = env.previews()[0]["message_id"]
    calls = len(env.fake.calls)
    await env.send("猫", uid=OTHER_USER)
    await env.press(mid, "save", uid=OTHER_USER)
    await env.send("/sync", uid=OTHER_USER)
    assert len(env.fake.calls) == calls
    assert await env.words() == []
    assert env.backend.syncs == 0


async def test_other_users_cannot_undo_or_discard(env):
    saved = await save_first(env)
    await env.send("猫")
    pending = env.previews()[-1]["message_id"]
    calls = len(env.fake.calls)
    await env.press(saved, env.buttons(saved)[0], uid=OTHER_USER)
    await env.press(pending, "discard", uid=OTHER_USER)
    await env.send("/stats", uid=OTHER_USER)
    await env.send("/help", uid=OTHER_USER)
    assert len(env.fake.calls) == calls
    assert await env.words() == ["食べる"]
    assert pending in env.user_previews()


async def test_other_users_inline_callbacks_are_ignored(env):
    await env.send("食べた")
    mid = env.previews()[0]["message_id"]
    calls = len(env.fake.calls)
    await env.press(mid, "save", uid=OTHER_USER, with_message=False)
    assert len(env.fake.calls) == calls
    assert await env.words() == []


async def test_updates_without_a_user_are_ignored(env):
    calls = len(env.fake.calls)
    post = {
        "update_id": 999,
        "channel_post": {
            "message_id": 1,
            "date": 0,
            "chat": {"id": -100, "type": "channel"},
            "text": "食べた",
        },
    }
    await env.app.process_update(Update.de_json(post, env.app.bot))
    assert len(env.fake.calls) == calls
    assert await env.words() == []


async def test_sync_command_reports_result(env):
    await env.send("/sync")
    assert env.backend.syncs == 1
    assert env.fake.sent()[-1]["text"].startswith("✅ Sync finished")


async def test_sync_command_shows_progress_then_result(env):
    env.backend.gate = threading.Event()
    task = asyncio.create_task(env.send("/sync"))
    await asyncio.to_thread(env.backend.entered.wait, 5)
    for _ in range(50):
        if env.fake.sent():
            break
        await asyncio.sleep(0.01)
    progress = env.fake.sent()[-1]
    assert progress["text"] == "⏳ Syncing…"
    env.backend.gate.set()
    await task
    assert env.fake.sent()[-1]["message_id"] == progress["message_id"]
    assert env.text(progress["message_id"]).startswith("✅ Sync finished")


async def test_stats_command(env):
    await save_first(env)
    env.flag.set()
    await env.send("/stats")
    text = env.fake.sent()[-1]["text"]
    assert "Added today: 1" in text
    assert "Cards in deck: 1" in text
    assert "Last sync: " in text
    assert "⛔ Full sync required" in text


async def test_help_command(env):
    await env.send("/help")
    assert "/sync" in env.fake.sent()[-1]["text"]


async def test_tts_failure_hint_is_above_status(env):
    class FailingTTS:
        async def synth(self, text):
            raise RuntimeError("tts down")

    await env.restart(tts=FailingTTS())
    mid = await save_first(env)
    lines = env.text(mid).splitlines()
    assert lines[-2:] == [T.hint_tts_failed, T.status_saved]
    assert await env.words() == ["食べる"]


# --- translation, TTS, examples -------------------------------------------------


class RecordingTranslator:
    translates_glosses = True

    def __init__(self):
        self.calls = []

    async def translate(self, texts, source_lang, context=None):
        self.calls.append((tuple(texts), source_lang, context))
        return [f"中:{t}" for t in texts]


def fake_edge(script=()):
    return FakeCommunicate(list(script))


async def test_preview_shows_translation_and_save_writes_meaning_tl(env):
    translator = RecordingTranslator()
    await env.restart(translator=translator)
    await env.send("食べた")
    mid = env.previews()[0]["message_id"]
    lines = env.text(mid).splitlines()
    assert lines[1].startswith("1. 中:") and not lines[2].startswith("1.")
    calls_before_save = len(translator.calls)
    await env.press(mid, "save")
    assert len(translator.calls) == calls_before_save

    def meaning_tl():
        col = env.service.collection
        return col.get_note(col.find_notes('"Word:食べる"')[0])["MeaningTL"]

    assert (await env.anki.run(meaning_tl)).startswith("中:")


async def test_sentence_is_translated_once_for_all_candidates(env):
    translator = RecordingTranslator()
    await env.restart(translator=translator)
    await env.send("猫が魚を食べた。")
    sentence_calls = [c for c in translator.calls if c[1] == "JA" and c[0] == ("猫が魚を食べた。",)]
    assert len(sentence_calls) == 1
    previews = env.previews()
    assert len(previews) == 3
    assert all(p["text"].splitlines()[1] == "中:猫が魚を食べた。" for p in previews)
    sense_calls = [c for c in translator.calls if c[1] == "EN"]
    assert len(sense_calls) == 3, "one request per candidate with all of its senses"
    assert all("猫が魚を食べた。" in c[2] for c in sense_calls)


async def test_deepl_timeout_still_sends_english_preview_in_time(env, tmp_path):
    import httpx
    from pydantic import SecretStr

    from ankibot.translator import DeepLTranslator, TranslationCache

    async def hang(request):
        await asyncio.sleep(30)

    cache = await env.lookup_thread.run(TranslationCache, tmp_path / "translations.db")
    client = httpx.AsyncClient(transport=httpx.MockTransport(hang))
    translator = DeepLTranslator(
        SecretStr("k:fx"), 0.1, cache, env.lookup_thread, target="ZH-HANT", client=client
    )
    await env.restart(translator=translator)
    loop = asyncio.get_running_loop()
    start = loop.time()
    await env.send("猫が好き。")
    assert loop.time() - start < 2
    previews = env.previews()
    assert previews
    for p in previews:
        lines = p["text"].splitlines()
        assert lines[-1] == "Translation failed"
        assert not any(line.startswith("   ") for line in lines), "English only"
    await client.aclose()
    await env.lookup_thread.run(cache.close)


async def test_tts_timeout_still_saves_card_in_time(env, tmp_path):
    from ankibot.tts import EdgeTTS

    async def no_sleep(seconds):
        return None

    tts = EdgeTTS(
        tmp_path / "tts", "ja-JP-NanamiNeural", 0.05, communicate=fake_edge(["hang"] * 3), sleep=no_sleep
    )
    await env.restart(tts=tts)
    loop = asyncio.get_running_loop()
    start = loop.time()
    mid = await save_first(env)
    assert loop.time() - start < 2
    assert env.text(mid).splitlines()[-2:] == [T.hint_tts_failed, T.status_saved]
    assert await env.words() == ["食べる"]


async def test_audio_field_uses_sound_tag_with_media_file(env, tmp_path):
    from ankibot.tts import EdgeTTS

    tts = EdgeTTS(tmp_path / "tts", "ja-JP-NanamiNeural", 1, communicate=fake_edge())
    await env.restart(tts=tts)
    await save_first(env)

    def audio():
        col = env.service.collection
        field = col.get_note(col.find_notes('"Word:食べる"')[0])["Audio"]
        return field, col.media.dir()

    field, media_dir = await env.anki.run(audio)
    assert field.startswith("[sound:tts-") and field.endswith(".mp3]")
    name = field.removeprefix("[sound:").removesuffix("]")
    assert (Path(media_dir) / name).read_bytes() == "ja-JP-NanamiNeural:たべる".encode()
    text, voice, _ = FakeCommunicate.instances[-1]
    assert (text, voice) == ("たべる", "ja-JP-NanamiNeural"), "TTS reads the reading, not the word"


class GatedTranslator(RecordingTranslator):
    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()

    async def translate(self, texts, source_lang, context=None):
        await self.gate.wait()
        return await super().translate(texts, source_lang, context)


async def test_preview_is_sent_before_translation_and_edited_after(env):
    translator = GatedTranslator()
    await env.restart(translator=translator)
    task = asyncio.create_task(env.send("食べた"))
    for _ in range(50):
        if env.fake.sent():
            break
        await asyncio.sleep(0.01)
    [placeholder] = env.fake.sent()
    mid = placeholder["message_id"]
    assert placeholder["text"].splitlines()[1] == "1. Translating…"
    assert env.buttons(mid) == []
    assert mid not in env.user_previews()
    translator.gate.set()
    await task
    assert [p["message_id"] for p in env.previews()] == [mid]
    assert env.text(mid).splitlines()[1].startswith("1. 中:")
    assert "Translating…" not in env.text(mid)
    assert mid in env.user_previews()
    bold = [e for e in env.fake.messages[(ALLOWED_USER, mid)]["entities"] if e["type"] == "bold"]
    assert len(bold) > 1, "headword and translations are bold"


async def test_translation_crash_still_offers_english_preview(env):
    class CrashingTranslator:
        translates_glosses = True

        async def translate(self, texts, source_lang, context=None):
            raise RuntimeError("boom")

    await env.restart(translator=CrashingTranslator())
    await env.send("猫が魚を食べた。")
    previews = env.previews()
    assert len(previews) == 3 and len(env.fake.sent()) == 3
    for p in previews:
        lines = p["text"].splitlines()
        assert lines[-1] == "Translation failed" and "Translating…" not in lines
    await env.press(previews[0]["message_id"], "save")
    assert await env.words() == ["猫"]
