import json
from dataclasses import dataclass

_KEYS = {"word", "translation", "example", "example_translation"}


@dataclass(frozen=True)
class ImportItem:
    word: str
    translation: tuple[str, ...] | None = None
    example: str | None = None
    example_translation: str | None = None


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _item(entry) -> ImportItem:
    if isinstance(entry, str):
        return ImportItem(_text(entry, "word"))
    if not isinstance(entry, dict):
        raise ValueError("must be a string or an object")
    if unknown := set(entry) - _KEYS:
        raise ValueError(f"unknown keys: {', '.join(sorted(unknown))}")
    if "word" not in entry:
        raise ValueError("missing word")
    if "example_translation" in entry and "example" not in entry:
        raise ValueError("example_translation requires example")
    translation = None
    if "translation" in entry:
        items = [entry["translation"]] if isinstance(entry["translation"], str) else entry["translation"]
        if not isinstance(items, list) or not items:
            raise ValueError("translation must be a string or a non-empty array of strings")
        translation = tuple(_text(t, "translation") for t in items)
    opt = {k: _text(entry[k], k) for k in ("example", "example_translation") if k in entry}
    return ImportItem(_text(entry["word"], "word"), translation, **opt)


def parse_words(raw: str) -> list[ImportItem]:
    """Validate the whole file up front; any bad item rejects all of it."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON: {e}") from e
    if not isinstance(data, list):
        raise ValueError("top level must be an array")
    items = []
    for i, entry in enumerate(data, 1):
        try:
            items.append(_item(entry))
        except ValueError as e:
            raise ValueError(f"item {i}: {e}") from None
    return items
