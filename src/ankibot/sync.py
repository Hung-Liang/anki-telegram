from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from anki.errors import BackendError, SyncError, SyncErrorKind
from anki.sync import SyncAuth

from ankibot.anki_service import AddResult, AddStatus, AnkiService, DeleteStatus
from ankibot.config import AnkiSettings
from ankibot.i18n import Texts, load_texts
from ankibot.models import CardData

log = logging.getLogger(__name__)

FULL_SYNC_STATES = frozenset({"FULL_SYNC", "FULL_DOWNLOAD", "FULL_UPLOAD"})

Sleep = Callable[[float], Awaitable[None]]
Notify = Callable[[str], Awaitable[None]]


def _local_now() -> datetime:
    return datetime.now().astimezone()


async def _no_notify(_message: str) -> None:
    return None


class FullSyncFlag:
    """`full_sync_required`, persisted as a marker file so it survives restarts.

    Only `bootstrap --force` removes the marker.
    """

    def __init__(self, marker: Path) -> None:
        self._marker = marker
        self._set = marker.exists()

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._marker.touch()
        self._set = True


class SyncAuthError(Exception):
    pass


# Errors a sync attempt may raise that are worth retrying.
RETRYABLE_ERRORS = (BackendError, SyncAuthError, OSError)


@dataclass(frozen=True)
class DbSyncResult:
    required: str
    new_endpoint: str | None


class SyncBackend(Protocol):
    """Blocking pylib calls; SyncManager runs every method on the Anki thread."""

    def login(self) -> SyncAuth: ...

    def sync_collection(self, auth: SyncAuth) -> DbSyncResult: ...

    def start_media_sync(self, auth: SyncAuth) -> None: ...

    def media_sync_active(self) -> bool: ...

    def has_local_changes(self) -> bool: ...

    def rebuild_known_words(self) -> None: ...


class Worker(Protocol):
    async def run(self, fn: Callable[..., Any], *args: Any) -> Any: ...


@contextmanager
def _auth_errors() -> Iterator[None]:
    try:
        yield
    except SyncError as exc:
        if exc.kind == SyncErrorKind.AUTH:
            raise SyncAuthError(str(exc)) from exc
        raise


class AnkiSyncBackend:
    """SyncBackend on top of the pinned pylib (call signatures verified by scripts/spike_sync.py)."""

    def __init__(self, service: AnkiService, settings: AnkiSettings) -> None:
        self._service = service
        self._settings = settings

    def login(self) -> SyncAuth:
        with _auth_errors():
            return self._service.collection.sync_login(
                username=self._settings.anki_user,
                password=self._settings.anki_password.get_secret_value(),
                endpoint=self._settings.anki_sync_endpoint,
            )

    def sync_collection(self, auth: SyncAuth) -> DbSyncResult:
        with _auth_errors():
            # sync_media=True would start media sync in the background without a way to wait for it here.
            out = self._service.collection.sync_collection(auth, sync_media=False)
        return DbSyncResult(type(out).ChangesRequired.Name(out.required), out.new_endpoint or None)

    def start_media_sync(self, auth: SyncAuth) -> None:
        with _auth_errors():
            self._service.collection.sync_media(auth)

    def media_sync_active(self) -> bool:
        # Raises when the background media sync failed.
        with _auth_errors():
            return self._service.collection.media_sync_status().active

    def has_local_changes(self) -> bool:
        db = self._service.collection.db
        return bool(
            db.scalar("select exists(select 1 from notes where usn = -1)")
            or db.scalar("select exists(select 1 from graves where usn = -1)")
        )

    def rebuild_known_words(self) -> None:
        self._service.rebuild_known_words()


class SyncKind(StrEnum):
    STARTUP = "startup"
    DEBOUNCE = "debounce"
    MANUAL = "manual"
    PRE = "pre"
    SHUTDOWN = "shutdown"


class SyncStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    FULL_SYNC_REQUIRED = "full_sync_required"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class SyncResult:
    status: SyncStatus
    kind: SyncKind
    finished_at: datetime
    error: str | None = None


class SyncManager:
    """Single owner of every sync, of the write/sync lock, and of the sync state.

    Writes and syncs share one asyncio.Lock; a write that arrives during a sync waits
    for it and is applied afterwards. Sync tasks are never cancelled, so the lock is
    only released once the Anki thread has really finished the sync.
    """

    def __init__(
        self,
        worker: Worker,
        service: AnkiService,
        backend: SyncBackend,
        flag: FullSyncFlag,
        *,
        debounce_seconds: float,
        stale_hours: float,
        pre_sync_timeout_seconds: float,
        notify: Notify = _no_notify,
        texts: Texts | None = None,
        now: Callable[[], datetime] = _local_now,
        sleep: Sleep = asyncio.sleep,
        retries: int = 3,
        retry_delays: tuple[float, ...] = (5, 15, 30),
        media_poll_seconds: float = 1.0,
    ) -> None:
        self._worker = worker
        self._service = service
        self._backend = backend
        self._flag = flag
        self._debounce = debounce_seconds
        self._stale = timedelta(hours=stale_hours)
        self._pre_timeout = pre_sync_timeout_seconds
        self._notify = notify
        self._t = texts or load_texts()
        self._now = now
        self._sleep = sleep
        self._retries = retries
        self._retry_delays = retry_delays
        self._media_poll = media_poll_seconds

        self._lock = asyncio.Lock()
        self._syncing = False
        self._auth: SyncAuth | None = None
        self._change_gen = 0
        # Writes that are waiting for (or holding) the lock; the shutdown sync must come after them.
        self._writers = 0
        self._closing = False
        self._timer: asyncio.Task[None] | None = None
        self._pre_task: asyncio.Task[SyncResult] | None = None
        self._tasks: set[asyncio.Task[SyncResult]] = set()

        self.last_sync_at: datetime | None = None
        self.has_pending_changes = False

    @property
    def full_sync_required(self) -> bool:
        return self._flag.is_set()

    @property
    def is_syncing(self) -> bool:
        return self._syncing

    # --- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """Load the pending state and start the startup sync in the background."""
        if await self._worker.run(self._backend.has_local_changes):
            self.has_pending_changes = True
        if self.full_sync_required:
            log.warning("full sync required (marker present); skipping startup sync")
            return
        self._spawn(SyncKind.STARTUP)

    async def shutdown(self, timeout: float) -> bool:
        """Run the final sync if needed; False when Anki-thread work is still running
        (timed out, or a sync with nothing to upload that is not waited for).

        On False the caller must not close() the collection (it would wait behind the sync).
        """
        self._closing = True
        self._cancel_timer()
        if (self.has_pending_changes or self._writers) and not self.full_sync_required:
            # The lock is FIFO, so this sync runs after every write already queued.
            self._spawn(SyncKind.SHUTDOWN)
        pending = {t for t in self._tasks if not t.done()}
        if not pending:
            return True
        if not self.has_pending_changes and not self._writers:
            # Nothing to upload: do not wait out a running sync (e.g. a long media download);
            # it runs again on next start.
            log.info("nothing to upload; not waiting for the running sync")
            return False
        finished = await self._wait_all(pending, timeout)
        if not finished:
            log.warning(
                "shutdown sync did not finish in %ss (has_pending_changes=%s)",
                timeout,
                self.has_pending_changes,
            )
        return finished

    # --- writes ----------------------------------------------------------------

    async def add_note(self, card: CardData) -> AddResult:
        self._writers += 1
        try:
            async with self._lock:
                result = await self._worker.run(self._service.add_note_if_absent, card)
        finally:
            self._writers -= 1
        if result.status is AddStatus.ADDED:
            self.schedule()
        return result

    async def delete_note(self, note_id: int) -> DeleteStatus:
        self._writers += 1
        try:
            async with self._lock:
                status = await self._worker.run(self._service.delete_note, note_id)
        finally:
            self._writers -= 1
        if status is DeleteStatus.DELETED:
            self.schedule()
        return status

    def schedule(self) -> None:
        """Mark pending changes and (re)start the debounce timer."""
        self.has_pending_changes = True
        self._change_gen += 1
        if self.full_sync_required or self._closing:
            return
        self._cancel_timer()
        self._timer = asyncio.create_task(self._debounce_timer())

    async def _debounce_timer(self) -> None:
        await self._sleep(self._debounce)
        # No await between clearing the timer and spawning, so a cancel can only hit the sleep.
        self._timer = None
        self._spawn(SyncKind.DEBOUNCE)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    # --- sync entry points -----------------------------------------------------

    async def sync_now(self) -> SyncResult:
        """`/sync`: run immediately, replacing any scheduled debounce sync."""
        self._cancel_timer()
        return await asyncio.shield(self._spawn(SyncKind.MANUAL))

    async def ensure_fresh(self) -> None:
        """Before handling a message: database-only sync when stale, waiting at most the pre-sync timeout."""
        if self.full_sync_required or not self._is_stale():
            return
        if self._pre_task is None or self._pre_task.done():
            self._pre_task = self._spawn(SyncKind.PRE)
        # Only stop waiting on timeout; the task keeps running and finishes its own bookkeeping.
        await self._wait_all({self._pre_task}, self._pre_timeout)

    # --- internals -------------------------------------------------------------

    def _is_stale(self) -> bool:
        return self.last_sync_at is None or self._now() - self.last_sync_at >= self._stale

    def _spawn(self, kind: SyncKind) -> asyncio.Task[SyncResult]:
        task = asyncio.create_task(self._sync(kind), name=f"sync-{kind}")
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[SyncResult]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("sync task %s crashed", task.get_name(), exc_info=task.exception())

    async def _wait_all(self, tasks: set[asyncio.Task[Any]], timeout: float) -> bool:
        timer = asyncio.ensure_future(self._sleep(timeout))
        remaining = set(tasks)
        try:
            while remaining:
                done, _ = await asyncio.wait(remaining | {timer}, return_when=asyncio.FIRST_COMPLETED)
                remaining -= done
                if timer in done:
                    return not remaining
            return True
        finally:
            timer.cancel()

    def _should_skip(self, kind: SyncKind) -> bool:
        if self.full_sync_required and kind is not SyncKind.MANUAL:
            return True
        return kind is SyncKind.PRE and not self._is_stale()

    async def _sync(self, kind: SyncKind) -> SyncResult:
        media = kind is not SyncKind.PRE
        last_error: BaseException | None = None
        for attempt in range(self._retries + 1):
            if attempt:
                # Back off without holding the lock so queued writes can go through.
                await self._sleep(self._retry_delays[min(attempt, len(self._retry_delays)) - 1])
            async with self._lock:
                if self._should_skip(kind):
                    return SyncResult(SyncStatus.SKIPPED, kind, self._now())
                gen = self._change_gen
                self._syncing = True
                try:
                    required = await self._attempt(media)
                except RETRYABLE_ERRORS as exc:
                    last_error = exc
                    log.warning("%s sync attempt %d failed: %s", kind, attempt + 1, exc)
                    continue
                finally:
                    self._syncing = False

                if required in FULL_SYNC_STATES:
                    first = not self.full_sync_required
                    self._flag.set()
                    self._cancel_timer()
                    log.error("server requires a full sync (%s); local writes are paused", required)
                    result = SyncResult(SyncStatus.FULL_SYNC_REQUIRED, kind, self._now(), required)
                else:
                    self.last_sync_at = self._now()
                    # Only a full (database + media) sync uploads everything written before it started.
                    if media and gen == self._change_gen:
                        self.has_pending_changes = False
                    await self._worker.run(self._backend.rebuild_known_words)
                    log.info("%s sync finished (%s)", kind, required)
                    return SyncResult(SyncStatus.OK, kind, self.last_sync_at)
            if first:
                await self._send(
                    self._t.notify_full_sync_required.format(
                        reason=required, recovery=self._t.sync_recovery_hint
                    )
                )
            return result

        log.error("%s sync failed after %d attempts: %s", kind, self._retries + 1, last_error)
        await self._send(self._t.notify_sync_failed.format(retries=self._retries, error=last_error))
        return SyncResult(SyncStatus.FAILED, kind, self._now(), str(last_error))

    async def _attempt(self, media: bool) -> str:
        try:
            if self._auth is None:
                self._auth = await self._worker.run(self._backend.login)
            result = await self._worker.run(self._backend.sync_collection, self._auth)
            if result.new_endpoint:
                self._auth = SyncAuth(hkey=self._auth.hkey, endpoint=result.new_endpoint)
            if result.required in FULL_SYNC_STATES or not media:
                return result.required
            await self._worker.run(self._backend.start_media_sync, self._auth)
            # Media sync runs in the background; poll without holding the Anki thread.
            while await self._worker.run(self._backend.media_sync_active):
                await self._sleep(self._media_poll)
            return result.required
        except SyncAuthError:
            self._auth = None
            raise

    async def _send(self, message: str) -> None:
        try:
            await self._notify(message)
        except Exception:
            log.exception("failed to send sync notification: %s", message)
