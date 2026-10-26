"""The pinned runtime dependencies install and work on Python 3.12."""

import sys

import pytest


def test_python_version():
    assert sys.version_info[:2] == (3, 12)


def test_fugashi_unidic_lite_tokenizes():
    import fugashi

    tagger = fugashi.Tagger()
    tokens = [(w.surface, w.feature.orthBase, w.feature.kanaBase) for w in tagger("食べている")]
    assert tokens[0] == ("食べ", "食べる", "タベル")


def test_jamdict_offline_lookup():
    from jamdict import Jamdict

    result = Jamdict().lookup("食べる")
    assert result.entries
    assert any(k.text == "食べる" for k in result.entries[0].kanji_forms)
    assert any(r.text == "たべる" for r in result.entries[0].kana_forms)


def test_anki_pinned_version():
    from anki import buildinfo

    assert buildinfo.version == "26.09.3"


def test_anki_collection_roundtrip(tmp_path):
    from anki.collection import Collection

    col = Collection(str(tmp_path / "collection.anki2"))
    try:
        basic = col.models.by_name("Basic")
        note = col.new_note(basic)
        note["Front"] = "食べる"
        col.add_note(note, col.decks.id("Default"))
        assert col.find_notes('"Front:*食べる*"') == [note.id]
    finally:
        col.close()


@pytest.mark.parametrize(
    "module",
    ["telegram", "edge_tts", "pydantic_settings", "httpx"],
)
def test_runtime_imports(module):
    __import__(module)
