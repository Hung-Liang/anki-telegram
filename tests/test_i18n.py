import pytest

from ankibot.i18n import DEFAULT_LANGUAGE, TextsError, available_languages, load_texts
from ankibot.messages import with_status


def test_builtin_languages():
    assert {"zh-Hant", "en"} <= set(available_languages())
    assert DEFAULT_LANGUAGE in available_languages()


@pytest.mark.parametrize("language", available_languages())
def test_every_locale_is_complete_and_formats(language):
    t = load_texts(language)
    assert t.reply_existing.format(words="猫")
    assert t.sync_failed.format(time="t", error="e")
    assert all(t.statuses)


def test_english_texts():
    t = load_texts("en")
    assert t.status_saved == "✅ Saved"
    assert t.reply_not_found.format(words="a, b") == "Not found: a, b"


def test_unknown_language():
    with pytest.raises(TextsError, match="available"):
        load_texts("xx")


def test_override_replaces_only_its_keys(tmp_path):
    path = tmp_path / "messages.toml"
    path.write_text('[status]\nsaved = "✅ <saved> & done"\n', encoding="utf-8")
    t = load_texts("en", path)
    assert t.status_saved == "✅ <saved> & done"
    assert t.status_failed == load_texts("en").status_failed
    assert with_status("<b>x</b>", t.status_saved) == "<b>x</b>\n✅ &lt;saved&gt; &amp; done"


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ('[status]\nsaveed = "x"\n', "unknown keys"),
        ('[reply]\nexisting = "found: {word}"\n', "placeholders"),
        ('[reply]\nexisting = "found: {words"\n', "reply.existing"),
        ("[status]\nsaved = 1\n", "must be a string"),
        ('saved = "x"\n', "section"),
        ("[status\n", "cannot read"),
    ],
)
def test_invalid_override(tmp_path, content, match):
    path = tmp_path / "messages.toml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(TextsError, match=match):
        load_texts(override=path)
