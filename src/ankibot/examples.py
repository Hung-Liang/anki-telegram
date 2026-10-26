"""Tatoeba example sentences from /data/tatoeba.db (built by scripts/build_tatoeba.py)."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
create table sentences (id integer primary key, text text not null);
create table sentence_words (
    form text not null,
    sentence_id integer not null references sentences(id),
    primary key (form, sentence_id)
) without rowid;
"""


class Examples:
    """Read-only; create and use it on the lookup thread only."""

    def __init__(self, con: sqlite3.Connection) -> None:
        self._con = con

    @classmethod
    def open(cls, path: Path) -> Examples | None:
        if not path.exists():
            log.info("%s not found; example sentences are disabled", path)
            return None
        return cls(sqlite3.connect(f"file:{path}?mode=ro", uri=True))

    def find(self, forms: Sequence[str]) -> str | None:
        forms = list(dict.fromkeys(forms))
        if not forms:
            return None
        marks = ", ".join("?" * len(forms))
        row = self._con.execute(
            "select s.text from sentence_words w join sentences s on s.id = w.sentence_id "
            f"where w.form in ({marks}) order by length(s.text), s.id limit 1",
            forms,
        ).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self._con.close()
