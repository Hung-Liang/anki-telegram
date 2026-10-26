import asyncio

from anki_helpers import DECK, NOTETYPE, make_collection
from telegram_fake import ALLOWED_USER, FakeTelegram
from test_bot import StubBackend
from test_tts import FakeCommunicate

from ankibot.config import Settings
from ankibot.runtime import Runtime
from ankibot.tts import EdgeTTS


def runtime_settings(tmp_path, **overrides):
    values = dict(
        tg_token="123:ABC",
        tg_allowed_user_id=ALLOWED_USER,
        anki_user="user",
        anki_password="pass",
        anki_sync_endpoint="http://anki-sync:8080/",
        anki_deck=DECK,
        anki_notetype=NOTETYPE,
        dedup_extra_sources="",
        data_dir=tmp_path,
    )
    values.update(overrides)
    return Settings(**values)


def test_tts_is_off_by_default(tmp_path):
    assert Runtime(runtime_settings(tmp_path)).tts is None


def test_tts_provider_edge_uses_edge_tts(tmp_path):
    assert isinstance(Runtime(runtime_settings(tmp_path, tts_provider="edge")).tts, EdgeTTS)


async def test_runtime_starts_polls_and_stops_cleanly(tmp_path):
    make_collection(tmp_path)
    fake = FakeTelegram()
    backend = StubBackend()
    tts = EdgeTTS(tmp_path / "tts", "ja-JP-NanamiNeural", 1, communicate=FakeCommunicate([]))
    runtime = Runtime(runtime_settings(tmp_path), request=fake, backend=lambda service, s: backend, tts=tts)
    fake.pending_updates.append(fake.raw_text_update("食べた"))
    await runtime.start()
    for _ in range(300):
        if fake.count("sendMessage") and backend.syncs:
            break
        await asyncio.sleep(0.01)
    assert backend.syncs == 1, "startup sync runs in the background"
    assert fake.sent()[0]["text"].startswith("食べる（たべる）")
    await runtime.stop()
    assert not (tmp_path / "collection.anki2-wal").exists(), "collection is closed"
    assert (tmp_path / "bot_state.pickle").exists()
