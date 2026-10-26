import re

import pytest

from ankibot.i18n import load_texts
from ankibot.messages import (
    MAX_MESSAGE,
    SENTENCE_LIMIT,
    PreviewContent,
    render_preview,
    replace_status,
    shorten,
    tg_len,
    truncate_around,
    with_status,
)

T = load_texts()
LONGEST_STATUS = max(T.statuses, key=tg_len)


def content(**kwargs):
    base = dict(
        word="食べる",
        reading="たべる",
        senses=("to eat",),
        senses_tl=None,
        sentence=None,
        start=0,
        end=0,
        sentence_tl=None,
        example=None,
        example_tl=None,
        translation_failed=False,
    )
    base.update(kwargs)
    return PreviewContent(**base)


def last_line(text):
    return text.rsplit("\n", 1)[-1]


def test_word_reading_and_senses():
    body = render_preview(content(senses=("to eat", "to live on"), senses_tl=("吃", "靠…生活")), T)
    assert body.splitlines() == [
        "<b>食べる</b>（たべる）",
        "1. <b>吃</b>",
        "   to eat",
        "2. <b>靠…生活</b>",
        "   to live on",
    ]


def test_kana_word_shows_reading_once():
    assert render_preview(content(word="ある", reading="ある"), T).splitlines()[0] == "<b>ある</b>"


def test_sentence_lines_come_first_and_bold_the_candidate():
    body = render_preview(content(sentence="パンを食べた。", start=3, end=5, sentence_tl="吃了麵包。"), T)
    lines = body.splitlines()
    assert lines[0] == "パンを<b>食べ</b>た。"
    assert lines[1] == "<b>吃了麵包。</b>"
    assert lines[2] == "<b>食べる</b>（たべる）"


def test_sentence_with_html_specials_keeps_bold_position():
    text = "a<b>&c 食べた"
    start = text.index("食べ")
    body = render_preview(content(sentence=text, start=start, end=start + 2), T)
    assert body.splitlines()[0] == "a&lt;b&gt;&amp;c <b>食べ</b>た"


def test_example_and_translation_failed_line():
    body = render_preview(
        content(example="毎日パンを食べる。", example_tl="每天吃麵包。", translation_failed=True), T
    )
    lines = body.splitlines()
    assert lines[-3:] == ["e.g. 毎日パンを食べる。", "　<b>每天吃麵包。</b>", T.hint_translation_failed]


def test_translating_shows_placeholder_in_every_translation_slot():
    body = render_preview(
        content(
            sentence="パンを食べた。",
            start=3,
            end=5,
            senses=("to eat", "to live on"),
            example="毎日パンを食べる。",
            translating=True,
        ),
        T,
    )
    assert body.splitlines() == [
        "パンを<b>食べ</b>た。",
        T.preview_translating,
        "<b>食べる</b>（たべる）",
        f"1. {T.preview_translating}",
        "   to eat",
        f"2. {T.preview_translating}",
        "   to live on",
        "e.g. 毎日パンを食べる。",
        f"　{T.preview_translating}",
    ]


def test_without_translation_only_english_is_shown():
    body = render_preview(content(senses=("to eat",), senses_tl=None), T)
    assert body.splitlines()[1:] == ["1. to eat"]


@pytest.mark.parametrize("hint", [None, T.hint_tts_failed])
@pytest.mark.parametrize("status", [T.status_saved, T.status_queued, T.status_failed])
def test_status_is_always_the_last_line(hint, status):
    body = render_preview(content(translation_failed=True), T)
    text = with_status(body, status, hint)
    lines = text.splitlines()
    assert lines[-1] == status
    if hint:
        assert lines[-2] == hint
    assert lines[-3 if hint else -2] == T.hint_translation_failed


def test_replace_status_only_touches_the_last_line():
    body = render_preview(content(sentence="パンを食べた。", start=3, end=5), T)
    saved = with_status(body, T.status_saved, T.hint_tts_failed)
    undone = replace_status(saved, T.status_undone)
    assert undone == with_status(body, T.status_undone, T.hint_tts_failed)


def test_truncate_around_keeps_candidate_inside_window():
    text = "あ" * 500 + "食べ" + "い" * 500
    snippet, start, end = truncate_around(text, 500, 502)
    assert snippet[start:end] == "食べ"
    assert snippet.startswith("…") and snippet.endswith("…")
    assert len(snippet) == SENTENCE_LIMIT + 2


def test_truncate_around_at_the_end_of_text():
    text = "あ" * 600 + "食べ"
    snippet, start, end = truncate_around(text, 600, 602)
    assert snippet.startswith("…") and not snippet.endswith("…")
    assert snippet[start:end] == "食べ"


def test_short_sentence_is_untouched():
    assert truncate_around("パンを食べた", 3, 5) == ("パンを食べた", 3, 5)


def test_candidate_after_300_chars_is_still_bold_in_preview():
    text = "<&" * 200 + "食べた" + "。" * 50
    start = text.index("食べ")
    body = render_preview(content(sentence=text, start=start, end=start + 2), T)
    first = body.splitlines()[0]
    assert "<b>食べ</b>" in first
    assert first.count("<b>") == 1 and first.count("</b>") == 1
    # Escaping happens after cutting, so no entity is split.
    assert not re.search(r"&(?!lt;|gt;|amp;|quot;|#x27;)", first)


def test_shorten():
    assert shorten("abcdef", 4) == "abc…"
    assert shorten("abc", 4) == "abc"
    assert shorten("abc", 0) == ""


def test_near_limit_preview_leaves_room_for_hints_and_longest_status():
    huge = "x&" * 1500
    body = render_preview(
        content(
            sentence="<" * 1000 + "食べた" + ">" * 1000,
            start=1000,
            end=1002,
            sentence_tl="&" * 1000,
            senses=(huge, huge, huge),
            senses_tl=(huge, huge, huge),
            example=huge,
            example_tl=huge,
            translation_failed=True,
        ),
        T,
    )
    assert "<b>食べ</b>" in body
    assert body.splitlines()[-1] == T.hint_translation_failed
    assert tg_len(with_status(body, LONGEST_STATUS, T.hint_tts_failed)) <= MAX_MESSAGE


def test_example_is_shortened_before_meanings():
    long_example = "例" * 4000
    body = render_preview(content(senses=("to eat",), example=long_example), T)
    assert "1. to eat" in body
    assert tg_len(with_status(body, LONGEST_STATUS, T.hint_tts_failed)) <= MAX_MESSAGE


def test_untranslated_senses_get_no_placeholder():
    body = render_preview(
        content(senses=("to eat",), example="毎日パンを食べる。", translating=True, translates_senses=False),
        T,
    )
    assert "1. to eat" in body.splitlines()
    assert f"　{T.preview_translating}" in body.splitlines()
