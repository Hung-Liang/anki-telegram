import asyncio
from concurrent.futures import ThreadPoolExecutor

import pytest
from anki.collection import Collection
from anki_helpers import (
    DECK,
    NOTETYPE,
    PLAIN,
    YOMITAN,
    add_raw_note,
    card,
    make_collection,
    settings,
)

from ankibot.anki_service import (
    REQUIRED_FIELDS,
    AddStatus,
    AnkiConfigError,
    AnkiService,
    DataLockError,
    DeleteStatus,
    data_lock,
    dedup_key,
    escape_search,
    parse_extra_sources,
    strip_html,
)

YOMITAN_SOURCE = f"{YOMITAN}:Expression:Reading"
PLAIN_SOURCE = f"{PLAIN}:Front"


@pytest.fixture
def open_service(tmp_path):
    services = []

    def _open(extra="", blocked=None, **schema):
        if not (tmp_path / "collection.anki2").exists():
            make_collection(tmp_path, **schema)
        flag = blocked if blocked is not None else (lambda: False)
        service = AnkiService.open(settings(tmp_path, extra), full_sync_required=flag)
        services.append(service)
        return service

    yield _open
    for service in services:
        service.close()


def bot_notes(service):
    col = service.collection
    return [col.get_note(nid) for nid in col.find_notes(f'"note:{NOTETYPE}"')]


# --- normalization ------------------------------------------------------------


def test_strip_html_removes_tags_then_unescapes():
    assert strip_html("<b>食べる</b>") == "食べる"
    assert strip_html("A&amp;B") == "A&B"
    assert strip_html("&lt;b&gt;") == "<b>"
    assert strip_html("x&nbsp;") == "x "


def test_dedup_key_normalizes_html_whitespace_and_katakana():
    assert dedup_key("<b>食べる</b>&nbsp;", " タベル ") == ("食べる", "たべる")
    assert dedup_key("コーヒー", "コーヒー") == ("コーヒー", "こーひー")


def test_escape_search():
    assert escape_search('a*b_c"d:e\\f') == 'a\\*b\\_c\\"d\\:e\\\\f'


def test_parse_extra_sources():
    specs = parse_extra_sources(f"{YOMITAN_SOURCE}; {PLAIN_SOURCE};")
    assert [(s.notetype, s.word_field, s.reading_field) for s in specs] == [
        (YOMITAN, "Expression", "Reading"),
        (PLAIN, "Front", None),
    ]
    assert parse_extra_sources("") == ()


@pytest.mark.parametrize("raw", ["OnlyName", "A:B:C:D", "A::B"])
def test_parse_extra_sources_rejects_bad_format(raw):
    with pytest.raises(AnkiConfigError, match="must not contain ':' or ';'"):
        parse_extra_sources(raw)


# --- startup validation ---------------------------------------------------------


def test_missing_collection_is_an_error_and_not_created(tmp_path):
    with pytest.raises(AnkiConfigError, match="bootstrap"):
        AnkiService.open(settings(tmp_path))
    assert not (tmp_path / "collection.anki2").exists()


def test_missing_notetype(tmp_path):
    make_collection(tmp_path)
    s = settings(tmp_path).model_copy(update={"anki_notetype": "Nope"})
    with pytest.raises(AnkiConfigError, match="Note type 'Nope' not found"):
        AnkiService.open(s)


def test_missing_required_field(tmp_path):
    make_collection(tmp_path, bot_fields=tuple(f for f in REQUIRED_FIELDS if f != "MeaningTL"))
    with pytest.raises(AnkiConfigError, match="missing required fields: MeaningTL"):
        AnkiService.open(settings(tmp_path))


def test_missing_deck_is_an_error_and_not_created(tmp_path):
    make_collection(tmp_path, deck=False)
    with pytest.raises(AnkiConfigError, match="Deck 'Japanese::Bot' not found"):
        AnkiService.open(settings(tmp_path))
    col = Collection(str(tmp_path / "collection.anki2"))
    try:
        assert col.decks.id_for_name(DECK) is None
    finally:
        col.close()


def test_extra_user_fields_and_other_order_are_allowed(open_service):
    service = open_service(bot_fields=("Extra", *reversed(REQUIRED_FIELDS), "Notes"))
    assert service.add_note_if_absent(card("食べる", "たべる")).status is AddStatus.ADDED


@pytest.mark.parametrize(
    "extra, message",
    [
        ("Missing Type:Expression", "note type 'Missing Type' not found"),
        (f"{YOMITAN}:Word", "field 'Word' not found"),
        (f"{YOMITAN}:Expression:Kana", "field 'Kana' not found"),
    ],
)
def test_extra_source_names_must_exist(tmp_path, extra, message):
    make_collection(tmp_path)
    with pytest.raises(AnkiConfigError, match=message) as exc:
        AnkiService.open(settings(tmp_path, extra))
    assert "must not contain ':' or ';'" in str(exc.value)


# --- add / dedup ----------------------------------------------------------------


def test_add_writes_fields_into_deck(open_service):
    service = open_service()
    result = service.add_note_if_absent(card("食べる", "たべる"))
    assert result.status is AddStatus.ADDED
    note = service.collection.get_note(result.note_id)
    assert note["Word"] == "食べる"
    assert note["Reading"] == "たべる"
    assert note["MeaningTL"] == "吃"
    assert note["Audio"] == ""
    assert note["Source"] == "食べる"
    assert {c.did for c in note.cards()} == {service.collection.decks.id_for_name(DECK)}
    assert service.known_words().contains("食べる", "たべる")


def test_add_same_word_twice_only_adds_once(open_service):
    service = open_service()
    assert service.add_note_if_absent(card("食べる", "たべる")).status is AddStatus.ADDED
    assert service.add_note_if_absent(card("食べる", "たべる")).status is AddStatus.EXISTS
    assert len(bot_notes(service)) == 1


async def test_concurrent_saves_on_anki_thread_add_one_note(open_service):
    service = open_service()
    loop = asyncio.get_running_loop()
    save = card("見る", "みる")
    with ThreadPoolExecutor(max_workers=1) as executor:
        results = await asyncio.gather(
            *(loop.run_in_executor(executor, service.add_note_if_absent, save) for _ in range(2))
        )
    assert sorted(r.status for r in results) == [AddStatus.ADDED, AddStatus.EXISTS]
    assert len(bot_notes(service)) == 1


def test_homographs_are_saved_separately(open_service):
    service = open_service()
    assert service.add_note_if_absent(card("辛い", "からい")).status is AddStatus.ADDED
    assert service.add_note_if_absent(card("辛い", "つらい")).status is AddStatus.ADDED
    assert len(bot_notes(service)) == 2


def test_dedup_does_not_rely_on_cache(open_service):
    service = open_service()
    add_raw_note(service.collection, NOTETYPE, Word="食べる", Reading="たべる")
    assert not service.known_words().contains("食べる", "たべる")
    assert service.add_note_if_absent(card("食べる", "たべる")).status is AddStatus.EXISTS


@pytest.mark.parametrize(
    "stored_word, stored_reading, word",
    [
        ("<b>食べる</b>", "たべる", "食べる"),
        ("食べる&nbsp;", "たべる", "食べる"),
        ("A&amp;B", "えーびー", "A&B"),
        ("a&lt;b", "えー", "a<b"),
        ("見る", "<span>みる</span>", "見る"),
    ],
)
def test_html_and_entities_in_fields_still_match(open_service, stored_word, stored_reading, word):
    service = open_service()
    add_raw_note(service.collection, NOTETYPE, Word=stored_word, Reading=stored_reading)
    reading = strip_html(stored_reading)
    assert service.add_note_if_absent(card(word, reading)).status is AddStatus.EXISTS
    assert service.rebuild_known_words().contains(word, reading)


@pytest.mark.parametrize("word", ["a*b", "a_b", 'a"b', "a:b", "a\\b", "(ab)", "-ab"])
def test_search_special_characters(open_service, word):
    service = open_service()
    add_raw_note(service.collection, NOTETYPE, Word="aXb", Reading="r")
    add_raw_note(service.collection, NOTETYPE, Word="aXXb", Reading="r")
    assert service.add_note_if_absent(card(word, "r")).status is AddStatus.ADDED
    assert service.add_note_if_absent(card(word, "r")).status is AddStatus.EXISTS


def test_loanword_katakana_reading_hits_cache_and_undo_removes(open_service):
    service = open_service()
    result = service.add_note_if_absent(card("コーヒー", "コーヒー"))
    assert result.status is AddStatus.ADDED
    assert service.known_words().contains("コーヒー", "コーヒー")
    assert service.known_words().contains("コーヒー", "こーひー")
    assert service.rebuild_known_words().contains("コーヒー", "コーヒー")
    assert service.delete_note(result.note_id) is DeleteStatus.DELETED
    assert not service.known_words().contains("コーヒー", "コーヒー")


def test_audio_uses_add_file_returned_name(open_service, tmp_path):
    service = open_service()
    first = tmp_path / "src1" / "voice.mp3"
    second = tmp_path / "src2" / "voice.mp3"
    for path, payload in ((first, b"one"), (second, b"two")):
        path.parent.mkdir()
        path.write_bytes(b"ID3" + payload)
    service.add_note_if_absent(card("一", "いち", audio=str(first)))
    result = service.add_note_if_absent(card("二", "に", audio=str(second)))
    audio = service.collection.get_note(result.note_id)["Audio"]
    assert audio.startswith("[sound:voice-") and audio.endswith(".mp3]")


def test_known_words_snapshot_is_copy_on_write(open_service):
    service = open_service()
    before = service.known_words()
    service.add_note_if_absent(card("食べる", "たべる"))
    assert not before.contains("食べる", "たべる")
    assert service.known_words() is not before
    with pytest.raises(TypeError):
        service.known_words().bot[("x", "y")] = 1


# --- extra sources (read-only) --------------------------------------------------


def test_extra_source_word_is_known_and_note_untouched(open_service, tmp_path):
    make_collection(tmp_path)
    col = Collection(str(tmp_path / "collection.anki2"))
    nid = add_raw_note(col, YOMITAN, Expression="勉強", Reading="べんきょう", Glossary="study")
    col.close()
    service = open_service(YOMITAN_SOURCE)
    before = service.collection.db.first("select mod, usn, flds from notes where id = ?", nid)

    assert service.known_words().contains("勉強", "べんきょう")
    assert service.add_note_if_absent(card("勉強", "べんきょう")).status is AddStatus.EXISTS
    assert service.delete_note(nid) is DeleteStatus.REJECTED

    assert service.collection.db.first("select mod, usn, flds from notes where id = ?", nid) == before
    assert bot_notes(service) == []


def test_extra_source_matches_any_form(open_service):
    service = open_service(YOMITAN_SOURCE)
    add_raw_note(service.collection, YOMITAN, Expression="宜しい", Reading="よろしい")
    forms = ("よろしい", "宜しい")
    assert service.add_note_if_absent(card("よろしい", "よろしい", forms=forms)).status is AddStatus.EXISTS
    assert service.rebuild_known_words().contains("よろしい", "よろしい", forms)


def test_extra_source_reading_must_match_when_configured(open_service):
    service = open_service(YOMITAN_SOURCE)
    add_raw_note(service.collection, YOMITAN, Expression="辛い", Reading="からい")
    assert service.rebuild_known_words().contains("辛い", "からい")
    assert not service.known_words().contains("辛い", "つらい")
    assert service.add_note_if_absent(card("辛い", "つらい")).status is AddStatus.ADDED


def test_extra_source_katakana_reading_is_normalized(open_service):
    service = open_service(YOMITAN_SOURCE)
    add_raw_note(service.collection, YOMITAN, Expression="<b>勉強</b>", Reading="ベンキョウ")
    assert service.rebuild_known_words().contains("勉強", "べんきょう")
    assert service.add_note_if_absent(card("勉強", "べんきょう")).status is AddStatus.EXISTS


def test_extra_source_empty_reading_falls_back_to_word(open_service):
    service = open_service(YOMITAN_SOURCE)
    add_raw_note(service.collection, YOMITAN, Expression="ありがとう", Reading="&nbsp;")
    assert service.rebuild_known_words().contains("ありがとう", "ありがとう")
    assert service.add_note_if_absent(card("ありがとう", "ありがとう")).status is AddStatus.EXISTS


def test_extra_source_without_reading_field_matches_word_only(open_service):
    service = open_service(PLAIN_SOURCE)
    add_raw_note(service.collection, PLAIN, Front="かみ")
    forms = ("かみ", "紙")
    assert service.rebuild_known_words().contains("紙", "かみ", forms)
    assert service.add_note_if_absent(card("紙", "かみ", forms=forms)).status is AddStatus.EXISTS


def test_undo_does_not_touch_extra_source_cache(open_service):
    service = open_service(YOMITAN_SOURCE)
    add_raw_note(service.collection, YOMITAN, Expression="猫", Reading="ねこ")
    add_raw_note(service.collection, NOTETYPE, Word="猫", Reading="ねこ")
    service.rebuild_known_words()
    bot_id = bot_notes(service)[0].id
    assert service.delete_note(bot_id) is DeleteStatus.DELETED
    assert service.known_words().contains("猫", "ねこ")


# --- delete_note ----------------------------------------------------------------


def test_delete_note(open_service):
    service = open_service()
    nid = service.add_note_if_absent(card("食べる", "たべる")).note_id
    assert service.delete_note(nid) is DeleteStatus.DELETED
    assert bot_notes(service) == []
    assert service.delete_note(nid) is DeleteStatus.NOT_FOUND


def test_delete_rejects_other_notetype(open_service):
    service = open_service()
    nid = add_raw_note(service.collection, PLAIN, Front="x")
    assert service.delete_note(nid) is DeleteStatus.REJECTED
    assert service.collection.get_note(nid)["Front"] == "x"


def test_undo_one_of_two_bot_notes_keeps_word_known(open_service):
    service = open_service()
    add_raw_note(service.collection, NOTETYPE, Word="食べる", Reading="たべる")
    add_raw_note(service.collection, NOTETYPE, Word="<b>食べる</b>", Reading="タベル")
    known = service.rebuild_known_words()
    assert known.bot[("食べる", "たべる")] == 2
    first = bot_notes(service)[0].id
    assert service.delete_note(first) is DeleteStatus.DELETED
    assert service.known_words().contains("食べる", "たべる")
    assert service.add_note_if_absent(card("食べる", "たべる")).status is AddStatus.EXISTS


# --- full_sync_required ---------------------------------------------------------


def test_full_sync_required_blocks_writes(open_service):
    blocked = False
    service = open_service(blocked=lambda: blocked)
    nid = service.add_note_if_absent(card("食べる", "たべる")).note_id
    blocked = True
    known = service.known_words()
    assert service.add_note_if_absent(card("見る", "みる")).status is AddStatus.BLOCKED
    assert service.delete_note(nid) is DeleteStatus.BLOCKED
    assert [n["Word"] for n in bot_notes(service)] == ["食べる"]
    assert service.known_words() is known


# --- stats ----------------------------------------------------------------------


def test_stats_counts_todays_bot_notes_and_deck_cards_with_subdecks(open_service):
    service = open_service()
    col = service.collection
    service.add_note_if_absent(card("食べる", "たべる"))
    service.add_note_if_absent(card("見る", "みる"))
    child = col.decks.id(f"{DECK}::Child", create=True)
    note = col.new_note(col.models.by_name(NOTETYPE))
    note["Word"], note["Reading"] = "犬", "いぬ"
    col.add_note(note, child)
    add_raw_note(col, PLAIN, Front="x", Back="y")
    stats = service.stats()
    assert stats.added_today == 3
    assert stats.deck_cards == 3


# --- data lock ------------------------------------------------------------------


def test_data_lock_is_exclusive(tmp_path):
    lock = tmp_path / ".lock"
    with data_lock(lock):
        with pytest.raises(DataLockError, match="locked"):
            with data_lock(lock):
                pass
    with data_lock(lock):
        pass
