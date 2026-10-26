from __future__ import annotations

import logging
from typing import Any

from jamdict import Jamdict

from ankibot.models import Entry
from ankibot.tokenizer import to_hiragana

log = logging.getLogger(__name__)

# JMdict priority tags that mark a word as "common".
COMMON_PRI = frozenset({"news1", "ichi1", "spec1", "spec2", "gai1"})
USUALLY_KANA = "word usually written using kana alone"
# Kanji form tags (KJI) for spellings that are never shown or used to find examples (机 also lists 案).
IRREGULAR_KANJI = frozenset(
    {
        "word containing irregular kana usage",
        "word containing irregular kanji usage",
        "word containing out-dated kanji",
    }
)
# jamdict switches to a LIKE query when the query contains any of these, and treats "id#" as an id lookup.
_WILDCARD_CHARS = frozenset("%_@")
MAX_SENSES = 3


def _to_katakana(text: str) -> str:
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in text)


def _texts(entry: Any) -> list[str]:
    return [k.text for k in entry.kanji_forms] + [r.text for r in entry.kana_forms]


def _is_common(entry: Any) -> bool:
    return any(set(f.pri) & COMMON_PRI for f in [*entry.kanji_forms, *entry.kana_forms])


def _sense_text(sense: Any) -> str:
    return "; ".join(g.text for g in sense.gloss)


class Dictionary:
    """Wraps jamdict; must be created and used on the lookup thread only."""

    def __init__(self, jam: Any | None = None) -> None:
        self._ctx: Any | None = None
        if jam is None:
            jam = Jamdict()
            # Own the SQLite context so it can be closed on this thread; jamdict's implicit one is
            # closed by __del__ on whichever thread collects it, which sqlite3 rejects.
            self._ctx = jam.jmdict.ctx()
        self._jam = jam
        self._cache: dict[str, list[Any]] = {}

    def _query(self, form: str) -> list[Any]:
        extra = {"ctx": self._ctx} if self._ctx is not None else {}
        result = self._jam.lookup(form, strict_lookup=True, lookup_chars=False, lookup_ne=False, **extra)
        return list(result.entries)

    def close(self) -> None:
        if self._ctx is not None:
            self._ctx.close()
            self._ctx = None

    def _find(self, form: str) -> list[Any]:
        if form in self._cache:
            return self._cache[form]
        entries: list[Any] = []
        if form and not (_WILDCARD_CHARS & set(form)) and not form.startswith("id#"):
            # jamdict also matches English glosses, so keep only entries that have this written form.
            entries = [e for e in self._query(form) if form in _texts(e)]
            if not entries:
                normalized = to_hiragana(form)
                seen: set[Any] = set()
                for variant in dict.fromkeys((normalized, _to_katakana(normalized))):
                    if variant == form:
                        continue
                    for e in self._query(variant):
                        if e.idseq not in seen and any(to_hiragana(t) == normalized for t in _texts(e)):
                            seen.add(e.idseq)
                            entries.append(e)
        self._cache[form] = entries
        return entries

    def exists(self, form: str) -> bool:
        return bool(self._find(form))

    def lookup(self, lemma: str, reading: str | None) -> Entry | None:
        entries = self._find(lemma)
        if not entries:
            return None
        target = to_hiragana(reading) if reading is not None else None

        def matches_reading(entry: Any) -> bool:
            return target is not None and any(to_hiragana(r.text) == target for r in entry.kana_forms)

        # sorted() is stable, so ties keep JMdict order.
        best = sorted(entries, key=lambda e: (not matches_reading(e), not _is_common(e)))[0]
        return self._build(best, lemma, target)

    def _build(self, entry: Any, lemma: str, target: str | None) -> Entry:
        matching = [r for r in entry.kana_forms if target is not None and to_hiragana(r.text) == target]
        # Several kana forms can normalize to the same reading (カラオケ / からオケ); prefer the queried one.
        kana = next(
            (r for r in matching if r.text == lemma), matching[0] if matching else entry.kana_forms[0]
        )
        reading = kana.text
        compatible = (
            []
            if kana.nokanji
            else [k.text for k in entry.kanji_forms if not kana.restr or k.text in kana.restr]
        )
        regular = [
            k.text for k in entry.kanji_forms if k.text in compatible and not set(k.info) & IRREGULAR_KANJI
        ] or compatible
        reading_senses = [s for s in entry.senses if not s.stagr or reading in s.stagr]
        usually_kana = bool(reading_senses) and all(USUALLY_KANA in s.misc for s in reading_senses)
        word = reading if usually_kana or not regular else regular[0]
        # Kana forms match homophones in the example index (こと finds 事 sentences for 琴),
        # so they are used only when the word itself is written in kana.
        example_forms = (reading, *regular) if word == reading else tuple(regular)

        senses = [
            s
            for s in entry.senses
            if (not s.stagk or word in s.stagk) and (not s.stagr or reading in s.stagr)
        ]
        if not senses:
            log.warning("no sense of JMdict entry %s applies to (%s, %s)", entry.idseq, word, reading)
            senses = list(entry.senses)
        senses = senses[:MAX_SENSES]

        return Entry(
            word=word,
            reading=reading,
            forms=(reading, *compatible),
            example_forms=example_forms,
            senses=tuple(_sense_text(s) for s in senses),
            is_common=_is_common(entry),
            pos=tuple(dict.fromkeys(p for s in senses for p in s.pos)),
        )
