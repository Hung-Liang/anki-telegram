"""Input text -> candidates -> dictionary entries -> card data and preview content."""

from __future__ import annotations

import html
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ankibot.anki_service import KnownWords, dedup_key
from ankibot.dictionary import Dictionary
from ankibot.messages import PreviewContent
from ankibot.models import Candidate, CardData, Entry
from ankibot.tokenizer import Tokenizer, bold_span, build_candidates, to_hiragana


class ExampleFinder(Protocol):
    """Runs on the lookup thread."""

    def find(self, forms: Sequence[str]) -> str | None: ...

    def close(self) -> None: ...


class Translator(Protocol):
    # False when the target is English: the English glosses are kept as they are.
    translates_glosses: bool

    async def translate(
        self, texts: Sequence[str], source_lang: str, context: str | None = None
    ) -> list[str] | None:
        """Translations aligned with `texts`, or None when the request failed."""
        ...


class Worker(Protocol):
    async def run(self, fn: Callable[..., Any], *args: Any) -> Any: ...


@dataclass(frozen=True)
class Found:
    candidate: Candidate
    entry: Entry
    example: str | None


@dataclass(frozen=True)
class Analysis:
    is_sentence: bool
    found: tuple[Found, ...]
    not_found: tuple[str, ...]


class Lookup:
    """Tokenizer, dictionary and examples; create and use it on the lookup thread only."""

    def __init__(
        self,
        tokenizer: Tokenizer,
        dictionary: Dictionary,
        examples: ExampleFinder | None = None,
        include_proper_nouns: bool = False,
    ) -> None:
        self.tokenizer = tokenizer
        self.dictionary = dictionary
        self.examples = examples
        self.include_proper_nouns = include_proper_nouns

    @classmethod
    def create(cls, include_proper_nouns: bool, examples: ExampleFinder | None = None) -> Lookup:
        return cls(Tokenizer(), Dictionary(), examples, include_proper_nouns)

    def analyze(self, text: str) -> Analysis:
        tokens = self.tokenizer.tokenize(text)
        candidates = build_candidates(tokens, self.dictionary.exists, self.include_proper_nouns)
        found: list[Found] = []
        not_found: list[str] = []
        seen: set[tuple[str, str]] = set()
        for candidate in candidates.candidates:
            entry = self._lookup(candidate)
            if entry is None:
                not_found.append(candidate.surface)
                continue
            # Different spellings (たべる / 食べる) can resolve to the same entry.
            key = dedup_key(entry.word, entry.reading)
            if key in seen:
                continue
            seen.add(key)
            example = self.examples.find(entry.example_forms) if self.examples is not None else None
            found.append(Found(candidate, entry, example))
        return Analysis(candidates.is_sentence, tuple(found), tuple(dict.fromkeys(not_found)))

    def _lookup(self, candidate: Candidate) -> Entry | None:
        if candidate.kanji_lemma is not None and candidate.reading is not None:
            entry = self.dictionary.lookup(candidate.kanji_lemma, candidate.reading)
            # The kanji lemma can belong to an entry with another reading (いい's lemma 良い reads よい).
            if entry is not None and to_hiragana(entry.reading) == candidate.reading:
                return entry
        return self.dictionary.lookup(candidate.lemma, candidate.reading)

    def close(self) -> None:
        self.dictionary.close()
        if self.examples is not None:
            self.examples.close()


@dataclass(frozen=True)
class Draft:
    content: PreviewContent
    card: CardData


@dataclass(frozen=True)
class PipelineResult:
    drafts: tuple[Draft, ...]
    existing: tuple[str, ...]
    not_found: tuple[str, ...]
    text: str = ""
    is_sentence: bool = False
    # Aligned with `drafts`.
    found: tuple[Found, ...] = ()
    # The drafts are placeholders until `Pipeline.translate` delivers the translated ones.
    translating: bool = False


def _join_escaped(items: Sequence[str]) -> str:
    return "<br>".join(html.escape(i) for i in items)


class Pipeline:
    def __init__(
        self,
        thread: Worker,
        lookup: Lookup,
        known_words: Callable[[], KnownWords],
        ensure_fresh: Callable[[], Awaitable[None]],
        translator: Translator | None = None,
    ) -> None:
        self._thread = thread
        self._lookup = lookup
        self._known_words = known_words
        self._ensure_fresh = ensure_fresh
        self._translator = translator

    async def process(self, text: str) -> PipelineResult:
        """Untranslated drafts; when `translating` is set, `translate` yields the final ones."""
        await self._ensure_fresh()
        analysis = await self._thread.run(self._lookup.analyze, text)
        # Only the in-memory snapshot is read here, so a running sync never blocks the preview.
        known = self._known_words()
        new: list[Found] = []
        existing: list[str] = []
        for f in analysis.found:
            if known.contains(f.entry.word, f.entry.reading, f.entry.forms):
                existing.append(f.entry.word)
            else:
                new.append(f)

        translator = self._translator
        translating = (
            bool(new)
            and translator is not None
            and (translator.translates_glosses or analysis.is_sentence or any(f.example for f in new))
        )
        drafts = tuple(
            self._draft(text, analysis.is_sentence, f, None, None, None, False, translating) for f in new
        )
        return PipelineResult(
            drafts,
            tuple(existing),
            analysis.not_found,
            text,
            analysis.is_sentence,
            tuple(new),
            translating,
        )

    async def translate(self, result: PipelineResult) -> AsyncIterator[Draft]:
        """Translated drafts aligned with `result.drafts`, each yielded as soon as it is ready."""
        translator = self._translator
        if translator is None or not result.translating:
            return
        text, is_sentence = result.text, result.is_sentence
        sentence_tl: str | None = None
        sentence_failed = False
        if is_sentence:
            out = await translator.translate([text], "JA")
            sentence_tl = out[0] if out else None
            sentence_failed = out is None

        for f in result.found:
            entry = f.entry
            failed = sentence_failed
            context = f"{entry.word}（{entry.reading}）" + (f" {text}" if is_sentence else "")
            senses_tl: list[str] | None = None
            if translator.translates_glosses:
                senses_tl = await translator.translate(list(entry.senses), "EN", context)
                failed |= senses_tl is None
            example_tl: str | None = None
            if f.example:
                out = await translator.translate([f.example], "JA")
                example_tl = out[0] if out else None
                failed |= out is None
            yield self._draft(text, is_sentence, f, sentence_tl, senses_tl, example_tl, failed, False)

    def untranslated(self, result: PipelineResult) -> list[Draft]:
        """Final drafts without translations, for when `translate` breaks off."""
        text, is_sentence = result.text, result.is_sentence
        return [self._draft(text, is_sentence, f, None, None, None, True, False) for f in result.found]

    def _draft(
        self,
        text: str,
        is_sentence: bool,
        f: Found,
        sentence_tl: str | None,
        senses_tl: list[str] | None,
        example_tl: str | None,
        failed: bool,
        translating: bool,
    ) -> Draft:
        entry, cand = f.entry, f.candidate
        content = PreviewContent(
            word=entry.word,
            reading=entry.reading,
            senses=entry.senses,
            senses_tl=tuple(senses_tl) if senses_tl else None,
            sentence=text if is_sentence else None,
            start=cand.start,
            end=cand.end,
            sentence_tl=sentence_tl if is_sentence else None,
            example=f.example,
            example_tl=example_tl,
            translation_failed=failed,
            translating=translating,
            translates_senses=self._translator is None or self._translator.translates_glosses,
        )
        card = CardData(
            word=entry.word,
            reading=entry.reading,
            meaning=_join_escaped(entry.senses),
            meaning_tl=_join_escaped(senses_tl) if senses_tl else "",
            sentence=bold_span(text, cand.start, cand.end) if is_sentence else "",
            sentence_tl=html.escape(sentence_tl) if is_sentence and sentence_tl else "",
            example_jp=html.escape(f.example) if f.example else "",
            example_tl=html.escape(example_tl) if example_tl else "",
            audio="",
            source=html.escape(text),
            forms=entry.forms,
        )
        return Draft(content, card)
