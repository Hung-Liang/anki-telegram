import json

import pytest

from ankibot.importer import ImportItem, parse_words


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
