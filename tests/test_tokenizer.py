import pytest

from ankibot.tokenizer import Token, Tokenizer, bold_span, build_candidates, to_hiragana


@pytest.fixture(scope="module")
def tokenizer():
    return Tokenizer()


def known(*forms):
    allowed = set(forms)
    return lambda form: form in allowed


def token(
    surface, pos1="名詞", pos2="普通名詞", c_form=None, start=0, orth=None, lemma=None, kana=None, base=None
):
    return Token(
        surface=surface,
        pos1=pos1,
        pos2=pos2,
        c_form=c_form,
        start=start,
        end=start + len(surface),
        orth_base=orth,
        lemma=lemma,
        kana=kana,
        kana_base=base,
    )


def test_to_hiragana():
    assert to_hiragana("コーヒー") == "こーひー"
    assert to_hiragana("ヴァイオリン") == "ゔぁいおりん"
    assert to_hiragana("ヵヶ") == "ゕゖ"
    # U+30F7 and beyond are outside the shifted range.
    assert to_hiragana("ヷ・ー") == "ヷ・ー"
    assert to_hiragana("食べる abc") == "食べる abc"


def test_tokenize_keeps_particles_and_symbols(tokenizer):
    tokens = tokenizer.tokenize("本を読む。")
    assert [(t.surface, t.pos1) for t in tokens] == [
        ("本", "名詞"),
        ("を", "助詞"),
        ("読む", "動詞"),
        ("。", "補助記号"),
    ]


def test_tokenize_positions_with_whitespace(tokenizer):
    text = "　食べる\n飲む Hello"
    tokens = tokenizer.tokenize(text)
    for t in tokens:
        assert text[t.start : t.end] == t.surface
    assert [t.surface for t in tokens if t.pos1 != "空白"] == ["食べる", "飲む", "Hello"]


def test_tokenize_normalizes_attributes(tokenizer):
    coffee, _ = tokenizer.tokenize("コーヒー。")
    assert coffee.lemma == "コーヒー"
    assert coffee.orth_base == "コーヒー"
    eat = tokenizer.tokenize("食べた")[0]
    assert (eat.orth_base, eat.kana, eat.kana_base, eat.c_form) == ("食べる", "タベ", "タベル", "連用形-一般")


def test_tokenize_unknown_word_attributes_are_none(tokenizer):
    hello = tokenizer.tokenize("Hello")[0]
    assert (hello.orth_base, hello.lemma, hello.kana, hello.kana_base) == (None, None, None, None)


CASES = [
    # (text, forms known to the dictionary, [(lemma, reading, surface, start, end)], is_sentence)
    ("食べている", {"食べる", "居る"}, [("食べる", "たべる", "食べ", 0, 2)], True),
    ("高くなった", {"高い", "成る", "なる"}, [("高い", "たかい", "高く", 0, 2)], False),
    ("食べた", {"食べる"}, [("食べる", "たべる", "食べ", 0, 2)], False),
    ("食べる。", {"食べる"}, [("食べる", "たべる", "食べる", 0, 3)], True),
    (
        "日本語を勉強する",
        {"日本", "語", "日本語", "勉強"},
        [("日本語", "にっぽんご", "日本語", 0, 3), ("勉強", "べんきょう", "勉強", 4, 6)],
        True,
    ),
    ("勉強しました", {"勉強"}, [("勉強", "べんきょう", "勉強", 0, 2)], False),
    ("愛している", {"愛する"}, [("愛する", "あいする", "愛し", 0, 2)], True),
    (
        "見つけ出した",
        {"見つける", "出す", "見つけ出す"},
        [("見つけ出す", "みつけだす", "見つけ出し", 0, 5)],
        False,
    ),
    (
        "読み始めた",
        {"読む", "始める", "読み始める", "読み始め"},
        [("読み始める", "よみはじめる", "読み始め", 0, 4)],
        False,
    ),
    (
        "読み始めは",
        {"読む", "始める", "読み始める", "読み始め"},
        [("読み始め", "よみはじめ", "読み始め", 0, 4)],
        True,
    ),
    ("引き出した", {"引き出す", "引き出し"}, [("引き出す", "ひきだす", "引き出し", 0, 4)], False),
    (
        "引き出しを開ける",
        {"引き出す", "引き出し", "開ける"},
        [("引き出し", "ひきだし", "引き出し", 0, 4), ("開ける", "あける", "開ける", 5, 8)],
        True,
    ),
    ("上手ですね", {"上手"}, [("上手", "じょうず", "上手", 0, 2)], True),
    (
        "コーヒーを飲む",
        {"コーヒー", "飲む"},
        [("コーヒー", "こーひー", "コーヒー", 0, 4), ("飲む", "のむ", "飲む", 5, 7)],
        True,
    ),
    (
        "気持ちいい",
        {"気持ち", "いい", "気持ちいい"},
        [("気持ちいい", "きもちいい", "気持ちいい", 0, 5)],
        False,
    ),
    ("よろしく", {"よろしく"}, [("よろしく", "よろしく", "よろしく", 0, 4)], False),
    ("ググった", {"ググる"}, [("ググる", "ぐぐる", "ググっ", 0, 3)], False),
    ("東京へ行く", {"東京", "行く"}, [("行く", "いく", "行く", 3, 5)], True),
    ("3つ買った", {"買う", "つ"}, [("買う", "かう", "買っ", 2, 4)], False),
    ("本がある", {"本", "ある"}, [("本", "ほん", "本", 0, 1)], True),
    ("皆が来た", {"皆", "来る"}, [("皆", "みな", "皆", 0, 1), ("来る", "くる", "来", 2, 3)], True),
    (
        "食べて、また食べた",
        {"食べる", "また"},
        [("食べる", "たべる", "食べ", 0, 2), ("また", "また", "また", 4, 6)],
        True,
    ),
    ("Hello 世界", {"世界"}, [("世界", "せかい", "世界", 6, 8)], False),
    ("a<b>&c 猫が好き", {"猫", "好き"}, [("猫", "ねこ", "猫", 7, 8), ("好き", "すき", "好き", 9, 11)], True),
    ("は", set(), [], True),
]


@pytest.mark.parametrize(("text", "forms", "expected", "is_sentence"), CASES, ids=[c[0] for c in CASES])
def test_build_candidates(tokenizer, text, forms, expected, is_sentence):
    result = build_candidates(tokenizer.tokenize(text), known(*forms))
    assert [(c.lemma, c.reading, c.surface, c.start, c.end) for c in result.candidates] == expected
    assert result.is_sentence is is_sentence
    for c in result.candidates:
        assert text[c.start : c.end] == c.surface


def test_sahen_noun_merges_only_when_dictionary_has_it(tokenizer):
    result = build_candidates(tokenizer.tokenize("勉強しました"), known("勉強", "勉強する"))
    assert [(c.lemma, c.surface) for c in result.candidates] == [("勉強する", "勉強し")]


def test_particle_blocks_compound(tokenizer):
    result = build_candidates(tokenizer.tokenize("日本語を勉強する"), known("日本語", "勉強", "日本語を勉強"))
    assert [c.lemma for c in result.candidates] == ["日本語", "勉強"]


def test_proper_nouns_opt_in(tokenizer):
    tokens = tokenizer.tokenize("東京へ行く")
    result = build_candidates(tokens, known("東京", "行く"), include_proper_nouns=True)
    assert [c.lemma for c in result.candidates] == ["東京", "行く"]


def test_unfound_word_falls_back_to_first_form(tokenizer):
    result = build_candidates(tokenizer.tokenize("食べた"), known())
    assert [(c.lemma, c.reading) for c in result.candidates] == [("食べる", "たべる")]


def test_lemma_used_when_orth_base_not_in_dictionary():
    tokens = [
        token(
            "見付け",
            pos1="動詞",
            c_form="連用形-一般",
            orth="見付ける",
            lemma="見つける",
            kana="ミツケ",
            base="ミツケル",
        )
    ]
    result = build_candidates(tokens, known("見つける"))
    assert [(c.lemma, c.reading) for c in result.candidates] == [("見つける", "みつける")]


def test_kana_orth_base_keeps_kanji_lemma_as_hint():
    tokens = [token("こと", orth="こと", lemma="事", kana="コト", base="コト")]
    result = build_candidates(tokens, known("こと", "事"))
    assert [(c.lemma, c.reading, c.kanji_lemma) for c in result.candidates] == [("こと", "こと", "事")]


def test_no_kanji_lemma_hint_when_unknown_or_already_kanji():
    kana = [token("こと", orth="こと", lemma="事", kana="コト", base="コト")]
    assert build_candidates(kana, known("こと")).candidates[0].kanji_lemma is None
    kanji = [token("机", orth="机", lemma="机", kana="ツクエ", base="ツクエ")]
    assert build_candidates(kanji, known("机")).candidates[0].kanji_lemma is None


def test_unknown_kanji_word_has_no_reading():
    tokens = [token("鬱鬱")]
    result = build_candidates(tokens, known())
    assert [(c.lemma, c.reading) for c in result.candidates] == [("鬱鬱", None)]


def test_unknown_kana_word_reads_its_surface():
    tokens = [token("ヤバみ")]
    result = build_candidates(tokens, known("ヤバみ"))
    assert [(c.lemma, c.reading) for c in result.candidates] == [("ヤバみ", "やばみ")]


def test_compound_with_unknown_reading_piece():
    tokens = [
        token("鬱鬱", start=0),
        token("出し", pos1="接尾辞", pos2="名詞的", start=2, orth="出し", kana="ダシ", base="ダシ"),
    ]
    result = build_candidates(tokens, known("鬱鬱出し"))
    assert [(c.lemma, c.reading, c.start, c.end) for c in result.candidates] == [("鬱鬱出し", None, 0, 4)]


def test_compound_at_sentence_end_prefers_surface():
    tokens = [
        token("見", pos1="動詞", c_form="連用形-一般", start=0, orth="見る", kana="ミ", base="ミル"),
        token("出し", pos1="動詞", c_form="連用形-一般", start=1, orth="出す", kana="ダシ", base="ダス"),
        token("。", pos1="補助記号", pos2="句点", start=3),
    ]
    result = build_candidates(tokens, known("見出し", "見出す"))
    assert [(c.lemma, c.reading) for c in result.candidates] == [("見出し", "みだし")]


def test_compound_window_is_at_most_four_tokens():
    tokens = [token(c, start=i, kana=c, base=c) for i, c in enumerate("アイウエオ")]
    result = build_candidates(tokens, known("アイウエオ", "イウエオ"))
    assert [c.lemma for c in result.candidates] == ["ア", "イウエオ"]


def test_bold_span_escapes_each_part():
    text = "a<b&食べた>"
    assert bold_span(text, 4, 6) == "a&lt;b&amp;<b>食べ</b>た&gt;"
