import asyncio
import heapq
import inspect
import itertools
from datetime import datetime, timedelta

import pytest
from anki.errors import NetworkError
from anki.sync import SyncAuth
from anki_helpers import card

from ankibot.anki_service import AddResult, AddStatus, DeleteStatus
from ankibot.sync import DbSyncResult, FullSyncFlag, SyncAuthError, SyncManager, SyncStatus

DEBOUNCE = 120
STALE_HOURS = 6
PRE_TIMEOUT = 15
BASE_TIME = datetime(2026, 10, 26, 9, 0, 0).astimezone()


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self._sleepers = []
        self._seq = itertools.count()

    def now(self):
        return BASE_TIME + timedelta(seconds=self.t)

    async def sleep(self, seconds):
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self.t + seconds, next(self._seq), future))
        await future

    async def advance(self, seconds):
        target = self.t + seconds
        await settle()
        while self._sleepers and self._sleepers[0][0] <= target:
            deadline, _, future = heapq.heappop(self._sleepers)
            self.t = max(self.t, deadline)
            if not future.done():
                future.set_result(None)
            await settle()
        self.t = target
        await settle()


async def settle():
    for _ in range(50):
        await asyncio.sleep(0)


class InlineWorker:
    """Runs calls on the event loop; fake backend methods may be coroutines to simulate long pylib work."""

    async def run(self, fn, *args):
        result = fn(*args)
        if inspect.isawaitable(result):
            result = await result
        return result


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.logins = 0
        self.results = []  # queued DbSyncResult or exceptions for sync_collection
        self.gate = None  # asyncio.Event that sync_collection waits on, when set
        self.media_polls_active = 0
        self.local_changes = False
        self.rebuilds = 0
        self.auths = []

    def login(self):
        self.logins += 1
        return SyncAuth(hkey=f"hkey{self.logins}")

    async def sync_collection(self, auth):
        self.calls.append("db")
        self.auths.append(auth)
        if self.gate is not None:
            await self.gate.wait()
        if self.results:
            item = self.results.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return DbSyncResult("NO_CHANGES", None)

    def start_media_sync(self, auth):
        self.calls.append("media")

    def media_sync_active(self):
        if self.media_polls_active:
            self.media_polls_active -= 1
            return True
        return False

    def has_local_changes(self):
        return self.local_changes

    def rebuild_known_words(self):
        self.rebuilds += 1

    def syncs(self):
        return self.calls.count("db")


class FakeService:
    def __init__(self, flag):
        self.flag = flag
        self.added = []
        self.deleted = []

    def add_note_if_absent(self, data):
        if self.flag.is_set():
            return AddResult(AddStatus.BLOCKED)
        self.added.append(data.word)
        return AddResult(AddStatus.ADDED, len(self.added))

    def delete_note(self, note_id):
        if self.flag.is_set():
            return DeleteStatus.BLOCKED
        self.deleted.append(note_id)
        return DeleteStatus.DELETED


@pytest.fixture
def env(tmp_path):
    clock = FakeClock()
    backend = FakeBackend()
    flag = FullSyncFlag(tmp_path / "full_sync_required")
    notes = []

    async def notify(message):
        notes.append(message)

    def make(flag=flag, **kwargs):
        options = dict(
            debounce_seconds=DEBOUNCE,
            stale_hours=STALE_HOURS,
            pre_sync_timeout_seconds=PRE_TIMEOUT,
            notify=notify,
            now=clock.now,
            sleep=clock.sleep,
            retry_delays=(5, 15, 30),
            media_poll_seconds=1,
        )
        options.update(kwargs)
        return SyncManager(InlineWorker(), FakeService(flag), backend, flag, **options)

    class Env:
        pass

    e = Env()
    e.clock, e.backend, e.flag, e.notes, e.make, e.tmp_path = clock, backend, flag, notes, make, tmp_path
    return e


# --- debounce -------------------------------------------------------------------


async def test_first_add_syncs_after_debounce(env):
    manager = env.make()
    await manager.add_note(card("食べる", "たべる"))
    assert manager.has_pending_changes
    await env.clock.advance(DEBOUNCE - 1)
    assert env.backend.syncs() == 0
    await env.clock.advance(1)
    assert env.backend.calls == ["db", "media"]
    assert not manager.has_pending_changes
    assert manager.last_sync_at == env.clock.now()


async def test_add_before_expiry_resets_timer_and_syncs_once(env):
    manager = env.make()
    await manager.add_note(card("食べる", "たべる"))
    await env.clock.advance(DEBOUNCE - 10)
    await manager.add_note(card("見る", "みる"))
    await env.clock.advance(10)
    assert env.backend.syncs() == 0, "original timer must not fire"
    await env.clock.advance(DEBOUNCE - 11)
    assert env.backend.syncs() == 0
    await env.clock.advance(1)
    assert env.backend.syncs() == 1
    await env.clock.advance(DEBOUNCE * 3)
    assert env.backend.syncs() == 1


async def test_undo_resets_debounce_timer(env):
    manager = env.make()
    await manager.add_note(card("食べる", "たべる"))
    await env.clock.advance(DEBOUNCE - 10)
    assert await manager.delete_note(1) is DeleteStatus.DELETED
    await env.clock.advance(10)
    assert env.backend.syncs() == 0
    await env.clock.advance(DEBOUNCE - 10)
    assert env.backend.syncs() == 1


async def test_manual_sync_cancels_debounce(env):
    manager = env.make()
    await manager.add_note(card("食べる", "たべる"))
    result = await manager.sync_now()
    assert result.status is SyncStatus.OK
    assert not manager.has_pending_changes
    await env.clock.advance(DEBOUNCE * 2)
    assert env.backend.syncs() == 1


async def test_media_sync_is_polled_until_done(env):
    manager = env.make()
    env.backend.media_polls_active = 3
    task = asyncio.create_task(manager.sync_now())
    await env.clock.advance(2)
    assert not task.done()
    await env.clock.advance(1)
    assert (await task).status is SyncStatus.OK
    assert env.clock.t == 3


# --- writes during sync -----------------------------------------------------------


async def test_save_during_sync_waits_and_is_not_lost(env):
    manager = env.make()
    env.backend.gate = asyncio.Event()
    sync_task = asyncio.create_task(manager.sync_now())
    await settle()
    assert manager.is_syncing

    save = asyncio.create_task(manager.add_note(card("食べる", "たべる")))
    await settle()
    assert not save.done(), "write must wait for the sync"

    env.backend.gate.set()
    await settle()
    assert (await sync_task).status is SyncStatus.OK
    assert (await save).status is AddStatus.ADDED
    assert manager.has_pending_changes, "pending state from the queued write must survive"

    env.backend.gate = None
    await env.clock.advance(DEBOUNCE)
    assert env.backend.syncs() == 2
    assert not manager.has_pending_changes


# --- failures and retries ---------------------------------------------------------


async def test_failed_sync_retries_then_notifies_and_keeps_pending(env):
    manager = env.make()
    env.backend.results = [NetworkError("down", None, None, None)] * 4
    await manager.add_note(card("食べる", "たべる"))
    task = asyncio.create_task(manager.sync_now())
    await env.clock.advance(5 + 15 + 30)
    result = await task
    assert result.status is SyncStatus.FAILED
    assert env.backend.syncs() == 4
    assert manager.has_pending_changes
    assert manager.last_sync_at is None
    assert len(env.notes) == 1 and "Sync failed" in env.notes[0]


async def test_retry_succeeds_after_transient_failure(env):
    manager = env.make()
    env.backend.results = [NetworkError("blip", None, None, None)]
    task = asyncio.create_task(manager.sync_now())
    await env.clock.advance(5)
    assert (await task).status is SyncStatus.OK
    assert env.notes == []


async def test_auth_error_triggers_relogin(env):
    manager = env.make()
    env.backend.results = [SyncAuthError("expired")]
    task = asyncio.create_task(manager.sync_now())
    await env.clock.advance(5)
    assert (await task).status is SyncStatus.OK
    assert env.backend.logins == 2
    assert env.backend.auths[-1].hkey == "hkey2"


async def test_new_endpoint_updates_cached_auth(env):
    manager = env.make()
    env.backend.results = [DbSyncResult("NO_CHANGES", "https://sync29.example/")]
    await manager.sync_now()
    await manager.sync_now()
    assert env.backend.logins == 1
    assert env.backend.auths[-1].endpoint == "https://sync29.example/"
    assert env.backend.auths[-1].hkey == "hkey1"


async def test_successful_sync_rebuilds_known_words(env):
    manager = env.make()
    await manager.sync_now()
    assert env.backend.rebuilds == 1


# --- pre-sync ----------------------------------------------------------------------


async def test_pre_sync_is_db_only_and_keeps_pending(env):
    manager = env.make()
    await manager.add_note(card("食べる", "たべる"))
    await manager.ensure_fresh()
    assert env.backend.calls == ["db"]
    assert manager.last_sync_at == env.clock.now()
    assert manager.has_pending_changes
    assert env.backend.rebuilds == 1


async def test_pre_sync_skipped_when_fresh(env):
    manager = env.make()
    await manager.sync_now()
    await env.clock.advance(STALE_HOURS * 3600 - 1)
    await manager.ensure_fresh()
    assert env.backend.syncs() == 1
    await env.clock.advance(1)
    await manager.ensure_fresh()
    assert env.backend.syncs() == 2


async def test_pre_sync_failure_does_not_touch_state(env):
    manager = env.make(retries=0)
    env.backend.results = [NetworkError("down", None, None, None)]
    await manager.ensure_fresh()
    assert manager.last_sync_at is None
    assert not manager.has_pending_changes


async def test_pre_sync_timeout_then_background_success(env):
    manager = env.make()
    env.backend.gate = asyncio.Event()
    waiter = asyncio.create_task(manager.ensure_fresh())
    await env.clock.advance(PRE_TIMEOUT)
    assert waiter.done(), "caller stops waiting at the timeout"
    assert manager.last_sync_at is None
    assert manager.is_syncing, "the sync keeps running and still holds the lock"

    save = asyncio.create_task(manager.add_note(card("食べる", "たべる")))
    await settle()
    assert not save.done()

    finished_at = env.clock.now()
    env.backend.gate.set()
    await settle()
    assert manager.last_sync_at == finished_at
    assert env.backend.rebuilds == 1
    assert (await save).status is AddStatus.ADDED


async def test_concurrent_messages_share_one_pre_sync(env):
    manager = env.make()
    env.backend.gate = asyncio.Event()
    waiters = [asyncio.create_task(manager.ensure_fresh()) for _ in range(3)]
    await settle()
    env.backend.gate.set()
    await asyncio.gather(*waiters)
    assert env.backend.syncs() == 1


# --- full sync required ---------------------------------------------------------------


@pytest.mark.parametrize("required", ["FULL_SYNC", "FULL_DOWNLOAD", "FULL_UPLOAD"])
async def test_full_sync_required_is_not_handled_automatically(env, required):
    manager = env.make()
    env.backend.results = [DbSyncResult(required, None)]
    await manager.add_note(card("食べる", "たべる"))
    result = await manager.sync_now()

    assert result.status is SyncStatus.FULL_SYNC_REQUIRED
    assert env.backend.calls == ["db"], "no media sync and no upload/download"
    assert manager.full_sync_required
    assert (env.tmp_path / "full_sync_required").exists()
    assert manager.has_pending_changes
    assert len(env.notes) == 1 and "bootstrap --force" in env.notes[0]

    assert (await manager.add_note(card("見る", "みる"))).status is AddStatus.BLOCKED
    manager.schedule()
    await env.clock.advance(DEBOUNCE * 2)
    await manager.ensure_fresh()
    assert env.backend.syncs() == 1, "no debounce or pre-sync while blocked"

    env.backend.results = [DbSyncResult(required, None)]
    assert (await manager.sync_now()).status is SyncStatus.FULL_SYNC_REQUIRED
    assert len(env.notes) == 1, "notify only on the first detection"


async def test_full_sync_flag_survives_restart(env):
    (env.tmp_path / "full_sync_required").touch()
    flag = FullSyncFlag(env.tmp_path / "full_sync_required")
    assert flag.is_set()
    manager = env.make(flag=flag)
    assert manager.full_sync_required
    await manager.start()
    await settle()
    assert env.backend.syncs() == 0, "startup sync skipped"
    assert await manager.shutdown(1)
    assert env.backend.syncs() == 0


# --- startup and shutdown ----------------------------------------------------------


async def test_startup_sync_runs_in_background_with_media(env):
    manager = env.make()
    env.backend.local_changes = True
    await manager.start()
    assert manager.has_pending_changes
    await settle()
    assert env.backend.calls == ["db", "media"]
    assert not manager.has_pending_changes


async def test_shutdown_runs_final_sync_when_pending(env):
    manager = env.make()
    await manager.add_note(card("食べる", "たべる"))
    assert await manager.shutdown(45)
    assert env.backend.calls == ["db", "media"]
    await env.clock.advance(DEBOUNCE * 2)
    assert env.backend.syncs() == 1, "debounce cancelled"


async def test_shutdown_without_pending_changes_does_not_sync(env):
    manager = env.make()
    assert await manager.shutdown(45)
    assert env.backend.syncs() == 0


async def test_shutdown_timeout_returns_in_time(env):
    manager = env.make()
    env.backend.gate = asyncio.Event()
    await manager.add_note(card("食べる", "たべる"))
    result = asyncio.create_task(manager.shutdown(45))
    await env.clock.advance(45)
    assert result.done()
    assert await result is False
    assert manager.has_pending_changes
    env.backend.gate.set()
    await settle()


async def test_shutdown_does_not_wait_for_a_running_sync_with_nothing_to_upload(env):
    manager = env.make()
    env.backend.gate = asyncio.Event()
    await manager.start()
    await settle()
    assert manager.is_syncing
    assert await manager.shutdown(45) is False
    assert env.backend.syncs() == 1, "no extra shutdown sync"
    env.backend.gate.set()
    await settle()


async def test_shutdown_waits_for_a_running_sync_that_uploads_changes(env):
    manager = env.make()
    env.backend.gate = asyncio.Event()
    await manager.add_note(card("食べる", "たべる"))
    running = asyncio.create_task(manager.sync_now())
    await settle()
    shutdown = asyncio.create_task(manager.shutdown(45))
    await settle()
    assert not shutdown.done()
    env.backend.gate.set()
    assert await shutdown
    await running
    assert not manager.has_pending_changes


async def test_shutdown_sync_covers_writes_queued_behind_a_running_sync(env):
    manager = env.make()
    env.backend.gate = asyncio.Event()
    running = asyncio.create_task(manager.sync_now())
    await settle()
    save = asyncio.create_task(manager.add_note(card("食べる", "たべる")))
    await settle()
    assert not manager.has_pending_changes, "the save is still waiting for the lock"
    shutdown = asyncio.create_task(manager.shutdown(45))
    await settle()
    env.backend.gate.set()
    assert await shutdown
    assert (await save).status is AddStatus.ADDED
    await running
    assert env.backend.syncs() == 2, "a final sync runs after the queued write"
    assert not manager.has_pending_changes


async def test_no_debounce_timer_is_started_after_shutdown(env):
    manager = env.make()
    assert await manager.shutdown(45)
    manager.schedule()
    assert manager.has_pending_changes
    await env.clock.advance(DEBOUNCE * 2)
    assert env.backend.syncs() == 0
