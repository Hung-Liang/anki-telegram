"""Telegram handlers: previews, Save / Discard / Undo, commands, whitelist and background upkeep."""

from __future__ import annotations

import asyncio
import dataclasses
import html
import logging
import time
import warnings
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    TypeHandler,
    filters,
)

from ankibot.anki_service import AddStatus, DeleteStatus, Stats
from ankibot.config import Settings
from ankibot.i18n import Texts, load_texts
from ankibot.messages import headword, render_preview, replace_status, with_status
from ankibot.pipeline import Draft, Pipeline
from ankibot.state import (
    Preview,
    PreviewStatus,
    expire,
    get_previews,
    load_persistence_file,
    reset_saving,
)
from ankibot.sync import SyncManager, SyncStatus
from ankibot.tts import TTSProvider

log = logging.getLogger(__name__)

SAVE = "save"
DISCARD = "discard"
UNDO_PREFIX = "undo:"
MAX_RETRY_AFTER = 5
CLEANUP_INTERVAL_SECONDS = 600


class SaveInterrupted(Exception):
    """The TTS phase of a Save was cancelled by shutdown; the preview stays SAVING."""


def preview_keyboard(t: Texts) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(t.button_save, callback_data=SAVE),
                InlineKeyboardButton(t.button_discard, callback_data=DISCARD),
            ]
        ]
    )


def undo_keyboard(t: Texts, note_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t.button_undo, callback_data=f"{UNDO_PREFIX}{note_id}")]]
    )


def _retry_seconds(exc: RetryAfter) -> float:
    # retry_after is int or timedelta depending on PTB_TIMEDELTA; reading it as int warns.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        value = exc.retry_after
    return value.total_seconds() if isinstance(value, timedelta) else float(value)


def prepare_persistence(path: Path) -> PicklePersistence:
    load_persistence_file(path, datetime.now().strftime("%Y%m%d-%H%M%S"))
    return PicklePersistence(path)


async def _no_audio() -> tuple[str, bool]:
    return "", False


class BotHandlers:
    def __init__(
        self,
        settings: Settings,
        pipeline: Pipeline,
        sync: SyncManager,
        stats: Callable[[], Awaitable[Stats]],
        tts: TTSProvider | None = None,
        *,
        texts: Texts | None = None,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._pipeline = pipeline
        self._sync = sync
        self._stats = stats
        self._tts = tts
        self._t = texts or load_texts(settings.bot_language, settings.bot_messages_file)
        self._preview_keyboard = preview_keyboard(self._t)
        self._now = now
        self._sleep = sleep
        self._ttl = settings.pending_ttl_hours * 3600
        self._allowed = settings.tg_allowed_user_id
        self._tts_tasks: set[asyncio.Task[Path]] = set()

    def register(self, app: Application) -> None:
        app.add_handler(TypeHandler(Update, self._whitelist), group=-1)
        app.add_handler(CommandHandler(["help", "start"], self.on_help))
        app.add_handler(CommandHandler("sync", self.on_sync))
        app.add_handler(CommandHandler("stats", self.on_stats))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_handler(CallbackQueryHandler(self.on_callback))

    async def _whitelist(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or user.id != self._allowed:
            raise ApplicationHandlerStop

    # --- Telegram calls ------------------------------------------------------------

    async def _call(self, fn: Callable[..., Awaitable[Any]], **kwargs: Any) -> Any:
        """Call the Bot API, waiting out RetryAfter so messages keep their order."""
        for _ in range(MAX_RETRY_AFTER):
            try:
                return await fn(**kwargs)
            except RetryAfter as exc:
                seconds = _retry_seconds(exc)
                log.warning("rate limited by Telegram; retrying in %ss", seconds)
                await self._sleep(seconds)
        return await fn(**kwargs)

    async def _send(
        self,
        bot: Bot,
        chat_id: int,
        text: str,
        markup: InlineKeyboardMarkup | None = None,
        reply_to: int | None = None,
    ) -> Message | None:
        reply = ReplyParameters(reply_to, allow_sending_without_reply=True) if reply_to else None
        try:
            return await self._call(
                bot.send_message,
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                reply_parameters=reply,
            )
        except TelegramError as exc:
            log.error("send_message to %s failed: %s", chat_id, exc)
            return None

    async def _edit(
        self, bot: Bot, chat_id: int, message_id: int, text: str, markup: InlineKeyboardMarkup | None
    ) -> bool:
        try:
            await self._call(
                bot.edit_message_text,
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
            return True
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return True
            log.warning("edit of message %s failed: %s", message_id, exc)
            return False
        except TelegramError as exc:
            log.warning("edit of message %s failed: %s", message_id, exc)
            return False

    async def _remove_buttons(self, bot: Bot, chat_id: int, message_id: int) -> None:
        try:
            await self._call(bot.edit_message_reply_markup, chat_id=chat_id, message_id=message_id)
        except TelegramError as exc:
            if "not modified" not in str(exc).lower():
                log.warning("removing buttons of message %s failed: %s", message_id, exc)

    async def _answer(self, update: Update, text: str | None = None) -> None:
        try:
            await self._call(update.callback_query.answer, text=text)
        except TelegramError as exc:
            log.warning("answering callback failed: %s", exc)

    async def notify(self, bot: Bot, text: str) -> None:
        await self._send(bot, self._allowed, html.escape(text))

    # --- text ----------------------------------------------------------------------

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        text = (message.text or "").strip()
        if not text:
            return
        chat_id = message.chat_id
        try:
            result = await self._pipeline.process(text)
        except Exception:
            log.exception("processing %r failed", text)
            await self._send(context.bot, chat_id, self._t.reply_processing_failed)
            return

        sep = self._t.reply_word_separator
        if not result.drafts:
            lines = [self._t.reply_no_new_words]
            if result.existing:
                lines.append(self._t.reply_existing.format(words=sep.join(result.existing)))
            await self._send(context.bot, chat_id, html.escape("\n".join(lines)))

        # While translating, previews go out without buttons and are edited once their draft is final.
        sent_ids: list[int | None] = []
        for draft in result.drafts:
            if result.translating:
                sent = await self._send(context.bot, chat_id, render_preview(draft.content, self._t))
                sent_ids.append(sent.message_id if sent is not None else None)
            else:
                await self._offer(context, chat_id, draft, None)

        if result.not_found:
            await self._send(
                context.bot,
                chat_id,
                html.escape(self._t.reply_not_found.format(words=sep.join(result.not_found))),
            )

        if not result.translating:
            return
        done = 0
        try:
            async for draft in self._pipeline.translate(result):
                await self._offer(context, chat_id, draft, sent_ids[done])
                done += 1
        except Exception:
            log.exception("translating %r failed", text)
            for draft, message_id in zip(
                self._pipeline.untranslated(result)[done:], sent_ids[done:], strict=True
            ):
                await self._offer(context, chat_id, draft, message_id)

    async def _offer(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, draft: Draft, message_id: int | None
    ) -> None:
        """Show the final preview with Save / Discard: edit the placeholder, or send a new message."""
        bot, body = context.bot, render_preview(draft.content, self._t)
        if message_id is None or not await self._edit(bot, chat_id, message_id, body, self._preview_keyboard):
            sent = await self._send(bot, chat_id, body, self._preview_keyboard)
            if sent is None:
                return
            message_id = sent.message_id
        previews = get_previews(context.user_data)
        previews[message_id] = Preview(chat_id, message_id, body, draft.card, self._now())

    # --- buttons -------------------------------------------------------------------

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        data = update.callback_query.data or ""
        if data.startswith(UNDO_PREFIX):
            await self._undo(update, context, data.removeprefix(UNDO_PREFIX))
        elif data in (SAVE, DISCARD):
            await self._save_or_discard(update, context, data)
        else:
            await self._answer(update, self._t.answer_expired)

    def _take_pending(self, context: ContextTypes.DEFAULT_TYPE, message_id: int | None) -> Preview | None:
        previews = get_previews(context.user_data)
        preview = previews.get(message_id) if message_id is not None else None
        if preview is None or preview.status is not PreviewStatus.PENDING:
            return None
        if preview.is_expired(self._now(), self._ttl):
            del previews[message_id]
            return None
        return preview

    async def _save_or_discard(self, update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> None:
        query = update.callback_query
        message_id = query.message.message_id if query.message is not None else None
        preview = self._take_pending(context, message_id)
        if preview is None:
            await self._answer(update, self._t.answer_expired)
            return
        previews = get_previews(context.user_data)
        if action == DISCARD:
            # Removed before awaiting so a concurrent Save sees it as handled.
            del previews[message_id]
            await self._answer(update)
            await self._edit(
                context.bot,
                preview.chat_id,
                message_id,
                with_status(preview.body_html, self._t.status_discarded),
                None,
            )
            return

        # No await between the check above and this transition, so a second press sees SAVING.
        previews[message_id] = preview.with_status(PreviewStatus.SAVING)
        await self._answer(update)
        try:
            await self._save(context, preview)
        except SaveInterrupted:
            log.info("save of message %s interrupted by shutdown; it stays SAVING", message_id)

    async def _synthesize(self, reading: str) -> tuple[str, bool]:
        """(audio path or "", failed)."""
        if self._tts is None:
            return "", False
        task = asyncio.create_task(self._tts.synth(reading))
        self._tts_tasks.add(task)
        try:
            return str(await task), False
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task.cancelled() and (current is None or not current.cancelling()):
                raise SaveInterrupted from None
            raise
        except Exception as exc:
            log.warning("TTS for %r failed: %r", reading, exc)
            return "", True
        finally:
            self._tts_tasks.discard(task)

    def cancel_tts(self) -> int:
        """Shutdown: cancel saves still in the TTS phase (not yet queued for the Anki thread)."""
        pending = [t for t in self._tts_tasks if not t.done()]
        for task in pending:
            task.cancel()
        return len(pending)

    async def _save(self, context: ContextTypes.DEFAULT_TYPE, preview: Preview) -> None:
        bot, chat_id, mid = context.bot, preview.chat_id, preview.message_id
        previews = get_previews(context.user_data)
        waiting = self._t.status_queued if self._sync.is_syncing else self._t.status_saving
        await self._edit(bot, chat_id, mid, with_status(preview.body_html, waiting), None)

        sentence = preview.card.tts_sentence if self._settings.tts_sentence else ""
        (audio, word_failed), (sentence_audio, sentence_failed) = await asyncio.gather(
            self._synthesize(preview.card.reading),
            self._synthesize(sentence) if sentence else _no_audio(),
        )
        tts_failed = word_failed or sentence_failed
        card = dataclasses.replace(preview.card, audio=audio, sentence_audio=sentence_audio)
        try:
            result = await self._sync.add_note(card)
        except Exception:
            log.exception("saving %s failed", card.word)
            previews[mid] = preview.with_status(PreviewStatus.PENDING)
            await self._edit(
                bot,
                chat_id,
                mid,
                with_status(preview.body_html, self._t.status_failed),
                self._preview_keyboard,
            )
            return

        if result.status is AddStatus.BLOCKED:
            previews[mid] = preview.with_status(PreviewStatus.PENDING)
            await self._edit(
                bot,
                chat_id,
                mid,
                with_status(preview.body_html, self._t.status_blocked),
                self._preview_keyboard,
            )
            return
        if result.status is AddStatus.EXISTS:
            await self._edit(bot, chat_id, mid, with_status(preview.body_html, self._t.status_exists), None)
            previews.pop(mid, None)
            return

        hint = self._t.hint_tts_failed if tts_failed else None
        markup = undo_keyboard(self._t, result.note_id)
        text = with_status(preview.body_html, self._t.status_saved, hint)
        # The note is written: from here on an edit failure must never lead to another add.
        if not await self._edit(bot, chat_id, mid, text, markup):
            fallback = with_status(headword(card.word, card.reading), self._t.status_saved)
            if await self._send(bot, chat_id, fallback, markup, reply_to=mid) is None:
                log.error(
                    "note %s saved but neither the edit nor the fallback message went through", result.note_id
                )
        previews.pop(mid, None)

    async def _undo(self, update: Update, context: ContextTypes.DEFAULT_TYPE, raw_id: str) -> None:
        query = update.callback_query
        try:
            note_id = int(raw_id)
        except ValueError:
            await self._answer(update, self._t.answer_expired)
            return
        try:
            status = await self._sync.delete_note(note_id)
        except Exception:
            log.exception("undo of note %s failed", note_id)
            await self._answer(update, self._t.answer_undo_failed)
            return

        message = query.message if isinstance(query.message, Message) else None
        if status is DeleteStatus.BLOCKED:
            await self._answer(update, self._t.answer_undo_blocked)
            return
        if status is DeleteStatus.DELETED:
            if message is not None and message.text:
                # text_html restores entities (bold) and escapes the text; .text would lose both.
                text = replace_status(message.text_html, self._t.status_undone)
                await self._answer(update)
                await self._edit(context.bot, message.chat_id, message.message_id, text, None)
            else:
                await self._answer(update, self._t.status_undone)
            return

        if status is DeleteStatus.REJECTED:
            log.warning(
                "undo of note %s rejected: it is no longer a %s note", note_id, self._settings.anki_notetype
            )
        if message is not None:
            await self._remove_buttons(context.bot, message.chat_id, message.message_id)
        await self._answer(
            update, self._t.answer_note_gone if status is DeleteStatus.NOT_FOUND else self._t.answer_rejected
        )

    # --- commands ------------------------------------------------------------------

    async def on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._send(context.bot, update.effective_chat.id, html.escape(self._t.reply_help))

    async def on_sync(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        progress = await self._send(context.bot, chat_id, html.escape(self._t.sync_in_progress))
        result = await self._sync.sync_now()
        stamp = result.finished_at.strftime("%Y-%m-%d %H:%M:%S")
        t = self._t
        if result.status is SyncStatus.OK:
            text = t.sync_ok.format(time=stamp)
        elif result.status is SyncStatus.FULL_SYNC_REQUIRED:
            text = t.sync_full_sync_required.format(reason=result.error, recovery=t.sync_recovery_hint)
        elif result.status is SyncStatus.SKIPPED:
            text = t.sync_skipped.format(time=stamp)
        else:
            text = t.sync_failed.format(time=stamp, error=result.error)
        text = html.escape(text)
        if progress is None or not await self._edit(context.bot, chat_id, progress.message_id, text, None):
            await self._send(context.bot, chat_id, text)

    async def on_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        stats = await self._stats()
        last = self._sync.last_sync_at
        t = self._t
        lines = [
            t.stats_added_today.format(count=stats.added_today),
            t.stats_deck_cards.format(count=stats.deck_cards),
            t.stats_last_sync.format(
                time=last.strftime("%Y-%m-%d %H:%M:%S") if last else t.stats_never_synced
            ),
        ]
        if self._sync.full_sync_required:
            lines.append(t.stats_full_sync_required)
        await self._send(context.bot, update.effective_chat.id, html.escape("\n".join(lines)))

    # --- background ----------------------------------------------------------------

    async def _persist(self, app: Application) -> None:
        # Changes made outside an Update are only written for users marked explicitly.
        app.mark_data_for_update_persistence(user_ids=[self._allowed])
        await app.update_persistence()

    async def recover(self, app: Application) -> None:
        """On startup: previews persisted as SAVING were interrupted; offer Save again."""
        user_data = app.user_data.get(self._allowed)
        if not user_data:
            return
        reset = reset_saving(user_data)
        if not reset:
            return
        await self._persist(app)
        for preview in reset:
            text = with_status(preview.body_html, self._t.status_interrupted)
            await self._edit(app.bot, preview.chat_id, preview.message_id, text, self._preview_keyboard)
        log.info("reset %d interrupted saves to PENDING", len(reset))

    async def cleanup_once(self, app: Application) -> list[int]:
        user_data = app.user_data.get(self._allowed)
        if not user_data:
            return []
        expired = expire(user_data, self._now(), self._ttl)
        if expired:
            await self._persist(app)
            log.info("removed %d expired previews", len(expired))
        return expired

    async def cleanup_loop(self, app: Application, interval: float = CLEANUP_INTERVAL_SECONDS) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.cleanup_once(app)
            except Exception:
                log.exception("preview cleanup failed")


def build_application(
    settings: Settings,
    handlers: BotHandlers,
    *,
    request: Any | None = None,
    persistence: PicklePersistence | None = None,
) -> Application:
    builder = Application.builder().token(settings.tg_token.get_secret_value()).concurrent_updates(True)
    if request is not None:
        builder = builder.request(request).get_updates_request(request)
    if persistence is not None:
        builder = builder.persistence(persistence)
    app = builder.build()
    handlers.register(app)
    return app
