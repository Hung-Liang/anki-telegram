from datetime import datetime

import pytest
from anki.collection import Collection
from anki_helpers import NOTETYPE, add_raw_note, create_schema, make_collection, settings

from ankibot.anki_service import AnkiConfigError, DataLockError, data_lock
from ankibot.bootstrap import (
    BootstrapError,
    backup_collection_files,
    run,
    unsynced_changes,
)

STAMP_TIME = datetime(2026, 10, 26, 9, 30, 0)
STAMP = "20261026-093000"


class FakeServer:
    """Stands in for login + full download: fills the collection with the server's schema and notes."""

    def __init__(self, notes=(("サーバー", "さーばー"),), with_schema=True):
        self.notes = notes
        self.with_schema = with_schema
        self.calls = 0

    def __call__(self, col, _settings):
        self.calls += 1
        if self.with_schema:
            create_schema(col)
            for word, reading in self.notes:
                add_raw_note(col, NOTETYPE, Word=word, Reading=reading)
            col.db.execute("update notes set usn = 0")


def run_bootstrap(tmp_path, server, **kwargs):
    lines = []
    run(settings(tmp_path), downloader=server, now=lambda: STAMP_TIME, out=lines.append, **kwargs)
    return "\n".join(lines)


def words(path):
    col = Collection(str(path))
    try:
        return sorted(col.get_note(nid)["Word"] for nid in col.find_notes(f'"note:{NOTETYPE}"'))
    finally:
        col.close()


def synced_collection(tmp_path):
    """A collection whose notes look already synced, with a mix of later local changes."""
    path = make_collection(tmp_path)
    col = Collection(str(path))
    keep = add_raw_note(col, NOTETYPE, Word="既存", Reading="きそん")
    edited = add_raw_note(col, NOTETYPE, Word="編集", Reading="へんしゅう")
    gone = add_raw_note(col, NOTETYPE, Word="削除", Reading="さくじょ")
    col.db.execute("update notes set usn = 0")
    col.db.execute("delete from graves")
    note = col.get_note(edited)
    note["Meaning"] = "changed"
    col.update_note(note)
    add_raw_note(col, NOTETYPE, Word="新規", Reading="しんき")
    col.remove_notes([gone])
    col.close()
    return path, keep


def test_dry_run_on_missing_collection_does_nothing(tmp_path):
    server = FakeServer()
    output = run_bootstrap(tmp_path, server, force=False, yes=False)
    assert "Dry run" in output
    assert server.calls == 0
    assert not (tmp_path / "collection.anki2").exists()


def test_yes_downloads_and_validates(tmp_path):
    server = FakeServer()
    output = run_bootstrap(tmp_path, server, force=False, yes=True)
    assert server.calls == 1
    assert "Bootstrap complete" in output
    assert words(tmp_path / "collection.anki2") == ["サーバー"]


def test_empty_existing_collection_is_accepted(tmp_path):
    Collection(str(tmp_path / "collection.anki2")).close()
    run_bootstrap(tmp_path, FakeServer(), force=False, yes=True)
    assert words(tmp_path / "collection.anki2") == ["サーバー"]


@pytest.mark.parametrize("yes", [False, True])
def test_non_empty_collection_without_force_is_refused(tmp_path, yes):
    path, _ = synced_collection(tmp_path)
    server = FakeServer()
    with pytest.raises(BootstrapError, match="--force"):
        run_bootstrap(tmp_path, server, force=False, yes=yes)
    assert server.calls == 0
    assert "既存" in words(path)


def test_validation_failure_after_download_is_reported(tmp_path):
    with pytest.raises(AnkiConfigError, match="Note type"):
        run_bootstrap(tmp_path, FakeServer(with_schema=False), force=False, yes=True)


def test_refuses_while_data_lock_is_held(tmp_path):
    server = FakeServer()
    with data_lock(tmp_path / ".lock"):
        with pytest.raises(DataLockError):
            run_bootstrap(tmp_path, server, force=True, yes=True)
    assert server.calls == 0


def test_unsynced_changes_lists_added_modified_and_counts_deleted(tmp_path):
    path, _ = synced_collection(tmp_path)
    col = Collection(str(path))
    try:
        changes = unsynced_changes(col)
    finally:
        col.close()
    assert sorted(changes.notes) == [("新規", "しんき"), ("編集", "へんしゅう")]
    assert changes.deleted_notes == 1


def test_force_dry_run_lists_changes_and_keeps_files(tmp_path):
    path, _ = synced_collection(tmp_path)
    (tmp_path / "full_sync_required").touch()
    server = FakeServer()
    output = run_bootstrap(tmp_path, server, force=True, yes=False)

    assert "新規（しんき）" in output
    assert "編集（へんしゅう）" in output
    assert "削除" not in output
    assert "Deleted notes: 1" in output
    assert "Dry run" in output
    assert server.calls == 0
    assert "既存" in words(path)
    assert not list(tmp_path.glob("backup-*"))
    assert (tmp_path / "full_sync_required").exists()


def test_force_yes_backs_up_downloads_and_clears_marker(tmp_path):
    path, _ = synced_collection(tmp_path)
    audio = tmp_path / "keep.mp3"
    audio.write_bytes(b"ID3")
    col = Collection(str(path))
    col.media.add_file(str(audio))
    col.close()
    assert (tmp_path / "collection.media.db2").exists()
    marker = tmp_path / "full_sync_required"
    marker.touch()

    server = FakeServer()
    run_bootstrap(tmp_path, server, force=True, yes=True)

    assert server.calls == 1
    assert words(path) == ["サーバー"]
    assert sorted(words(tmp_path / f"backup-{STAMP}-collection.anki2")) == ["新規", "既存", "編集"]
    assert (tmp_path / f"backup-{STAMP}-collection.media.db2").exists()
    assert (tmp_path / "collection.media" / "keep.mp3").exists()
    assert not marker.exists()


def test_force_validation_failure_keeps_marker(tmp_path):
    synced_collection(tmp_path)
    marker = tmp_path / "full_sync_required"
    marker.touch()
    with pytest.raises(AnkiConfigError):
        run_bootstrap(tmp_path, FakeServer(with_schema=False), force=True, yes=True)
    assert marker.exists()
    assert (tmp_path / f"backup-{STAMP}-collection.anki2").exists()


def test_backup_renames_wal_and_shm_with_same_stamp(tmp_path):
    names = ["collection.anki2", "collection.anki2-wal", "collection.anki2-shm", "collection.media.db2"]
    for name in names:
        (tmp_path / name).write_text(name)

    moves = backup_collection_files(tmp_path, STAMP)

    assert len(moves) == len(names)
    for name in names:
        assert not (tmp_path / name).exists()
        assert (tmp_path / f"backup-{STAMP}-{name}").read_text() == name


def test_backup_refuses_to_overwrite_existing_backup(tmp_path):
    (tmp_path / "collection.anki2").write_text("new")
    (tmp_path / f"backup-{STAMP}-collection.anki2").write_text("old")
    with pytest.raises(BootstrapError, match="already exists"):
        backup_collection_files(tmp_path, STAMP)
    assert (tmp_path / "collection.anki2").read_text() == "new"
    assert (tmp_path / f"backup-{STAMP}-collection.anki2").read_text() == "old"
