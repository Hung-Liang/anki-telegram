from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    surface: str
    lemma: str
    # Reading of the dictionary form, in hiragana; None when it cannot be determined.
    reading: str | None
    start: int
    end: int
    pos: str
    # UniDic's kanji lemma when `lemma` is a kana orthBase; disambiguates homophones (こと: 事 / 琴).
    kanji_lemma: str | None = None


@dataclass(frozen=True)
class CandidateSet:
    candidates: tuple[Candidate, ...]
    is_sentence: bool


@dataclass(frozen=True)
class Entry:
    word: str
    # Kept exactly as in JMdict (katakana loanwords stay katakana).
    reading: str
    # Every written form that can be paired with `reading`, starting with `reading` itself.
    forms: tuple[str, ...]
    # Regular spellings for the example index; kana only when `word` is written in kana.
    example_forms: tuple[str, ...]
    senses: tuple[str, ...]
    is_common: bool
    pos: tuple[str, ...]


@dataclass(frozen=True)
class CardData:
    word: str
    reading: str
    meaning: str
    meaning_tl: str
    sentence: str
    sentence_tl: str
    example_jp: str
    example_tl: str
    # Local path of the generated audio file; empty when TTS failed.
    audio: str
    # Raw user input, HTML-escaped.
    source: str
    forms: tuple[str, ...]
