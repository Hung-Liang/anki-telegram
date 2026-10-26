"""Telegram message text: previews, hint lines and the status line (always the single last line)."""

from __future__ import annotations

import html
from dataclasses import dataclass

from ankibot.i18n import Texts
from ankibot.tokenizer import bold_span

MAX_MESSAGE = 4096
SENTENCE_LIMIT = 300
ELLIPSIS = "…"


def tg_len(text: str) -> int:
    """Length in UTF-16 code units, which is how Telegram counts (conservative for HTML)."""
    return len(text.encode("utf-16-le")) // 2


def reserve(t: Texts) -> int:
    """Room kept free in every preview so later edits (hint line + status line) never exceed the limit."""
    return tg_len("\n" + html.escape(t.hint_tts_failed)) + max(
        tg_len("\n" + html.escape(s)) for s in t.statuses
    )


# Successive shortening steps: example first, then meanings, then the sentence translation and sentence.
_LEVELS: tuple[dict[str, int], ...] = (
    {},
    {"example": 100},
    {"example": 40},
    {"example": 0},
    {"example": 0, "sense": 200},
    {"example": 0, "sense": 80},
    {"example": 0, "sense": 30},
    {"example": 0, "sense": 30, "sentence_tl": 100},
    {"example": 0, "sense": 30, "sentence_tl": 100, "sentence": 100},
    {"example": 0, "sense": 20, "sentence_tl": 0, "sentence": 40},
)


@dataclass(frozen=True)
class PreviewContent:
    """Raw (unescaped) preview parts."""

    word: str
    reading: str
    senses: tuple[str, ...]
    # Aligned with `senses`; None when there is no translation.
    senses_tl: tuple[str, ...] | None
    # Full original sentence; None for single-word input.
    sentence: str | None
    start: int
    end: int
    sentence_tl: str | None
    example: str | None
    example_tl: str | None
    translation_failed: bool
    # Sent before the translations arrive: every translation slot shows a placeholder.
    translating: bool = False
    # False when the senses stay untranslated, so they get no placeholder.
    translates_senses: bool = True


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + ELLIPSIS if limit > 0 else ""


def truncate_around(text: str, start: int, end: int, limit: int = SENTENCE_LIMIT) -> tuple[str, int, int]:
    """Cut `text` to a `limit`-char window that contains start..end (roughly centred), adding "…"."""
    if len(text) <= limit:
        return text, start, end
    extra = max(limit - (end - start), 0)
    ws = max(0, start - extra // 2)
    we = min(len(text), max(ws + limit, end))
    ws = min(ws, max(0, we - limit))
    prefix = ELLIPSIS if ws > 0 else ""
    suffix = ELLIPSIS if we < len(text) else ""
    return prefix + text[ws:we] + suffix, start - ws + len(prefix), end - ws + len(prefix)


def headword(word: str, reading: str) -> str:
    if word == reading:
        return f"<b>{html.escape(word)}</b>"
    return f"<b>{html.escape(word)}</b>（{html.escape(reading)}）"


def _render(c: PreviewContent, t: Texts, level: dict[str, int]) -> str:
    def cut(text: str, key: str) -> str:
        return html.escape(shorten(text, level[key]) if key in level else text)

    def translated(escaped: str) -> str:
        return escaped if c.translating else f"<b>{escaped}</b>"

    pending = t.preview_translating if c.translating else None
    lines: list[str] = []
    if c.sentence is not None:
        snippet, s, e = truncate_around(c.sentence, c.start, c.end, level.get("sentence", SENTENCE_LIMIT))
        lines.append(bold_span(snippet, s, e))
        sentence_tl = pending or c.sentence_tl
        if sentence_tl and level.get("sentence_tl", 1):
            tl_limit = min(level.get("sentence_tl", SENTENCE_LIMIT), SENTENCE_LIMIT)
            lines.append(translated(html.escape(shorten(sentence_tl, tl_limit))))
    lines.append(headword(c.word, c.reading))
    for i, sense in enumerate(c.senses, 1):
        tl = (pending if c.translates_senses else None) or (
            c.senses_tl[i - 1] if c.senses_tl and i <= len(c.senses_tl) else ""
        )
        if tl:
            lines.append(f"{i}. {translated(cut(tl, 'sense'))}")
            lines.append(f"   {cut(sense, 'sense')}")
        else:
            lines.append(f"{i}. {cut(sense, 'sense')}")
    if c.example and level.get("example", 1):
        lines.append(html.escape(t.preview_example).format(example=cut(c.example, "example")))
        example_tl = pending or c.example_tl
        if example_tl:
            lines.append(f"　{translated(cut(example_tl, 'example'))}")
    if c.translation_failed:
        lines.append(html.escape(t.hint_translation_failed))
    return "\n".join(lines)


def render_preview(c: PreviewContent, t: Texts, max_len: int = MAX_MESSAGE) -> str:
    """Preview body without a status line, short enough for every later edit."""
    budget = max_len - reserve(t)
    body = ""
    for level in _LEVELS:
        body = _render(c, t, level)
        if tg_len(body) <= budget:
            return body
    raise ValueError(f"preview for {c.word!r} cannot be shortened below {budget} characters")


def with_status(body: str, status: str, hint: str | None = None) -> str:
    """`body` is HTML; `status` and `hint` are plain text."""
    parts = [body] if body else []
    if hint:
        parts.append(html.escape(hint))
    parts.append(html.escape(status))
    return "\n".join(parts)


def replace_status(text: str, status: str) -> str:
    head, sep, _ = text.rpartition("\n")
    return f"{head}{sep}{html.escape(status)}"
