import asyncio
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from anki.collection import Collection
from anki_helpers import NOTETYPE, card, make_collection
from telegram.ext import PicklePersistence
from telegram_fake import ALLOWED_USER, FakeTelegram
from test_bot import StubBackend
from test_runtime import runtime_settings
from test_tts import FakeCommunicate

from ankibot.i18n import load_texts
from ankibot.runtime import Runtime
from ankibot.state import PreviewStatus
from ankibot.tts import EdgeTTS

T = load_texts()
CHILD = Path(__file__).parent / "shutdown_child.py"


async def wait_for(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


def make_runtime(tmp_path, fake, backend, script=(), tts_timeout=30, **overrides):
    settings = runtime_settings(tmp_path, heartbeat_file=tmp_path / "heartbeat", **overrides)
    tts = EdgeTTS(
        tmp_path / "tts", "ja-JP-NanamiNeural", tts_timeout, communicate=FakeCommunicate(list(script))
    )
    return Runtime(settings, request=fake, backend=lambda service, s: backend, tts=tts)


def bot_words(tmp_path):
    col = Collection(str(tmp_path / "collection.anki2"))
    try:
        return [col.get_note(n)["Word"] for n in col.find_notes(f'"note:{NOTETYPE}"')]
    finally:
        col.close()


async def test_shutdown_cancels_saves_in_tts_and_recovers_them_on_restart(tmp_path):
    make_collection(tmp_path)
    fake = FakeTelegram()
    backend = StubBackend()
    runtime = make_runtime(tmp_path, fake, backend, script=["hang"])
    fake.pending_updates.append(fake.raw_text_update("食べた"))
    await runtime.start()
    await wait_for(lambda: fake.sent())
    mid = fake.sent()[0]["message_id"]
    fake.pending_updates.append(fake.raw_callback_update(mid, "save"))
    await wait_for(lambda: fake.messages[(ALLOWED_USER, mid)]["text"].endswith(T.status_saving))

    loop = asyncio.get_running_loop()
    start = loop.time()
    assert await runtime.stop() is True
    assert loop.time() - start < 5
    assert bot_words(tmp_path) == [], "the cancelled save never reached the Anki thread"
    data = await PicklePersistence(tmp_path / "bot_state.pickle").get_user_data()
    assert data[ALLOWED_USER]["previews"][mid].status is PreviewStatus.SAVING

    restarted = make_runtime(tmp_path, fake, backend)
    await restarted.start()
    assert fake.messages[(ALLOWED_USER, mid)]["text"].endswith(T.status_interrupted)
    assert fake.buttons(ALLOWED_USER, mid) == ["save", "discard"]
    fake.pending_updates.append(fake.raw_callback_update(mid, "save"))
    await wait_for(lambda: fake.buttons(ALLOWED_USER, mid)[:1] != ["save"])
    await restarted.stop()
    assert bot_words(tmp_path) == ["食べる"]


async def test_final_sync_runs_and_collection_closes(tmp_path):
    make_collection(tmp_path)
    fake = FakeTelegram()
    backend = StubBackend()
    runtime = make_runtime(tmp_path, fake, backend)
    await runtime.start()
    await wait_for(lambda: backend.syncs == 1)
    await runtime.sync.add_note(card("食べる", "たべる"))
    assert await runtime.stop() is True
    assert backend.syncs == 2
    assert not (tmp_path / "collection.anki2-wal").exists()


async def test_final_sync_timeout_skips_close_and_returns_in_time(tmp_path, monkeypatch):
    make_collection(tmp_path)
    fake = FakeTelegram()
    backend = StubBackend()
    runtime = make_runtime(tmp_path, fake, backend, shutdown_sync_timeout_seconds=0.3)
    await runtime.start()
    await wait_for(lambda: backend.syncs == 1)
    fake.pending_updates.append(fake.raw_text_update("食べた"))
    await wait_for(lambda: fake.sent())
    await runtime.sync.add_note(card("見る", "みる"))
    backend.gate = threading.Event()
    closed = []
    monkeypatch.setattr(runtime.service, "close", lambda: closed.append(True))

    loop = asyncio.get_running_loop()
    start = loop.time()
    assert await runtime.stop() is False
    assert loop.time() - start < 3
    assert closed == []
    data = await PicklePersistence(tmp_path / "bot_state.pickle").get_user_data()
    assert len(data[ALLOWED_USER]["previews"]) == 1, "persistence is written before exiting"
    # Let the Anki thread finish so the test process can clean up.
    backend.gate.set()
    runtime.anki.shutdown()
    runtime.lookup_thread.shutdown()


def run_child(tmp_path, mode):
    proc = subprocess.Popen(
        [sys.executable, str(CHILD), str(tmp_path), mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines = []
    for line in proc.stdout:
        lines.append(line)
        if line.strip() == "ready":
            break
    else:
        proc.wait(10)
        pytest.fail("child did not start:\n" + "".join(lines))
    start = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    try:
        out, _ = proc.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("child did not exit after SIGTERM")
    return proc.returncode, time.monotonic() - start, "".join(lines) + out


def test_sigterm_with_hanging_final_sync_exits_in_time_without_close(tmp_path):
    code, elapsed, log = run_child(tmp_path, "hang")
    assert code == 1, log
    assert elapsed < 10, log
    assert "did not finish" in log and "has_pending_changes=True" in log
    assert (tmp_path / "collection.anki2-wal").exists(), "collection was not closed"
    # The committed note survives the hard exit (WAL), and the collection reopens fine.
    assert bot_words(tmp_path) == ["食べる"]


def test_sigterm_exits_cleanly(tmp_path):
    code, elapsed, log = run_child(tmp_path, "ok")
    assert code == 0, log
    assert elapsed < 10, log
    assert not (tmp_path / "collection.anki2-wal").exists()
