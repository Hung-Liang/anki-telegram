import importlib.util
import sqlite3
from pathlib import Path

import pytest

from ankibot.examples import Examples
from ankibot.tokenizer import Tokenizer

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_tatoeba.py"
SENTENCES = [
    (1, "jpn", "私は毎日パンを食べます。"),
    (2, "jpn", "昨日は寿司を食べた。"),
    (3, "jpn", "目を閉じて夢見る。"),
    (4, "jpn", "毎晩テレビをよく見る。"),
    (5, "jpn", "木がある。"),
    (6, "jpn", "大きな木の下で本を読むのが好きです。"),
    (7, "eng", "I eat bread."),
]


def load_script():
    spec = importlib.util.spec_from_file_location("build_tatoeba", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("tatoeba")
    source = tmp / "jpn_sentences.tsv"
    source.write_text("".join(f"{i}\t{lang}\t{text}\n" for i, lang, text in SENTENCES), encoding="utf-8")
    out = tmp / "tatoeba.db"
    count = load_script().build(source.open(encoding="utf-8"), out, Tokenizer())
    assert count == 6
    return out


@pytest.fixture
def examples(db):
    ex = Examples.open(db)
    yield ex
    ex.close()


def test_conjugated_sentences_are_found_by_dictionary_form(examples):
    # Shortest sentence wins.
    assert examples.find(("たべる", "食べる")) == "昨日は寿司を食べた。"


def test_one_and_two_character_words(examples):
    assert examples.find(("き", "木")) == "木がある。"
    assert examples.find(("みる", "見る")) == "毎晩テレビをよく見る。"


def test_compound_containing_the_word_is_not_matched(examples):
    # The 夢見る sentence is shorter, but 見る must not match inside 夢見る.
    assert examples.find(("ゆめみる", "夢見る")) == "目を閉じて夢見る。"
    assert examples.find(("みる", "見る")) == "毎晩テレビをよく見る。"


def test_unknown_word_returns_none(examples):
    assert examples.find(("ぞう", "象")) is None


def test_missing_database_disables_examples(tmp_path):
    assert Examples.open(tmp_path / "missing.db") is None


def test_build_is_atomic_and_skips_other_languages(db):
    con = sqlite3.connect(db)
    try:
        texts = {row[0] for row in con.execute("select text from sentences")}
    finally:
        con.close()
    assert "I eat bread." not in texts
    assert not list(db.parent.glob("*.tmp"))


def test_build_replaces_existing_file(tmp_path):
    out = tmp_path / "tatoeba.db"
    out.write_bytes(b"old")
    load_script().build(iter(["1\tjpn\t猫がいる。\n"]), out, Tokenizer())
    ex = Examples.open(out)
    try:
        assert ex.find(("ねこ", "猫")) == "猫がいる。"
    finally:
        ex.close()
