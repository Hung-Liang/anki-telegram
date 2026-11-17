import asyncio
import dataclasses
import html
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ankibot.anki_service import AddStatus
from ankibot.pipeline import Pipeline, Translator, _join_escaped
from ankibot.sync import SyncManager
from ankibot.tts import TTSProvider

log = logging.getLogger(__name__)

# Backoff before each retry of a failed translation.
RETRY_DELAYS = (5.0, 15.0, 30.0)
INBOX_INTERVAL_SECONDS = 5.0
RESULT_SUFFIX = ".result.json"

_KEYS = {"word", "translation", "example", "example_translation"}


@dataclass(frozen=True)
class ImportItem:
    word: str
    translation: tuple[str, ...] | None = None
    example: str | None = None
    example_translation: str | None = None


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _item(entry) -> ImportItem:
    if isinstance(entry, str):
        return ImportItem(_text(entry, "word"))
    if not isinstance(entry, dict):
        raise ValueError("must be a string or an object")
    if unknown := set(entry) - _KEYS:
        raise ValueError(f"unknown keys: {', '.join(sorted(unknown))}")
    if "word" not in entry:
        raise ValueError("missing word")
    if "example_translation" in entry and "example" not in entry:
        raise ValueError("example_translation requires example")
    translation = None
    if "translation" in entry:
        items = [entry["translation"]] if isinstance(entry["translation"], str) else entry["translation"]
        if not isinstance(items, list) or not items:
            raise ValueError("translation must be a string or a non-empty array of strings")
        translation = tuple(_text(t, "translation") for t in items)
    opt = {k: _text(entry[k], k) for k in ("example", "example_translation") if k in entry}
    return ImportItem(_text(entry["word"], "word"), translation, **opt)


def parse_words(raw: str) -> list[ImportItem]:
    """Validate the whole file up front; any bad item rejects all of it."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON: {e}") from e
    if not isinstance(data, list):
        raise ValueError("top level must be an array")
    items = []
    for i, entry in enumerate(data, 1):
        try:
            items.append(_item(entry))
        except ValueError as e:
            raise ValueError(f"item {i}: {e}") from None
    return items


class ItemStatus(StrEnum):
    ADDED = "added"
    EXISTS = "exists"
    NOT_FOUND = "not_found"
    NOT_SINGLE_WORD = "not_single_word"
    TRANSLATION_FAILED = "translation_failed"
    BLOCKED = "blocked"
    ERROR = "error"


@dataclass(frozen=True)
class ItemResult:
    word: str
    status: ItemStatus


_ADD_STATUS = {
    AddStatus.ADDED: ItemStatus.ADDED,
    AddStatus.EXISTS: ItemStatus.EXISTS,
    AddStatus.BLOCKED: ItemStatus.BLOCKED,
}


class _TranslationFailed(Exception):
    pass


class Importer:
    def __init__(
        self,
        pipeline: Pipeline,
        translator: Translator | None,
        sync: SyncManager,
        tts: TTSProvider | None,
        sentence_audio: bool,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._pipeline = pipeline
        self._translator = translator
        self._sync = sync
        self._tts = tts
        self._sentence_audio = sentence_audio
        self._sleep = sleep

    async def inbox_loop(self, inbox: Path, interval: float = INBOX_INTERVAL_SECONDS) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.process_inbox(inbox)
            except Exception:
                log.exception("inbox scan failed")

    async def process_inbox(self, inbox: Path) -> None:
        # Temp files end in .tmp, so the glob already skips them.
        for job in sorted(p for p in inbox.glob("*.json") if not p.name.endswith(RESULT_SUFFIX)):
            await self._process_job(job)

    async def _process_job(self, job: Path) -> None:
        log.info("importing %s", job.name)
        try:
            items = parse_words(job.read_text(encoding="utf-8"))
        except ValueError as e:
            result: dict = {"error": str(e)}
        else:
            result = {"results": [dataclasses.asdict(await self.import_word(i)) for i in items]}
        out = job.with_name(job.stem + RESULT_SUFFIX)
        tmp = out.with_name(out.name + ".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        tmp.replace(out)
        # Deleted only after the result exists: a job interrupted before this reruns in full,
        # and words already saved come back as exists.
        job.unlink()

    async def import_word(self, item: ImportItem) -> ItemResult:
        try:
            status = await self._import(item)
        except _TranslationFailed:
            status = ItemStatus.TRANSLATION_FAILED
        except Exception:
            log.exception("importing %s failed", item.word)
            status = ItemStatus.ERROR
        return ItemResult(item.word, status)

    async def _import(self, item: ImportItem) -> ItemStatus:
        result = await self._pipeline.process(item.word)
        found = len(result.drafts) + len(result.existing)
        if not result.is_sentence and found == 0 and result.not_found:
            return ItemStatus.NOT_FOUND
        if result.is_sentence or found != 1:
            return ItemStatus.NOT_SINGLE_WORD
        if result.existing:
            return ItemStatus.EXISTS
        if result.not_found:
            return ItemStatus.NOT_FOUND
        entry = result.found[0].entry
        example = item.example or result.found[0].example or ""

        translator = self._translator
        meaning_tl = item.translation
        if meaning_tl is None and translator is not None and translator.translates_glosses:
            meaning_tl = await self._translate(entry.senses, "EN", f"{entry.word}（{entry.reading}）")
        example_tl = item.example_translation
        if example_tl is None and example and translator is not None:
            example_tl = (await self._translate([example], "JA"))[0]

        audio = await self._synthesize(entry.reading)
        sentence_audio = await self._synthesize(example) if self._sentence_audio and example else ""
        card = dataclasses.replace(
            result.drafts[0].card,
            meaning_tl=_join_escaped(meaning_tl) if meaning_tl else "",
            example_jp=html.escape(example),
            example_tl=html.escape(example_tl) if example_tl else "",
            tts_sentence=example,
            audio=audio,
            sentence_audio=sentence_audio,
        )
        return _ADD_STATUS[(await self._sync.add_note(card)).status]

    async def _translate(
        self, texts: Sequence[str], source_lang: str, context: str | None = None
    ) -> list[str]:
        assert self._translator is not None
        for delay in (*RETRY_DELAYS, None):
            out = await self._translator.translate(texts, source_lang, context)
            if out is not None:
                return out
            if delay is not None:
                await self._sleep(delay)
        raise _TranslationFailed

    async def _synthesize(self, text: str) -> str:
        if self._tts is None:
            return ""
        try:
            return str(await self._tts.synth(text))
        except Exception as exc:
            log.warning("TTS for %r failed: %r", text, exc)
            return ""
