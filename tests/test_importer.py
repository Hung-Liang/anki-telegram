import json
from pathlib import Path
from types import MappingProxyType

import pytest

from ankibot.anki_service import AddResult, AddStatus, KnownWords, dedup_key
from ankibot.importer import Importer, ImportItem, ItemResult, ItemStatus, parse_words
from ankibot.pipeline import Lookup, Pipeline
from ankibot.threads import DedicatedThread


def test_parses_all_valid_forms():
    raw = json.dumps(
        [
            "食べる",
            {"word": "猫"},
            {"word": "猫", "translation": "貓"},
            {"word": "走る", "translation": ["跑", "行駛"], "example": "毎朝公園を走る。"},
            {"word": "走る", "example": "走る。", "example_translation": "跑。"},
        ]
    )
    assert parse_words(raw) == [
        ImportItem("食べる"),
        ImportItem("猫"),
        ImportItem("猫", ("貓",)),
        ImportItem("走る", ("跑", "行駛"), "毎朝公園を走る。"),
        ImportItem("走る", None, "走る。", "跑。"),
    ]


def test_empty_array_is_valid():
    assert parse_words("[]") == []


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not json", "invalid JSON"),
        ('{"word": "猫"}', "top level must be an array"),
        ('"猫"', "top level must be an array"),
    ],
)
def test_rejects_bad_document(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_words(raw)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ("", "word must be a non-empty string"),
        ("  ", "word must be a non-empty string"),
        (3, "must be a string or an object"),
        (None, "must be a string or an object"),
        ({}, "missing word"),
        ({"word": ""}, "word must be a non-empty string"),
        ({"word": 1}, "word must be a non-empty string"),
        ({"word": "猫", "translation": ""}, "translation must be a non-empty string"),
        ({"word": "猫", "translation": 1}, "translation must be a string or a non-empty array"),
        ({"word": "猫", "translation": None}, "translation must be a string or a non-empty array"),
        ({"word": "猫", "translation": []}, "translation must be a string or a non-empty array"),
        ({"word": "猫", "translation": ["貓", 1]}, "translation must be a non-empty string"),
        ({"word": "猫", "translation": ["貓", ""]}, "translation must be a non-empty string"),
        ({"word": "猫", "example": ""}, "example must be a non-empty string"),
        ({"word": "猫", "example": ["猫だ。"]}, "example must be a non-empty string"),
        ({"word": "猫", "example": "猫だ。", "example_translation": ""}, "example_translation must be"),
        ({"word": "猫", "example": "猫だ。", "example_translation": 1}, "example_translation must be"),
        ({"word": "猫", "example_translation": "是貓。"}, "example_translation requires example"),
        ({"word": "猫", "reading": "ねこ"}, "unknown keys: reading"),
    ],
)
def test_rejects_bad_item_with_position(entry, message):
    raw = json.dumps(["食べる", "走る", entry])
    with pytest.raises(ValueError, match=rf"^item 3: {message}"):
        parse_words(raw)


class FakeExamples:
    def __init__(self, sentences):
        self.sentences = sentences

    def find(self, forms):
        return next((self.sentences[f] for f in forms if f in self.sentences), None)

    def close(self):
        pass


class FakeTranslator:
    translates_glosses = True

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def translate(self, texts, source_lang, context=None):
        self.calls.append((list(texts), source_lang, context))
        return None if self.fail else [f"tl:{t}" for t in texts]


class FakeSync:
    def __init__(self, status=AddStatus.ADDED):
        self.status = status
        self.cards = []

    async def add_note(self, card):
        self.cards.append(card)
        return AddResult(self.status, 1 if self.status is AddStatus.ADDED else None)


class FakeTTS:
    def __init__(self, fail=False):
        self.fail = fail
        self.texts = []

    async def synth(self, text):
        self.texts.append(text)
        if self.fail:
            raise RuntimeError("tts down")
        return Path(f"/audio/{text}.mp3")


@pytest.fixture
async def make_importer():
    thread = DedicatedThread("import-test")
    lookup = await thread.run(Lookup.create, False)
    lookup.examples = FakeExamples({"食べる": "毎日パンを食べる。"})

    async def fresh():
        pass

    def make(known=(), translator=None, sync=None, tts=None, sleeps=None):
        words = KnownWords(MappingProxyType({dedup_key(w, r): 1 for w, r in known}), ())
        translator = translator or FakeTranslator()
        pipeline = Pipeline(thread, lookup, lambda: words, fresh, translator=translator)

        async def sleep(delay):
            if sleeps is not None:
                sleeps.append(delay)

        return Importer(pipeline, translator, sync or FakeSync(), tts or FakeTTS(), True, sleep=sleep)

    yield make
    await thread.run(lookup.close)
    thread.shutdown()


async def test_given_translation_skips_gloss_translation(make_importer):
    translator, sync = FakeTranslator(), FakeSync()
    importer = make_importer(translator=translator, sync=sync)
    result = await importer.import_word(ImportItem("食べる", ("吃", "<食>")))
    assert result == ItemResult("食べる", ItemStatus.ADDED)
    assert [c[1] for c in translator.calls] == ["JA"]
    assert sync.cards[0].meaning_tl == "吃<br>&lt;食&gt;"


async def test_given_example_replaces_tatoeba(make_importer):
    translator, sync, tts = FakeTranslator(), FakeSync(), FakeTTS()
    importer = make_importer(translator=translator, sync=sync, tts=tts)
    await importer.import_word(ImportItem("食べる", example="<肉>を食べる。", example_translation="吃肉。"))
    card = sync.cards[0]
    assert card.example_jp == "&lt;肉&gt;を食べる。" and card.example_tl == "吃肉。"
    assert card.tts_sentence == "<肉>を食べる。" and tts.texts == ["たべる", "<肉>を食べる。"]
    assert [c[1] for c in translator.calls] == ["EN"]

    await importer.import_word(ImportItem("食べる", example="肉を食べる。"))
    assert translator.calls[-1][:2] == (["肉を食べる。"], "JA")
    assert sync.cards[1].example_tl == "tl:肉を食べる。"


async def test_without_example_uses_tatoeba(make_importer):
    sync = FakeSync()
    await make_importer(sync=sync).import_word(ImportItem("食べる"))
    card = sync.cards[0]
    assert card.example_jp == "毎日パンを食べる。" and card.example_tl == "tl:毎日パンを食べる。"
    assert card.meaning_tl.startswith("tl:")


async def test_translation_failure_retries_then_skips(make_importer):
    translator, sync, sleeps = FakeTranslator(fail=True), FakeSync(), []
    importer = make_importer(translator=translator, sync=sync, sleeps=sleeps)
    result = await importer.import_word(ImportItem("食べる"))
    assert result.status is ItemStatus.TRANSLATION_FAILED
    assert len(translator.calls) == 4 and sleeps == [5.0, 15.0, 30.0]
    assert sync.cards == []


@pytest.mark.parametrize(
    ("word", "known", "status"),
    [
        ("猫が魚を食べた。", (), ItemStatus.NOT_SINGLE_WORD),
        ("猫", (("猫", "ねこ"),), ItemStatus.EXISTS),
        ("ﾌｶﾞﾎｹﾞ", (), ItemStatus.NOT_FOUND),
    ],
)
async def test_unsavable_words(make_importer, word, known, status):
    sync = FakeSync()
    assert (await make_importer(known=known, sync=sync).import_word(ImportItem(word))).status is status
    assert sync.cards == []


async def test_tts_failure_still_saves(make_importer):
    sync = FakeSync()
    result = await make_importer(sync=sync, tts=FakeTTS(fail=True)).import_word(ImportItem("食べる"))
    assert result.status is ItemStatus.ADDED
    assert sync.cards[0].audio == "" and sync.cards[0].sentence_audio == ""


async def test_blocked(make_importer):
    result = await make_importer(sync=FakeSync(AddStatus.BLOCKED)).import_word(ImportItem("食べる"))
    assert result.status is ItemStatus.BLOCKED
