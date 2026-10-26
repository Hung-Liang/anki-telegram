"""Preview state machine stored in user_data (persisted with PicklePersistence).

PENDING -> SAVING -> SAVED -> UNDONE, or PENDING -> DISCARDED. Only PENDING and SAVING are
stored; SAVED / DISCARDED entries are removed once the message is edited, and UNDONE is
driven by the note itself (undo:<note_id>), never by user_data.
"""

from __future__ import annotations

import dataclasses
import logging
import pickle
from collections.abc import MutableMapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from ankibot.models import CardData

log = logging.getLogger(__name__)

PREVIEWS_KEY = "previews"


class PreviewStatus(StrEnum):
    PENDING = "pending"
    SAVING = "saving"


@dataclass(frozen=True)
class Preview:
    chat_id: int
    message_id: int
    # Preview HTML without the status line; the Bot API cannot return message contents after a restart.
    body_html: str
    card: CardData
    # Unix time, so it stays meaningful across restarts.
    created_at: float
    status: PreviewStatus = PreviewStatus.PENDING

    def with_status(self, status: PreviewStatus) -> Preview:
        return dataclasses.replace(self, status=status)

    def is_expired(self, now: float, ttl_seconds: float) -> bool:
        return now - self.created_at >= ttl_seconds


def get_previews(user_data: MutableMapping[str, Any]) -> dict[int, Preview]:
    return user_data.setdefault(PREVIEWS_KEY, {})


def expire(user_data: MutableMapping[str, Any], now: float, ttl_seconds: float) -> list[int]:
    """Remove expired PENDING previews; SAVING ones are left for the save (or restart recovery)."""
    previews = get_previews(user_data)
    expired = [
        mid
        for mid, p in previews.items()
        if p.status is PreviewStatus.PENDING and p.is_expired(now, ttl_seconds)
    ]
    for mid in expired:
        del previews[mid]
    return expired


def reset_saving(user_data: MutableMapping[str, Any]) -> list[Preview]:
    """After a restart, saves that were in flight go back to PENDING (re-saving is deduplicated)."""
    previews = get_previews(user_data)
    reset = []
    for mid, p in list(previews.items()):
        if p.status is PreviewStatus.SAVING:
            previews[mid] = p.with_status(PreviewStatus.PENDING)
            reset.append(previews[mid])
    return reset


def load_persistence_file(path: Path, stamp: str) -> Path | None:
    """Move an unreadable persistence file aside so the bot starts with empty state.

    Returns the new path of the moved file, or None when the file is missing or readable.
    """
    if not path.exists():
        return None
    try:
        with path.open("rb") as fh:
            pickle.load(fh)
        return None
    except Exception as exc:
        moved = path.with_name(f"{path.name}.corrupt-{stamp}")
        path.rename(moved)
        log.error("persistence file %s is unreadable (%r); moved to %s, starting empty", path, exc, moved)
        return moved
