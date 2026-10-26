"""Wires the services together and runs the bot until a stop signal arrives."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from telegram.ext import Application

from ankibot.anki_service import AnkiService
from ankibot.bot import BotHandlers, build_application, prepare_persistence
from ankibot.config import Settings
from ankibot.examples import Examples
from ankibot.healthcheck import Heartbeat
from ankibot.i18n import load_texts
from ankibot.pipeline import Lookup, Pipeline
from ankibot.sync import AnkiSyncBackend, FullSyncFlag, SyncBackend, SyncManager
from ankibot.threads import DedicatedThread
from ankibot.translator import CachedTranslator, TranslationCache, is_set, make_translator
from ankibot.tts import EdgeTTS, TTSProvider

log = logging.getLogger(__name__)


def _translation_enabled(settings: Settings) -> bool:
    return is_set(settings.deepl_api_key) or is_set(settings.gemini_api_key)


def _open_lookup(settings: Settings) -> tuple[Lookup, TranslationCache | None]:
    """Runs on the lookup thread, which then owns every object created here."""
    examples = Examples.open(settings.data_dir / "tatoeba.db")
    lookup = Lookup.create(settings.include_proper_nouns, examples)
    cache = (
        TranslationCache(settings.data_dir / "translations.db") if _translation_enabled(settings) else None
    )
    return lookup, cache


def _close_lookup(lookup: Lookup, cache: TranslationCache | None) -> None:
    lookup.close()
    if cache is not None:
        cache.close()


class Runtime:
    def __init__(
        self,
        settings: Settings,
        *,
        request: Any | None = None,
        backend: Callable[[AnkiService, Settings], SyncBackend] = AnkiSyncBackend,
        tts: TTSProvider | None = None,
    ) -> None:
        self.settings = settings
        self._request = request
        self._backend = backend
        if tts is None and settings.tts_provider == "edge":
            tts = EdgeTTS(settings.data_dir / "tts", settings.tts_voice, settings.tts_timeout_seconds)
        self.tts = tts
        self.translator: CachedTranslator | None = None
        self.anki = DedicatedThread("anki")
        self.lookup_thread = DedicatedThread("lookup")
        self.app: Application | None = None
        self._background: list[asyncio.Task[None]] = []
        self.started = asyncio.Event()

    async def start(self) -> None:
        s = self.settings
        texts = load_texts(s.bot_language, s.bot_messages_file)
        flag = FullSyncFlag(s.full_sync_marker_path)
        self.service = await self.anki.run(AnkiService.open, s, flag.is_set)
        self.lookup, self._cache = await self.lookup_thread.run(_open_lookup, s)
        self.translator = make_translator(
            deepl_key=s.deepl_api_key,
            deepl_timeout=s.deepl_timeout_seconds,
            gemini_key=s.gemini_api_key,
            gemini_model=s.gemini_model,
            gemini_timeout=s.gemini_timeout_seconds,
            target=s.translation_target,
            target_name=s.translation_target_name,
            cache=self._cache,
            thread=self.lookup_thread,
        )
        self.sync = SyncManager(
            self.anki,
            self.service,
            self._backend(self.service, s),
            flag,
            debounce_seconds=s.sync_debounce_seconds,
            stale_hours=s.sync_stale_hours,
            pre_sync_timeout_seconds=s.pre_sync_timeout_seconds,
            notify=self._notify,
            texts=texts,
        )
        pipeline = Pipeline(
            self.lookup_thread, self.lookup, self.service.known_words, self.sync.ensure_fresh, self.translator
        )
        self.handlers = BotHandlers(s, pipeline, self.sync, self._stats, self.tts, texts=texts)
        persistence = prepare_persistence(s.data_dir / "bot_state.pickle")
        self.app = build_application(s, self.handlers, request=self._request, persistence=persistence)

        await self.app.initialize()
        await self.handlers.recover(self.app)
        await self.app.start()
        # Start the startup sync before polling, so a stale-data pre-sync waits for it instead of racing it.
        await self.sync.start()
        await self.app.updater.start_polling()
        self._background.append(asyncio.create_task(self.handlers.cleanup_loop(self.app)))
        heartbeat = Heartbeat(s.heartbeat_file, self.app.bot.get_me)
        self._background.append(asyncio.create_task(heartbeat.run()))
        self.started.set()
        log.info("bot started")

    async def _stats(self):
        return await self.anki.run(self.service.stats)

    async def _notify(self, text: str) -> None:
        if self.app is not None:
            await self.handlers.notify(self.app.bot, text)

    async def stop(self) -> bool:
        """Graceful shutdown; False when a sync is still running (see SyncManager.shutdown).

        On False the collection is deliberately left open: pylib work on the Anki thread cannot
        be cancelled and close() would only queue behind it. The caller then exits with os._exit().
        """
        s = self.settings
        app = self.app
        # 1. Stop receiving updates.
        if app is not None and app.updater is not None and app.updater.running:
            await app.updater.stop()
        for task in self._background:
            task.cancel()
        # 2. Saves still in TTS are cancelled and stay SAVING (recovered on next start);
        #    writes already queued for the Anki thread are not cancelled.
        cancelled = self.handlers.cancel_tts()
        if cancelled:
            log.info("cancelled %d saves that were still generating audio", cancelled)
        # 3. Final sync (if needed); the timeout also covers the writes queued before it.
        finished = await self.sync.shutdown(s.shutdown_sync_timeout_seconds)
        if not finished:
            # 5. Sync still running: persist the preview state, but do not close() or wait for handlers.
            log.warning(
                "sync still running; exiting without closing the collection (has_pending_changes=%s)",
                self.sync.has_pending_changes,
            )
            if app is not None and app.persistence is not None:
                app.mark_data_for_update_persistence(user_ids=[s.tg_allowed_user_id])
                await app.update_persistence()
                await app.persistence.flush()
            return False
        # 4. Finished in time: let handlers complete, write persistence, close the collection.
        if app is not None:
            if app.running:
                await app.stop()
            await app.shutdown()
        await self.anki.run(self.service.close)
        if self.translator is not None:
            await self.translator.aclose()
        await self.lookup_thread.run(_close_lookup, self.lookup, self._cache)
        self.anki.shutdown()
        self.lookup_thread.shutdown()
        log.info("bot stopped")
        return True


async def serve(
    settings: Settings,
    runtime: Runtime | None = None,
    hard_exit: Callable[[int], None] = os._exit,
) -> None:
    runtime = runtime or Runtime(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await runtime.start()
        await stop.wait()
        log.info("stop signal received")
    finally:
        with suppress(NotImplementedError):
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)
    if not await runtime.stop():
        # A normal exit would wait for the busy executor thread forever. Committed SQLite
        # transactions are safe in the WAL; the interrupted sync runs again on next start.
        logging.shutdown()
        hard_exit(1)
