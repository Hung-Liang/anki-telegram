"""Real syncs against the local-sync `anki-sync` server (`make local-sync-up`, then `make test-integration`).

Each test full-uploads a fresh collection A (overwriting the server copy, which is only
allowed on local-sync) and bootstraps B from it, so tests do not depend on each other.
"""

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from anki.collection import Collection
from anki_helpers import NOTETYPE, card, make_collection, settings
from pydantic import SecretStr

from ankibot.anki_service import AddStatus, AnkiService
from ankibot.bootstrap import full_download
from ankibot.sync import AnkiSyncBackend, FullSyncFlag, SyncManager, SyncStatus
from ankibot.threads import DedicatedThread

pytestmark = pytest.mark.integration

ENDPOINT = os.environ.get("LOCAL_SYNC_ENDPOINT", "http://anki-sync:8080/")
USER, _, PASSWORD = os.environ.get("SYNC_USER1", "user:pass").partition(":")


def local_settings(data_dir: Path):
    return settings(data_dir).model_copy(
        update={"anki_user": USER, "anki_password": SecretStr(PASSWORD), "anki_sync_endpoint": ENDPOINT}
    )


def force_upload(col: Collection, s) -> None:
    auth = col.sync_login(username=USER, password=PASSWORD, endpoint=ENDPOINT)
    out = col.sync_collection(auth, sync_media=False)
    col.close_for_full_sync()
    col.full_upload_or_download(auth=auth, server_usn=out.server_media_usn, upload=True)
    col.reopen(after_full_sync=True)


def bootstrap_into(path: Path, s) -> None:
    col = Collection(str(path))
    try:
        full_download(col, s)
    finally:
        col.close()


class Device:
    def __init__(self, name: str, data_dir: Path):
        self.name = name
        self.settings = local_settings(data_dir)
        self.thread = DedicatedThread(f"anki-{name}")
        self.service: AnkiService | None = None
        self.manager: SyncManager | None = None

    async def open(self) -> None:
        s = self.settings
        self.service = await self.thread.run(AnkiService.open, s)
        flag = FullSyncFlag(s.full_sync_marker_path)
        self.manager = SyncManager(
            self.thread,
            self.service,
            AnkiSyncBackend(self.service, s),
            flag,
            debounce_seconds=3600,
            stale_hours=6,
            pre_sync_timeout_seconds=15,
            retries=0,
            media_poll_seconds=0.2,
        )

    async def sync(self) -> None:
        result = await self.manager.sync_now()
        assert result.status is SyncStatus.OK, result

    async def words(self) -> set[str]:
        def read():
            col = self.service.collection
            return {col.get_note(nid)["Word"] for nid in col.find_notes(f'"note:{NOTETYPE}"')}

        return await self.thread.run(read)

    async def media_path(self, filename: str) -> Path:
        return Path(await self.thread.run(self.service.collection.media.dir)) / filename

    async def close(self) -> None:
        if self.service is not None:
            await self.thread.run(self.service.close)
        self.thread.shutdown()


@pytest.fixture
async def devices(tmp_path):
    a = Device("a", tmp_path / "a")
    b = Device("b", tmp_path / "b")
    make_collection(tmp_path / "a")
    try:
        await a.thread.run(lambda: _upload_fresh(a.settings))
    except Exception as exc:
        pytest.fail(f"local-sync server at {ENDPOINT} is not usable (run `make local-sync-up`): {exc}")
    (tmp_path / "b").mkdir()
    await b.thread.run(bootstrap_into, b.settings.collection_path, b.settings)
    await a.open()
    await b.open()
    try:
        yield a, b
    finally:
        await a.close()
        await b.close()


def _upload_fresh(s) -> None:
    col = Collection(str(s.collection_path))
    try:
        force_upload(col, s)
    finally:
        col.close()


async def test_note_and_audio_reach_the_other_collection(devices, tmp_path):
    a, b = devices
    # Server media persists across runs; a reused name with new content would be a media conflict.
    audio = tmp_path / f"integration-{uuid.uuid4().hex}.mp3"
    payload = b"ID3" + os.urandom(64)
    audio.write_bytes(payload)

    result = await a.manager.add_note(card("食べる", "たべる", audio=str(audio)))
    assert result.status is AddStatus.ADDED
    assert a.manager.has_pending_changes
    await a.sync()
    assert not a.manager.has_pending_changes

    await b.sync()
    assert "食べる" in await b.words()
    field = await b.thread.run(lambda: b.service.collection.get_note(result.note_id)["Audio"])
    filename = field.removeprefix("[sound:").removesuffix("]")
    received = await b.media_path(filename)
    assert received.read_bytes() == payload
    assert b.service.known_words().contains("食べる", "たべる")


async def test_edit_on_other_device_is_pulled(devices):
    a, b = devices
    nid = (await a.manager.add_note(card("見る", "みる"))).note_id
    await a.sync()
    await b.sync()

    def edit():
        col = b.service.collection
        note = col.get_note(nid)
        note["Meaning"] = "edited-on-b"
        col.update_note(note)

    await b.thread.run(edit)
    await b.sync()
    await a.sync()
    meaning = await a.thread.run(lambda: a.service.collection.get_note(nid)["Meaning"])
    assert meaning == "edited-on-b"


async def test_both_devices_add_different_notes(devices):
    a, b = devices
    assert (await a.manager.add_note(card("犬", "いぬ"))).status is AddStatus.ADDED
    assert (await b.manager.add_note(card("猫", "ねこ"))).status is AddStatus.ADDED
    await a.sync()
    await b.sync()
    await a.sync()
    assert {"犬", "猫"} <= await a.words()
    assert {"犬", "猫"} <= await b.words()


async def test_save_during_sync_is_queued_and_synced_later(devices):
    a, b = devices
    sync_task = asyncio.create_task(a.manager.sync_now())
    await asyncio.sleep(0)
    save_task = asyncio.create_task(a.manager.add_note(card("走る", "はしる")))

    save = await save_task
    assert sync_task.done(), "the save must wait for the running sync"
    assert (await sync_task).status is SyncStatus.OK
    assert save.status is AddStatus.ADDED
    assert a.manager.has_pending_changes

    await a.sync()
    await b.sync()
    assert "走る" in await b.words()


async def test_delete_is_synced(devices):
    a, b = devices
    nid = (await a.manager.add_note(card("消す", "けす"))).note_id
    await a.sync()
    await b.sync()
    assert "消す" in await b.words()

    await a.manager.delete_note(nid)
    await a.sync()
    await b.sync()
    assert "消す" not in await b.words()
    assert not b.service.known_words().contains("消す", "けす")
