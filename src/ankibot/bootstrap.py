"""First-time setup (full download) and recovery after a full sync request (--force).

Run with the bot stopped:
  python -m ankibot.bootstrap                 # dry-run
  python -m ankibot.bootstrap --yes           # full download into an empty collection
  python -m ankibot.bootstrap --force [--yes] # back up the local collection, then full download
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from anki.collection import Collection
from anki.sync import SyncAuth

from ankibot.anki_service import AnkiConfigError, DataLockError, data_lock, validate_collection
from ankibot.config import AnkiSettings

# Renamed together so a backed-up database keeps its matching -wal / -shm files.
BACKUP_FILES = (
    "collection.anki2",
    "collection.anki2-wal",
    "collection.anki2-shm",
    "collection.media.db2",
    "collection.media.db2-wal",
    "collection.media.db2-shm",
)
# graves.type: 0 = card, 1 = note, 2 = deck
GRAVE_TYPE_NOTE = 1
DOWNLOADABLE = ("FULL_SYNC", "FULL_DOWNLOAD")

Downloader = Callable[[Collection, AnkiSettings], None]


class BootstrapError(Exception):
    pass


@dataclass(frozen=True)
class UnsyncedChanges:
    notes: tuple[tuple[str, str], ...]
    deleted_notes: int


def unsynced_changes(col: Collection) -> UnsyncedChanges:
    notes = []
    for nid in col.db.list("select id from notes where usn = -1 order by id"):
        note = col.get_note(nid)
        if "Word" in note and "Reading" in note:
            notes.append((note["Word"], note["Reading"]))
        else:
            notes.append((note.fields[0], ""))
    deleted = col.db.scalar("select count() from graves where usn = -1 and type = ?", GRAVE_TYPE_NOTE)
    return UnsyncedChanges(tuple(notes), deleted)


def is_empty_collection(path: Path) -> bool:
    if not path.exists():
        return True
    col = Collection(str(path))
    try:
        return col.note_count() == 0
    finally:
        col.close()


def backup_collection_files(data_dir: Path, stamp: str) -> list[tuple[Path, Path]]:
    moves = [
        (data_dir / name, data_dir / f"backup-{stamp}-{name}")
        for name in BACKUP_FILES
        if (data_dir / name).exists()
    ]
    for _, dst in moves:
        if dst.exists():
            raise BootstrapError(f"backup target {dst} already exists")
    for src, dst in moves:
        src.rename(dst)
    return moves


def full_download(col: Collection, settings: AnkiSettings) -> None:
    auth = col.sync_login(
        username=settings.anki_user,
        password=settings.anki_password.get_secret_value(),
        endpoint=settings.anki_sync_endpoint,
    )
    out = col.sync_collection(auth, sync_media=False)
    if out.new_endpoint:
        auth = SyncAuth(hkey=auth.hkey, endpoint=out.new_endpoint)
    required = type(out).ChangesRequired.Name(out.required)
    if required not in DOWNLOADABLE:
        raise BootstrapError(f"expected the server to offer a full download, got {required}")
    col.close_for_full_sync()
    col.full_upload_or_download(auth=auth, server_usn=out.server_media_usn, upload=False)
    col.reopen(after_full_sync=True)


def _print_changes(changes: UnsyncedChanges, out: Callable[[str], None]) -> None:
    if not changes.notes and not changes.deleted_notes:
        out("No unsynced local changes.")
        return
    out("These unsynced local changes will be lost (deleted notes will reappear):")
    out(f"  Added or modified notes: {len(changes.notes)}")
    for word, reading in changes.notes:
        out(f"    - {word}（{reading}）" if reading else f"    - {word}")
    out(f"  Deleted notes: {changes.deleted_notes}")


def run(
    settings: AnkiSettings,
    *,
    force: bool,
    yes: bool,
    downloader: Downloader = full_download,
    now: Callable[[], datetime] = datetime.now,
    out: Callable[[str], None] = print,
) -> None:
    path = settings.collection_path
    with data_lock(settings.lock_path):
        if force:
            if path.exists():
                # Opening and closing through pylib also folds the WAL back into the main file.
                col = Collection(str(path))
                try:
                    changes = unsynced_changes(col)
                finally:
                    col.close()
                _print_changes(changes, out)
            else:
                out(f"{path} does not exist; nothing to back up.")
        elif not is_empty_collection(path):
            raise BootstrapError(
                f"{path} is not empty; refusing to overwrite it. "
                "To replace it with the server copy, run with --force."
            )

        out("Plan:")
        if force and path.exists():
            out(f"  - Rename the collection and media database in {settings.data_dir} to timestamped backups")
        out("  - Log in and full-download the collection from the server (media is synced later by the bot)")
        out(f"  - Check note type {settings.anki_notetype!r}, its fields, and deck {settings.anki_deck!r}")
        if force:
            out(f"  - Remove {settings.full_sync_marker_path}")
        if not yes:
            out("Dry run only; re-run with --yes to apply.")
            return

        if force:
            for src, dst in backup_collection_files(settings.data_dir, now().strftime("%Y%m%d-%H%M%S")):
                out(f"Backed up {src.name} -> {dst.name}")

        col = Collection(str(path))
        try:
            downloader(col, settings)
            validate_collection(col, settings.anki_notetype, settings.anki_deck, settings.dedup_extra_sources)
            out(f"Downloaded {col.note_count()} notes.")
        finally:
            col.close()

        if force:
            settings.full_sync_marker_path.unlink(missing_ok=True)
        out("Bootstrap complete.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ankibot.bootstrap",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--force", action="store_true", help="replace a non-empty local collection")
    parser.add_argument("--yes", action="store_true", help="apply the changes (default is a dry run)")
    args = parser.parse_args(argv)
    try:
        run(AnkiSettings(), force=args.force, yes=args.yes)
    except (BootstrapError, AnkiConfigError, DataLockError) as exc:
        print(f"bootstrap failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
