from types import SimpleNamespace

import pytest

from ankibot.dictionary import Dictionary


@pytest.fixture(scope="module")
def dictionary():
    return Dictionary()


def kanji(text, pri=()):
    return SimpleNamespace(text=text, pri=list(pri), info=[])


def kana(text, pri=(), restr=(), nokanji=0):
    return SimpleNamespace(text=text, pri=list(pri), restr=list(restr), nokanji=nokanji, info=[])


def sense(*glosses, stagk=(), stagr=(), misc=(), pos=("noun (common) (futsuumeishi)",)):
    return SimpleNamespace(
        gloss=[SimpleNamespace(text=g) for g in glosses],
        stagk=list(stagk),
        stagr=list(stagr),
        misc=list(misc),
        pos=list(pos),
    )


def entry(idseq, kanji_forms, kana_forms, senses):
    return SimpleNamespace(idseq=idseq, kanji_forms=kanji_forms, kana_forms=kana_forms, senses=senses)


class FakeJam:
    def __init__(self, *entries):
        self.entries = entries
        self.queries = []

    def lookup(self, query, **kwargs):
        self.queries.append(query)
        hits = [e for e in self.entries if query in [f.text for f in [*e.kanji_forms, *e.kana_forms]]]
        return SimpleNamespace(entries=hits)


def test_reading_match_wins(dictionary):
    jouzu = dictionary.lookup("上手", "じょうず")
    uwate = dictionary.lookup("上手", "うわて")
    assert (jouzu.word, jouzu.reading) == ("上手", "じょうず")
    assert (uwate.word, uwate.reading) == ("上手", "うわて")
    assert jouzu.senses[0].startswith("skillful")


def test_senses_restricted_to_reading(dictionary):
    uwate = dictionary.lookup("上手", "うわて")
    assert uwate.senses[0] == "upper part"
    assert not any("upper stream" in s or "stage" in s for s in uwate.senses)
    assert len(uwate.senses) <= 3


def test_common_entry_preferred_without_reading(dictionary):
    jam = FakeJam(
        entry(1, [kanji("御名")], [kana("みな")], [sense("name of God")]),
        entry(2, [kanji("皆", pri=["ichi1"])], [kana("みな", pri=["ichi1"])], [sense("everyone")]),
    )
    result = Dictionary(jam).lookup("みな", None)
    assert result.senses == ("everyone",)
    assert result.is_common


def test_jmdict_order_breaks_ties():
    jam = FakeJam(
        entry(1, [kanji("甲")], [kana("こう")], [sense("first")]),
        entry(2, [kanji("乙")], [kana("こう")], [sense("second")]),
    )
    assert Dictionary(jam).lookup("こう", "こう").senses == ("first",)


def test_not_found(dictionary):
    assert dictionary.lookup("ほげほげふが", None) is None
    assert not dictionary.exists("ほげほげふが")


def test_english_gloss_does_not_count_as_form(dictionary):
    assert not dictionary.exists("coffee")
    assert dictionary.lookup("Hello", None) is None


@pytest.mark.parametrize("query", ["食べ%", "食_る", "@", "id#1358280", ""])
def test_wildcard_and_id_queries_are_rejected(query):
    jam = FakeJam()
    assert not Dictionary(jam).exists(query)
    assert jam.queries == []


@pytest.mark.parametrize(
    ("inputs", "word"),
    [
        ([("食べる", "たべる"), ("たべる", "たべる"), ("食べる", None)], "食べる"),
        ([("宜しい", "よろしい"), ("よろしい", "よろしい")], "よろしい"),
    ],
)
def test_kana_and_kanji_input_give_same_word(dictionary, inputs, word):
    assert {dictionary.lookup(lemma, reading).word for lemma, reading in inputs} == {word}


def test_usually_kana_keeps_kanji_in_forms(dictionary):
    yoroshii = dictionary.lookup("宜しい", "よろしい")
    assert yoroshii.forms == ("よろしい", "宜しい")


def test_forms_only_include_kanji_compatible_with_reading(dictionary):
    hikkosu = dictionary.lookup("引っ越す", "ひっこす")
    hikikosu = dictionary.lookup("引越す", "ひきこす")
    assert (hikkosu.word, hikkosu.forms) == ("引っ越す", ("ひっこす", "引っ越す", "引っこす"))
    assert (hikikosu.word, hikikosu.forms) == ("引越す", ("ひきこす", "引越す", "引き越す"))


def test_restricted_reading_never_pairs_with_other_kanji(dictionary):
    mina = dictionary.lookup("皆", "みな")
    assert mina.word == "みな"
    assert mina.forms == ("みな", "皆")


def test_irregular_kanji_forms_are_not_used_for_examples(dictionary):
    desk = dictionary.lookup("机", "つくえ")
    assert desk.word == "机"
    assert "案" in desk.forms
    assert desk.example_forms == ("机",)


def test_example_forms_include_kana_only_for_kana_words(dictionary):
    assert dictionary.lookup("食べる", "たべる").example_forms == ("食べる",)
    assert dictionary.lookup("宜しい", "よろしい").example_forms == ("よろしい", "宜しい")


def test_reading_without_kanji(dictionary):
    karaoke = dictionary.lookup("カラオケ", "からおけ")
    assert (karaoke.word, karaoke.reading, karaoke.forms) == ("カラオケ", "カラオケ", ("カラオケ",))


def test_katakana_reading_is_preserved(dictionary):
    coffee = dictionary.lookup("コーヒー", "こーひー")
    assert (coffee.word, coffee.reading) == ("コーヒー", "コーヒー")
    assert coffee.forms == ("コーヒー", "珈琲")
    assert coffee.senses == ("coffee",)


def test_kana_variant_fallback(dictionary):
    assert dictionary.exists("こーひー")
    assert dictionary.lookup("こーひー", "こーひー").reading == "コーヒー"


def test_kanji_restricted_sense():
    jam = FakeJam(
        entry(
            1,
            [kanji("安い"), kanji("廉い")],
            [kana("やすい")],
            [sense("cheap"), sense("calm", stagk=["廉い"])],
        )
    )
    assert Dictionary(jam).lookup("廉い", "やすい").senses == ("cheap",)


def test_falls_back_to_all_senses_when_none_apply(caplog):
    jam = FakeJam(
        entry(1, [kanji("甲")], [kana("こう")], [sense("a", stagk=["乙"]), sense("b", stagr=["おつ"])])
    )
    result = Dictionary(jam).lookup("甲", "こう")
    assert result.senses == ("a", "b")
    assert "no sense" in caplog.text


def test_sense_text_joins_glosses_and_pos_is_collected():
    jam = FakeJam(
        entry(
            1,
            [kanji("甲")],
            [kana("こう")],
            [sense("x", "y", pos=["noun"]), sense("z", pos=["noun", "adverb"]), sense("w"), sense("v")],
        )
    )
    result = Dictionary(jam).lookup("甲", "こう")
    assert result.senses == ("x; y", "z", "w")
    assert result.pos == ("noun", "adverb", "noun (common) (futsuumeishi)")
