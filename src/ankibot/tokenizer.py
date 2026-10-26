from __future__ import annotations

import html
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import fugashi

from ankibot.models import Candidate, CandidateSet

KEEP_POS = frozenset({"名詞", "動詞", "形容詞", "形状詞", "副詞", "感動詞", "連体詞"})
COMPOUND_LAST_POS = frozenset({"名詞", "動詞", "形容詞", "接尾辞"})
COMPOUND_BREAK_POS = frozenset({"助詞", "補助記号", "記号", "空白"})
NOMINALIZING_PARTICLES = frozenset({"格助詞", "係助詞", "副助詞"})
STANDALONE_EXCLUDED = frozenset({"する", "いる", "ある", "なる"})
SENTENCE_END_MARKS = frozenset("。！？!?")
MAX_COMPOUND_TOKENS = 4

_KATAKANA_START = 0x30A1
_KATAKANA_END = 0x30F6
_KANA_OFFSET = 0x60


def to_hiragana(text: str) -> str:
    return "".join(
        chr(ord(c) - _KANA_OFFSET) if _KATAKANA_START <= ord(c) <= _KATAKANA_END else c for c in text
    )


def is_kana(text: str) -> bool:
    return bool(text) and all("ぁ" <= c <= "ゟ" or "゠" <= c <= "ヿ" for c in text)


def has_japanese(text: str) -> bool:
    return any("ぁ" <= c <= "ヿ" or "㐀" <= c <= "鿿" or c in "々〆ヶ" or "ｦ" <= c <= "ﾟ" for c in text)


def bold_span(text: str, start: int, end: int) -> str:
    # Positions refer to the unescaped text, so split first and escape each part separately.
    head, mid, tail = text[:start], text[start:end], text[end:]
    return f"{html.escape(head)}<b>{html.escape(mid)}</b>{html.escape(tail)}"


@dataclass(frozen=True)
class Token:
    surface: str
    pos1: str
    pos2: str
    c_form: str | None
    start: int
    end: int
    orth_base: str | None
    lemma: str | None
    kana: str | None
    kana_base: str | None


def _valid(value: str | None) -> str | None:
    if value is None or value == "*" or value == "":
        return None
    return value


def _strip_lemma_note(lemma: str | None) -> str | None:
    lemma = _valid(lemma)
    if lemma is None:
        return None
    # UniDic annotates some lemmas, e.g. "コーヒー-coffee".
    head, sep, _ = lemma.partition("-")
    return head if sep and head else lemma


class Tokenizer:
    """Wraps a fugashi Tagger; must be created and used on the lookup thread only."""

    def __init__(self) -> None:
        self._tagger = fugashi.Tagger()

    def tokenize(self, text: str) -> list[Token]:
        tokens: list[Token] = []
        cursor = 0
        for word in self._tagger(text):
            surface = word.surface
            start = text.find(surface, cursor)
            if start < 0:
                raise ValueError(f"token {surface!r} not found in input after offset {cursor}")
            end = start + len(surface)
            cursor = end
            f = word.feature
            tokens.append(
                Token(
                    surface=surface,
                    pos1=_valid(f.pos1) or "",
                    pos2=_valid(f.pos2) or "",
                    c_form=_valid(f.cForm),
                    start=start,
                    end=end,
                    orth_base=_valid(f.orthBase),
                    lemma=_strip_lemma_note(f.lemma),
                    kana=_valid(f.kana),
                    kana_base=_valid(f.kanaBase),
                )
            )
        return tokens


def _reading_piece(kana: str | None, written: str) -> str | None:
    if kana is not None:
        return to_hiragana(kana)
    if is_kana(written):
        return to_hiragana(written)
    return None


def _is_sentence_end(tokens: Sequence[Token], index: int) -> bool:
    return index >= len(tokens) or tokens[index].surface in SENTENCE_END_MARKS


def _last_token_forms(tokens: Sequence[Token], index: int) -> list[tuple[str, str]]:
    """Ordered (form, kind) attempts for the last token of a compound; kind is "surface" or "orth"."""
    token = tokens[index]
    nxt = index + 1
    nominalized = (
        token.c_form is not None
        and token.c_form.startswith("連用形")
        and (
            _is_sentence_end(tokens, nxt)
            or (tokens[nxt].pos1 == "助詞" and tokens[nxt].pos2 in NOMINALIZING_PARTICLES)
        )
    )
    attempts = [("orth", token.orth_base), ("surface", token.surface)]
    if nominalized:
        attempts.reverse()
    return [(form, kind) for kind, form in attempts if form is not None]


def _try_compound(
    tokens: Sequence[Token], i: int, size: int, exists: Callable[[str], bool]
) -> Candidate | None:
    if any(t.pos1 in COMPOUND_BREAK_POS for t in tokens[i : i + size]):
        return None
    last = tokens[i + size - 1]
    if last.pos1 not in COMPOUND_LAST_POS:
        return None
    head = tokens[i : i + size - 1]
    prefix = "".join(t.surface for t in head)
    for form, kind in _last_token_forms(tokens, i + size - 1):
        lemma = prefix + form
        if not exists(lemma):
            continue
        pieces = [_reading_piece(t.kana, t.surface) for t in head]
        if kind == "surface":
            pieces.append(_reading_piece(last.kana, last.surface))
        else:
            pieces.append(_reading_piece(last.kana_base, form))
        reading = None if any(p is None for p in pieces) else "".join(pieces)
        return Candidate(
            surface="".join(t.surface for t in tokens[i : i + size]),
            lemma=lemma,
            reading=reading,
            start=tokens[i].start,
            end=last.end,
            pos=last.pos1,
        )
    return None


def _single_candidate(
    token: Token, exists: Callable[[str], bool], include_proper_nouns: bool
) -> Candidate | None:
    if token.pos1 not in KEEP_POS:
        return None
    if token.pos2 == "数詞":
        return None
    if token.pos2 == "固有名詞" and not include_proper_nouns:
        return None
    if token.orth_base in STANDALONE_EXCLUDED:
        return None
    if not has_japanese(token.surface):
        return None
    forms = [f for f in (token.orth_base, token.lemma, token.surface) if f is not None]
    # Fall back to the first usable form so the caller can report it as not found.
    lemma = next((f for f in forms if exists(f)), forms[0])
    kanji_lemma = (
        token.lemma
        if token.lemma is not None
        and token.lemma != lemma
        and is_kana(lemma)
        and not is_kana(token.lemma)
        and exists(token.lemma)
        else None
    )
    return Candidate(
        surface=token.surface,
        lemma=lemma,
        reading=_reading_piece(token.kana_base, token.surface),
        start=token.start,
        end=token.end,
        pos=token.pos1,
        kanji_lemma=kanji_lemma,
    )


def build_candidates(
    tokens: Sequence[Token], exists: Callable[[str], bool], include_proper_nouns: bool = False
) -> CandidateSet:
    found: list[Candidate] = []
    i = 0
    while i < len(tokens):
        for size in range(min(MAX_COMPOUND_TOKENS, len(tokens) - i), 1, -1):
            compound = _try_compound(tokens, i, size, exists)
            if compound is not None:
                found.append(compound)
                i += size
                break
        else:
            single = _single_candidate(tokens[i], exists, include_proper_nouns)
            if single is not None:
                found.append(single)
            i += 1

    seen: set[tuple[str, str | None]] = set()
    unique: list[Candidate] = []
    for candidate in found:
        key = (candidate.lemma, candidate.reading)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)

    has_particle = any(t.pos1 == "助詞" for t in tokens)
    has_end_mark = any(t.surface in SENTENCE_END_MARKS for t in tokens)
    is_sentence = not (len(unique) == 1 and not has_particle and not has_end_mark)
    return CandidateSet(candidates=tuple(unique), is_sentence=is_sentence)
