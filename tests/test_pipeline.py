from types import MappingProxyType

import pytest

from ankibot.anki_service import KnownWords, dedup_key
from ankibot.pipeline import Lookup, Pipeline
from ankibot.threads import DedicatedThread


def known(*pairs):
    return KnownWords(MappingProxyType({dedup_key(w, r): 1 for w, r in pairs}), ())


class FakeExamples:
    def __init__(self, sentences):
        self.sentences = sentences
        self.queries = []

    def find(self, forms):
        self.queries.append(forms)
        return next((self.sentences[f] for f in forms if f in self.sentences), None)

    def close(self):
        pass


class Fresh:
    def __init__(self):
        self.calls = 0

    async def __call__(self):
        self.calls += 1


@pytest.fixture
async def make_pipeline():
    thread = DedicatedThread("lookup-test")
    lookup = await thread.run(Lookup.create, False)

    def make(words=None, examples=None, translator=None):
        words = words if words is not None else known()
        if examples is not None:
            lookup.examples = examples
        fresh = Fresh()
        pipeline = Pipeline(thread, lookup, lambda: words, fresh, translator=translator)
        pipeline.fresh = fresh
        return pipeline

    yield make
    await thread.run(lookup.close)
    thread.shutdown()


async def test_single_word_input(make_pipeline):
    pipeline = make_pipeline()
    result = await pipeline.process("食べた")
    assert pipeline.fresh.calls == 1
    assert [d.card.word for d in result.drafts] == ["食べる"]
    draft = result.drafts[0]
    assert draft.content.sentence is None
    assert draft.card.sentence == "" and draft.card.source == "食べた"
    assert draft.card.reading == "たべる"
    assert draft.card.meaning.split("<br>")[0] == draft.content.senses[0]
    assert draft.card.forms[:2] == ("たべる", "食べる")


async def test_sentence_keeps_order_and_bolds_position(make_pipeline):
    result = await make_pipeline().process("猫が魚を食べた。")
    assert [d.card.word for d in result.drafts] == ["猫", "魚", "食べる"]
    eat = result.drafts[2]
    assert eat.content.sentence == "猫が魚を食べた。"
    assert eat.card.sentence == "猫が魚を<b>食べ</b>た。"
    assert eat.card.source == "猫が魚を食べた。"


async def test_known_words_are_split_out(make_pipeline):
    result = await make_pipeline(known(("猫", "ねこ"))).process("猫が魚を食べた。")
    assert [d.card.word for d in result.drafts] == ["魚", "食べる"]
    assert result.existing == ("猫",)


async def test_katakana_reading_matches_known_cache(make_pipeline):
    result = await make_pipeline(known(("コーヒー", "コーヒー"))).process("コーヒー")
    assert result.drafts == () and result.existing == ("コーヒー",)


async def test_not_found_is_reported(make_pipeline):
    result = await make_pipeline().process("ﾌｶﾞﾎｹﾞが来た。")
    assert [d.card.word for d in result.drafts] == ["来る"]
    assert result.not_found == ("ﾌｶﾞﾎｹﾞ",)


async def test_user_input_is_escaped_in_card(make_pipeline):
    result = await make_pipeline().process("<b>猫</b>&犬")
    words = [d.card.word for d in result.drafts]
    assert "猫" in words
    cat = result.drafts[words.index("猫")]
    assert cat.card.source == "&lt;b&gt;猫&lt;/b&gt;&amp;犬"
    assert "<b>猫</b>" in cat.card.sentence and "&lt;b&gt;" in cat.card.sentence


async def test_examples_use_regular_kanji_forms(make_pipeline):
    examples = FakeExamples({"食べる": "毎日パンを食べる。"})
    result = await make_pipeline(examples=examples).process("たべる")
    assert result.drafts[0].card.example_jp == "毎日パンを食べる。"
    assert result.drafts[0].content.example == "毎日パンを食べる。"
    assert examples.queries[0] == ("食べる",)


async def test_kanji_lemma_disambiguates_kana_homophones(make_pipeline):
    result = await make_pipeline().process("寝ることにした。")
    koto = next(d for d in result.drafts if d.card.reading == "こと")
    assert "事" in koto.card.forms and "琴" not in koto.card.forms


async def test_kanji_lemma_with_other_reading_falls_back_to_kana(make_pipeline):
    result = await make_pipeline().process("いい")
    assert [(d.card.word, d.card.reading) for d in result.drafts] == [("いい", "いい")]


async def test_same_entry_through_different_spellings_is_deduplicated(make_pipeline):
    result = await make_pipeline().process("食べる、たべる。")
    assert [d.card.word for d in result.drafts] == ["食べる"]


class EchoTranslator:
    def __init__(self, translates_glosses=True):
        self.translates_glosses = translates_glosses
        self.sources = []

    async def translate(self, texts, source_lang, context=None):
        self.sources.append(source_lang)
        return [f"中:{t}" for t in texts]


async def test_without_translator_drafts_are_final(make_pipeline):
    pipeline = make_pipeline()
    result = await pipeline.process("食べた")
    assert not result.translating
    assert [d async for d in pipeline.translate(result)] == []


async def test_process_returns_placeholders_and_translate_yields_final_drafts(make_pipeline):
    pipeline = make_pipeline(translator=EchoTranslator())
    result = await pipeline.process("猫が魚を食べた。")
    assert result.translating
    assert all(d.content.translating and d.card.meaning_tl == "" for d in result.drafts)
    translated = [d async for d in pipeline.translate(result)]
    assert [d.card.word for d in translated] == ["猫", "魚", "食べる"]
    for d in translated:
        assert not d.content.translating and not d.content.translation_failed
        assert d.content.sentence_tl == "中:猫が魚を食べた。"
        assert d.card.meaning_tl.startswith("中:")


async def test_untranslated_drafts_are_marked_failed(make_pipeline):
    pipeline = make_pipeline(translator=EchoTranslator())
    result = await pipeline.process("食べた")
    [draft] = pipeline.untranslated(result)
    assert draft.content.translation_failed and not draft.content.translating
    assert draft.content.senses_tl is None


async def test_english_target_keeps_glosses_and_translates_the_sentence(make_pipeline):
    translator = EchoTranslator(translates_glosses=False)
    pipeline = make_pipeline(translator=translator)
    result = await pipeline.process("猫が魚を食べた。")
    assert result.translating
    assert not any(d.content.translates_senses for d in result.drafts)
    translated = [d async for d in pipeline.translate(result)]
    assert "EN" not in translator.sources
    for d in translated:
        assert d.content.senses_tl is None and d.card.meaning_tl == ""
        assert d.content.sentence_tl == "中:猫が魚を食べた。"
        assert not d.content.translation_failed


async def test_english_target_single_word_without_example_needs_no_translation(make_pipeline):
    translator = EchoTranslator(translates_glosses=False)
    pipeline = make_pipeline(translator=translator, examples=FakeExamples({}))
    result = await pipeline.process("食べた")
    assert not result.translating
    assert [d async for d in pipeline.translate(result)] == []
    assert translator.sources == []
