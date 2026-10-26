"""Bot UI text, loaded from `locales/<language>.toml` and optionally overridden by a user file.

TOML `[section] key` becomes the attribute `section_key`. Every locale must define every key, with
the same {placeholders} as the reference locale.
"""

from __future__ import annotations

import string
import tomllib
from dataclasses import dataclass, fields
from functools import cache
from pathlib import Path

LOCALES_DIR = Path(__file__).parent / "locales"
DEFAULT_LANGUAGE = "en"
# Placeholders of every locale and override must match this one.
_REFERENCE_PATH = LOCALES_DIR / f"{DEFAULT_LANGUAGE}.toml"


class TextsError(Exception):
    pass


@dataclass(frozen=True)
class Texts:
    status_saving: str
    status_queued: str
    status_saved: str
    status_exists: str
    status_failed: str
    status_blocked: str
    status_discarded: str
    status_undone: str
    status_interrupted: str
    hint_tts_failed: str
    hint_translation_failed: str
    preview_translating: str
    preview_example: str
    button_save: str
    button_discard: str
    button_undo: str
    answer_expired: str
    answer_note_gone: str
    answer_rejected: str
    answer_undo_blocked: str
    answer_undo_failed: str
    reply_processing_failed: str
    reply_no_new_words: str
    reply_existing: str
    reply_not_found: str
    reply_word_separator: str
    reply_help: str
    sync_in_progress: str
    sync_ok: str
    sync_full_sync_required: str
    sync_skipped: str
    sync_failed: str
    sync_recovery_hint: str
    stats_added_today: str
    stats_deck_cards: str
    stats_last_sync: str
    stats_never_synced: str
    stats_full_sync_required: str
    notify_full_sync_required: str
    notify_sync_failed: str

    @property
    def statuses(self) -> tuple[str, ...]:
        return tuple(getattr(self, f.name) for f in fields(self) if f.name.startswith("status_"))


KEYS = frozenset(f.name for f in fields(Texts))


def available_languages() -> list[str]:
    return sorted(p.stem for p in LOCALES_DIR.glob("*.toml"))


def _read(path: Path) -> dict[str, str]:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise TextsError(f"cannot read {path}: {exc}") from exc
    flat: dict[str, str] = {}
    for section, table in data.items():
        if not isinstance(table, dict):
            raise TextsError(f"{path}: top-level key {section!r} must be a [section]")
        for key, value in table.items():
            if not isinstance(value, str):
                raise TextsError(f"{path}: {section}.{key} must be a string")
            flat[f"{section}_{key}"] = value
    unknown = flat.keys() - KEYS
    if unknown:
        raise TextsError(f"{path}: unknown keys {sorted(k.replace('_', '.', 1) for k in unknown)}")
    return flat


def _placeholders(path: Path, key: str, template: str) -> frozenset[str]:
    try:
        return frozenset(name for _, name, _, _ in string.Formatter().parse(template) if name is not None)
    except ValueError as exc:
        raise TextsError(f"{path}: {key.replace('_', '.', 1)}: {exc}") from exc


def _check_placeholders(path: Path, values: dict[str, str], reference: dict[str, str]) -> None:
    for key, template in values.items():
        expected = _placeholders(_REFERENCE_PATH, key, reference[key])
        found = _placeholders(path, key, template)
        if found != expected:
            raise TextsError(
                f"{path}: {key.replace('_', '.', 1)} must use the placeholders "
                f"{sorted(expected)}, found {sorted(found)}"
            )


def _read_complete(path: Path) -> dict[str, str]:
    values = _read(path)
    missing = KEYS - values.keys()
    if missing:
        raise TextsError(f"{path}: missing keys {sorted(k.replace('_', '.', 1) for k in missing)}")
    return values


@cache
def _reference() -> dict[str, str]:
    return _read_complete(_REFERENCE_PATH)


def load_texts(language: str = DEFAULT_LANGUAGE, override: Path | None = None) -> Texts:
    """Built-in `language`, with the keys present in `override` (any subset) replacing its values."""
    if language not in available_languages():
        raise TextsError(f"unknown BOT_LANGUAGE {language!r}; available: {', '.join(available_languages())}")
    path = LOCALES_DIR / f"{language}.toml"
    values = _read_complete(path)
    reference = _reference()
    _check_placeholders(path, values, reference)
    if override is not None:
        extra = _read(override)
        _check_placeholders(override, extra, reference)
        values.update(extra)
    return Texts(**values)
