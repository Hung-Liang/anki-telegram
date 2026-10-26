"""Download Tatoeba's Japanese sentences and build the example index (tatoeba.db).

Usage:
  python scripts/build_tatoeba.py                       # download, write $DATA_DIR/tatoeba.db
  python scripts/build_tatoeba.py --source file.tsv[.bz2] --out path/to/tatoeba.db

Re-run after upgrading fugashi or unidic-lite: the index stores their tokenization.
Data: Tatoeba (https://tatoeba.org), CC BY 2.0 FR.
"""

from __future__ import annotations

import argparse
import bz2
import io
import os
import sqlite3
import sys
import urllib.request
from collections.abc import Iterable
from pathlib import Path

from ankibot.examples import SCHEMA
from ankibot.tokenizer import KEEP_POS, Tokenizer

SOURCE_URL = "https://downloads.tatoeba.org/exports/per_language/jpn/jpn_sentences.tsv.bz2"
BATCH = 5000


def _rows(lines: Iterable[str]) -> Iterable[tuple[int, str]]:
    for line in lines:
        parts = line.rstrip("\n").split("\t")
        if len(parts) >= 3 and parts[1] == "jpn" and parts[2].strip():
            yield int(parts[0]), parts[2].strip()


def build(lines: Iterable[str], out: Path, tokenizer: Tokenizer) -> int:
    """Index every Japanese sentence by the orthBase and lemma of its content-word tokens."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    count = 0
    try:
        con = sqlite3.connect(tmp)
        try:
            con.executescript(SCHEMA)
            sentences: list[tuple[int, str]] = []
            words: list[tuple[str, int]] = []
            for sid, text in _rows(lines):
                sentences.append((sid, text))
                forms = {
                    form
                    for token in tokenizer.tokenize(text)
                    if token.pos1 in KEEP_POS
                    for form in (token.orth_base, token.lemma)
                    if form is not None
                }
                words.extend((form, sid) for form in forms)
                count += 1
                if len(sentences) >= BATCH:
                    _flush(con, sentences, words)
            _flush(con, sentences, words)
            con.commit()
        finally:
            con.close()
        # Atomic swap so the bot never opens a half-built database.
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    return count


def _flush(con: sqlite3.Connection, sentences: list, words: list) -> None:
    con.executemany("insert or ignore into sentences (id, text) values (?, ?)", sentences)
    con.executemany("insert or ignore into sentence_words (form, sentence_id) values (?, ?)", words)
    sentences.clear()
    words.clear()


def _open_source(source: str | None) -> io.TextIOBase:
    if source is None:
        print(f"Downloading {SOURCE_URL}", file=sys.stderr)
        response = urllib.request.urlopen(SOURCE_URL, timeout=60)  # noqa: S310 - fixed https URL
        return io.TextIOWrapper(bz2.BZ2File(response), encoding="utf-8")
    if source.endswith(".bz2"):
        return io.TextIOWrapper(bz2.BZ2File(source), encoding="utf-8")
    return open(source, encoding="utf-8")  # noqa: SIM115


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source", help="local jpn_sentences.tsv or .tsv.bz2 instead of downloading")
    parser.add_argument("--out", type=Path, default=Path(os.environ.get("DATA_DIR", "/data")) / "tatoeba.db")
    args = parser.parse_args(argv)
    with _open_source(args.source) as lines:
        count = build(lines, args.out, Tokenizer())
    print(f"Indexed {count} sentences into {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
