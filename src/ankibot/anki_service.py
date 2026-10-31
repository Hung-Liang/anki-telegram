from __future__ import annotations

import dataclasses
import fcntl
import html
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from anki.collection import Collection
from anki.errors import NotFoundError

from ankibot.config import AnkiSettings
from ankibot.models import CardData
from ankibot.tokenizer import to_hiragana

REQUIRED_FIELDS = (
    "Word",
    "Reading",
    "Meaning",
    "MeaningTL",
    "Sentence",
    "SentenceTL",
    "ExampleJP",
    "ExampleTL",
    "Audio",
    "Source",
)
CARD_FIELDS = {
    "Word": "word",
    "Reading": "reading",
    "Meaning": "meaning",
    "MeaningTL": "meaning_tl",
    "Sentence": "sentence",
    "SentenceTL": "sentence_tl",
    "ExampleJP": "example_jp",
    "ExampleTL": "example_tl",
    "Source": "source",
}
# Optional: sentence audio is written only when the note type has this field.
SENTENCE_AUDIO_FIELD = "SentenceAudio"
FIELD_SEPARATOR = "\x1f"
NAME_RULE = "Note type and field names must not contain ':' or ';'; rename them in Anki desktop."

_TAG_RE = re.compile(r"<!--.*?-->|<[^>]*>", re.DOTALL)
_SEARCH_SPECIAL = '\\"*_:'

DedupKey = tuple[str, str]


class AnkiConfigError(Exception):
    pass


class DataLockError(Exception):
    pass


@contextmanager
def data_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DataLockError(f"{path} is locked by another process; stop the running bot first") from exc
        yield


def strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text))


def dedup_key(word: str, reading: str) -> DedupKey:
    return strip_html(word).strip(), to_hiragana(strip_html(reading).strip())


def escape_search(text: str) -> str:
    return "".join("\\" + c if c in _SEARCH_SPECIAL else c for c in text)


def _loose_search(notetype: str, field: str, value: str) -> str:
    # Field search does not decode entities, so also try the escaped forms the field may store.
    variants = dict.fromkeys((value, html.escape(value, quote=False), html.escape(value)))
    terms = " OR ".join(f'"{escape_search(field)}:*{escape_search(v)}*"' for v in variants)
    return f'"note:{escape_search(notetype)}" ({terms})'


@dataclass(frozen=True)
class ExtraSourceSpec:
    notetype: str
    word_field: str
    reading_field: str | None


def parse_extra_sources(raw: str) -> tuple[ExtraSourceSpec, ...]:
    specs = []
    for segment in raw.split(";"):
        if not segment.strip():
            continue
        parts = [p.strip() for p in segment.split(":")]
        if len(parts) not in (2, 3) or not all(parts):
            raise AnkiConfigError(
                f"Invalid DEDUP_EXTRA_SOURCES entry {segment!r}: expected NoteType:WordField[:ReadingField]. "
                + NAME_RULE
            )
        specs.append(ExtraSourceSpec(parts[0], parts[1], parts[2] if len(parts) == 3 else None))
    return tuple(specs)


@dataclass(frozen=True)
class ExtraSource:
    spec: ExtraSourceSpec
    notetype_id: int
    word_index: int
    reading_index: int | None

    def record(self, fields: list[str]) -> tuple[str, str | None]:
        """Normalized (word, reading); reading is None when the note must be matched by word only."""
        reading = fields[self.reading_index] if self.reading_index is not None else ""
        word, norm_reading = dedup_key(fields[self.word_index], reading)
        return word, norm_reading or None


@dataclass(frozen=True)
class CollectionSchema:
    notetype: str
    notetype_id: int
    field_index: Mapping[str, int]
    deck_id: int
    extra_sources: tuple[ExtraSource, ...]


def _field_names(col: Collection, notetype_id: int) -> list[str]:
    return [f["name"] for f in col.models.get(notetype_id)["flds"]]


def validate_collection(col: Collection, notetype: str, deck: str, extra_sources: str) -> CollectionSchema:
    specs = parse_extra_sources(extra_sources)
    nt = col.models.by_name(notetype)
    if nt is None:
        raise AnkiConfigError(
            f"Note type {notetype!r} not found. Create it in Anki desktop with fields "
            f"{', '.join(REQUIRED_FIELDS)}, sync, then run bootstrap again."
        )
    names = _field_names(col, nt["id"])
    missing = [f for f in REQUIRED_FIELDS if f not in names]
    if missing:
        raise AnkiConfigError(f"Note type {notetype!r} is missing required fields: {', '.join(missing)}")
    # decks.id() would create the deck; id_for_name() only looks it up.
    deck_id = col.decks.id_for_name(deck)
    if deck_id is None:
        raise AnkiConfigError(f"Deck {deck!r} not found. Create it in Anki desktop, sync, then try again.")

    extras = []
    for spec in specs:
        extra_nt = col.models.by_name(spec.notetype)
        if extra_nt is None:
            raise AnkiConfigError(f"DEDUP_EXTRA_SOURCES: note type {spec.notetype!r} not found. {NAME_RULE}")
        extra_names = _field_names(col, extra_nt["id"])
        for field in (spec.word_field, spec.reading_field):
            if field is not None and field not in extra_names:
                raise AnkiConfigError(
                    f"DEDUP_EXTRA_SOURCES: field {field!r} not found in note type {spec.notetype!r}. "
                    + NAME_RULE
                )
        extras.append(
            ExtraSource(
                spec=spec,
                notetype_id=extra_nt["id"],
                word_index=extra_names.index(spec.word_field),
                reading_index=extra_names.index(spec.reading_field) if spec.reading_field else None,
            )
        )
    return CollectionSchema(
        notetype=notetype,
        notetype_id=nt["id"],
        field_index=MappingProxyType({name: i for i, name in enumerate(names)}),
        deck_id=deck_id,
        extra_sources=tuple(extras),
    )


@dataclass(frozen=True)
class ExtraKnown:
    pairs: frozenset[DedupKey]
    # Notes matched by word only: the source has no reading field, or the note's reading is empty.
    words: frozenset[str]

    def matches(self, keys: Iterable[DedupKey]) -> bool:
        return any(key in self.pairs or key[0] in self.words for key in keys)


@dataclass(frozen=True)
class KnownWords:
    """Immutable snapshot; changes always produce a new object (copy-on-write)."""

    bot: Mapping[DedupKey, int]
    extra: tuple[ExtraKnown, ...]

    def contains(self, word: str, reading: str, forms: Iterable[str] = ()) -> bool:
        if self.bot.get(dedup_key(word, reading), 0) > 0:
            return True
        keys = {dedup_key(form, reading) for form in (word, *forms)}
        return any(source.matches(keys) for source in self.extra)

    def with_bot_delta(self, key: DedupKey, delta: int) -> KnownWords:
        counts = dict(self.bot)
        count = counts.get(key, 0) + delta
        if count > 0:
            counts[key] = count
        else:
            counts.pop(key, None)
        return dataclasses.replace(self, bot=MappingProxyType(counts))


class AddStatus(StrEnum):
    ADDED = "added"
    EXISTS = "exists"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class AddResult:
    status: AddStatus
    note_id: int | None = None


class DeleteStatus(StrEnum):
    DELETED = "deleted"
    NOT_FOUND = "not_found"
    REJECTED = "rejected"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Stats:
    added_today: int
    deck_cards: int


class AnkiService:
    """Owns the Collection; every method except known_words() must run on the Anki thread."""

    def __init__(
        self,
        col: Collection,
        schema: CollectionSchema,
        full_sync_required: Callable[[], bool] = lambda: False,
    ) -> None:
        self._col = col
        self._schema = schema
        self._full_sync_required = full_sync_required
        self._known = self.rebuild_known_words()

    @classmethod
    def open(
        cls, settings: AnkiSettings, full_sync_required: Callable[[], bool] = lambda: False
    ) -> AnkiService:
        path = settings.collection_path
        # Collection() silently creates a new file, which would later force a full sync.
        if not path.exists():
            raise AnkiConfigError(f"{path} does not exist; run `python -m ankibot.bootstrap` first")
        col = Collection(str(path))
        try:
            schema = validate_collection(
                col, settings.anki_notetype, settings.anki_deck, settings.dedup_extra_sources
            )
        except BaseException:
            col.close()
            raise
        return cls(col, schema, full_sync_required)

    @property
    def collection(self) -> Collection:
        return self._col

    @property
    def schema(self) -> CollectionSchema:
        return self._schema

    def known_words(self) -> KnownWords:
        """Safe to call from the event loop: returns the current immutable snapshot."""
        return self._known

    def rebuild_known_words(self) -> KnownWords:
        word_i = self._schema.field_index["Word"]
        reading_i = self._schema.field_index["Reading"]
        counts: dict[DedupKey, int] = {}
        for fields in self._note_fields(self._schema.notetype_id):
            key = dedup_key(fields[word_i], fields[reading_i])
            counts[key] = counts.get(key, 0) + 1

        extras = []
        for source in self._schema.extra_sources:
            pairs: set[DedupKey] = set()
            words: set[str] = set()
            for fields in self._note_fields(source.notetype_id):
                word, reading = source.record(fields)
                if reading is None:
                    words.add(word)
                else:
                    pairs.add((word, reading))
            extras.append(ExtraKnown(frozenset(pairs), frozenset(words)))

        self._known = KnownWords(MappingProxyType(counts), tuple(extras))
        return self._known

    def _note_fields(self, notetype_id: int) -> Iterator[list[str]]:
        for (flds,) in self._col.db.all("select flds from notes where mid = ?", notetype_id):
            yield flds.split(FIELD_SEPARATOR)

    def _bot_note_exists(self, key: DedupKey) -> bool:
        query = _loose_search(self._schema.notetype, "Word", key[0])
        for nid in self._col.find_notes(query):
            note = self._col.get_note(nid)
            if dedup_key(note["Word"], note["Reading"]) == key:
                return True
        return False

    def _extra_note_exists(self, word: str, reading: str, forms: Iterable[str]) -> bool:
        keys = {dedup_key(form, reading) for form in (word, *forms)}
        for source in self._schema.extra_sources:
            for form, norm_reading in keys:
                query = _loose_search(source.spec.notetype, source.spec.word_field, form)
                for nid in self._col.find_notes(query):
                    note_word, note_reading = source.record(self._col.get_note(nid).fields)
                    if note_word == form and note_reading in (None, norm_reading):
                        return True
        return False

    def add_note_if_absent(self, card: CardData) -> AddResult:
        if self._full_sync_required():
            return AddResult(AddStatus.BLOCKED)
        key = dedup_key(card.word, card.reading)
        if self._bot_note_exists(key) or self._extra_note_exists(card.word, card.reading, card.forms):
            return AddResult(AddStatus.EXISTS)

        note = self._col.new_note(self._col.models.get(self._schema.notetype_id))
        for field, attr in CARD_FIELDS.items():
            note[field] = getattr(card, attr)
        if card.audio:
            # add_file may rename the file, so the field must use the returned name.
            note["Audio"] = f"[sound:{self._col.media.add_file(card.audio)}]"
        if card.sentence_audio and SENTENCE_AUDIO_FIELD in self._schema.field_index:
            note[SENTENCE_AUDIO_FIELD] = f"[sound:{self._col.media.add_file(card.sentence_audio)}]"
        self._col.add_note(note, self._schema.deck_id)
        self._known = self._known.with_bot_delta(key, 1)
        return AddResult(AddStatus.ADDED, note.id)

    def delete_note(self, note_id: int) -> DeleteStatus:
        if self._full_sync_required():
            return DeleteStatus.BLOCKED
        try:
            note = self._col.get_note(note_id)
        except NotFoundError:
            return DeleteStatus.NOT_FOUND
        if note.mid != self._schema.notetype_id:
            return DeleteStatus.REJECTED
        key = dedup_key(note["Word"], note["Reading"])
        self._col.remove_notes([note_id])
        self._known = self._known.with_bot_delta(key, -1)
        return DeleteStatus.DELETED

    def stats(self) -> Stats:
        deck = self._col.decks.name(self._schema.deck_id)
        # added:1 follows the collection's day rollover and the container TZ; deck: includes subdecks.
        added = self._col.find_notes(f'"note:{escape_search(self._schema.notetype)}" added:1')
        cards = self._col.find_cards(f'"deck:{escape_search(deck)}"')
        return Stats(added_today=len(added), deck_cards=len(cards))

    def close(self) -> None:
        self._col.close()
