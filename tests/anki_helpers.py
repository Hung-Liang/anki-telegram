from __future__ import annotations

from pathlib import Path

from anki.collection import Collection

from ankibot.anki_service import REQUIRED_FIELDS
from ankibot.config import AnkiSettings
from ankibot.models import CardData

NOTETYPE = "JP Bot Vocab"
DECK = "Japanese::Bot"
YOMITAN = "Yomitan Japanese"
PLAIN = "Plain Vocab"


def add_notetype(col: Collection, name: str, fields: tuple[str, ...]) -> None:
    mm = col.models
    nt = mm.new(name)
    for field in fields:
        mm.add_field(nt, mm.new_field(field))
    tmpl = mm.new_template("Card 1")
    tmpl["qfmt"] = "{{" + fields[0] + "}}"
    tmpl["afmt"] = "{{FrontSide}}"
    mm.add_template(nt, tmpl)
    mm.add(nt)


def create_schema(col: Collection, bot_fields: tuple[str, ...] = REQUIRED_FIELDS, deck: bool = True) -> None:
    add_notetype(col, NOTETYPE, bot_fields)
    add_notetype(col, YOMITAN, ("Expression", "Reading", "Glossary"))
    add_notetype(col, PLAIN, ("Front", "Back"))
    if deck:
        col.decks.id(DECK, create=True)


def make_collection(data_dir: Path, **kwargs) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "collection.anki2"
    col = Collection(str(path))
    create_schema(col, **kwargs)
    col.close()
    return path


def settings(data_dir: Path, extra: str = "") -> AnkiSettings:
    return AnkiSettings(
        anki_user="user",
        anki_password="pass",
        anki_sync_endpoint="http://anki-sync:8080/",
        anki_deck=DECK,
        anki_notetype=NOTETYPE,
        dedup_extra_sources=extra,
        data_dir=data_dir,
    )


def add_raw_note(col: Collection, notetype: str, **fields: str) -> int:
    note = col.new_note(col.models.by_name(notetype))
    for key, value in fields.items():
        note[key] = value
    col.add_note(note, col.decks.id_for_name("Default"))
    return note.id


def card(word: str, reading: str, forms: tuple[str, ...] = (), audio: str = "") -> CardData:
    return CardData(
        word=word,
        reading=reading,
        meaning="to eat",
        meaning_tl="吃",
        sentence="",
        sentence_tl="",
        example_jp="",
        example_tl="",
        audio=audio,
        source=word,
        forms=forms or (reading, word),
    )
