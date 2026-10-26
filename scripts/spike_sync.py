"""Verify the pinned anki pylib sync API against a real sync server.

The server is --endpoint, defaulting to ANKI_SYNC_ENDPOINT; one of them is required.

Targets:
  local    - full read/write verification against a local `anki.syncserver`
             (two collections A and B, notes + media round-trip).
  readonly - read-only verification: login, full download into an empty
             directory, and a sync with no local changes. Never writes to the server.

Usage:
  python scripts/spike_sync.py local --user u --password p
  python scripts/spike_sync.py readonly   # reads ANKI_USER / ANKI_PASSWORD
"""

from __future__ import annotations

import argparse
import inspect
import json
import multiprocessing
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from anki import buildinfo
from anki.collection import Collection
from anki.sync import SyncAuth

FIELDS = [
    "Word",
    "Reading",
    "Meaning",
    "MeaningTL",
    "Sentence",
    "SentenceTL",
    "ExampleJP",
    "ExampleTL",
    "Audio",
]
NOTETYPE = "JP Bot Vocab"
DECK = "Japanese::Bot"
MEDIA_POLL_INTERVAL = 0.2
MEDIA_TIMEOUT = 120
KILL_TEST_BULK_NOTES = 3000
# graves.type: 0 = card, 1 = note, 2 = deck
GRAVE_TYPE_NOTE = 1


class SpikeFailure(Exception):
    pass


@dataclass
class Report:
    findings: dict[str, object] = field(default_factory=dict)
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    def note(self, key: str, value: object) -> None:
        self.findings[key] = value
        print(f"  [info] {key}: {value}")

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" ({detail})" if detail else ""))
        if not ok:
            raise SpikeFailure(f"{name}: {detail}")

    def dump(self) -> str:
        return json.dumps(
            {
                "anki_version": buildinfo.version,
                "findings": self.findings,
                "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in self.checks],
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )


def required_name(out) -> str:
    return type(out).ChangesRequired.Name(out.required)


def login(col: Collection, user: str, password: str, endpoint: str) -> SyncAuth:
    return col.sync_login(username=user, password=password, endpoint=endpoint)


def apply_new_endpoint(auth: SyncAuth, out, report: Report, label: str) -> SyncAuth:
    if out.new_endpoint:
        report.note(f"{label}.new_endpoint", out.new_endpoint)
        auth = SyncAuth(hkey=auth.hkey, endpoint=out.new_endpoint)
    return auth


def full_sync(col: Collection, auth: SyncAuth, out, upload: bool) -> None:
    # Mirrors aqt: close_for_full_sync() hands the collection to the backend,
    # which closes and reopens the DB file around the transfer.
    col.close_for_full_sync()
    col.full_upload_or_download(auth=auth, server_usn=out.server_media_usn, upload=upload)
    col.reopen(after_full_sync=True)


def sync_media_and_wait(col: Collection, auth: SyncAuth, report: Report, label: str) -> None:
    t0 = time.monotonic()
    col.sync_media(auth)
    returned_after = time.monotonic() - t0
    first = col.media_sync_status()
    report.note(f"{label}.sync_media_returned_after_s", round(returned_after, 3))
    report.note(f"{label}.media_active_right_after_call", first.active)
    polls = 0
    while True:
        status = col.media_sync_status()
        if not status.active:
            break
        polls += 1
        if time.monotonic() - t0 > MEDIA_TIMEOUT:
            raise SpikeFailure(f"{label}: media sync did not finish in {MEDIA_TIMEOUT}s")
        time.sleep(MEDIA_POLL_INTERVAL)
    report.note(f"{label}.media_poll_count", polls)
    report.note(f"{label}.media_total_s", round(time.monotonic() - t0, 3))


def normal_sync(col: Collection, auth: SyncAuth, report: Report, label: str, media: bool = True) -> SyncAuth:
    out = col.sync_collection(auth, sync_media=False)
    report.note(f"{label}.required", required_name(out))
    auth = apply_new_endpoint(auth, out, report, label)
    if required_name(out) not in ("NO_CHANGES", "NORMAL_SYNC"):
        raise SpikeFailure(f"{label}: unexpected {required_name(out)}")
    if media:
        sync_media_and_wait(col, auth, report, label)
    return auth


def ensure_notetype_and_deck(col: Collection) -> tuple[int, int]:
    mm = col.models
    nt = mm.new(NOTETYPE)
    for name in FIELDS:
        mm.add_field(nt, mm.new_field(name))
    tmpl = mm.new_template("Card 1")
    tmpl["qfmt"] = "{{Word}}{{Audio}}"
    tmpl["afmt"] = "{{FrontSide}}<hr id=answer>{{Reading}}<br>{{MeaningTL}}<br>{{Meaning}}"
    mm.add_template(nt, tmpl)
    mm.add(nt)
    deck_id = col.decks.id(DECK, create=True)
    return mm.id_for_name(NOTETYPE), deck_id


def add_note(col: Collection, word: str, reading: str, audio: str = "") -> int:
    nt = col.models.by_name(NOTETYPE)
    deck = col.decks.id_for_name(DECK)
    note = col.new_note(nt)
    note["Word"] = word
    note["Reading"] = reading
    note["Meaning"] = "spike"
    note["Audio"] = audio
    col.add_note(note, deck)
    return note.id


def words(col: Collection) -> set[str]:
    return {col.get_note(nid)["Word"] for nid in col.find_notes(f'"note:{NOTETYPE}"')}


def make_mp3(path: Path, payload: bytes) -> Path:
    # Content does not need to be playable for a transfer test.
    path.write_bytes(b"ID3" + payload)
    return path


def record_api_signatures(report: Report) -> None:
    for name in (
        "sync_login",
        "sync_collection",
        "sync_media",
        "media_sync_status",
        "full_upload_or_download",
        "close_for_full_sync",
        "reopen",
        "abort_sync",
        "abort_media_sync",
    ):
        report.note(f"signature.Collection.{name}", str(inspect.signature(getattr(Collection, name))))
    report.note("signature.MediaManager.add_file", str(inspect.signature(col_media_add_file())))


def col_media_add_file():
    from anki.media import MediaManager

    return MediaManager.add_file


def _killed_during_sync(
    col_path: str, user: str, password: str, endpoint: str, delay: float, marker: str
) -> None:
    import threading

    col = Collection(col_path)
    add_note(col, "kill-test", "きるてすと")
    # Bulk notes make the upload slow enough to reliably interrupt it.
    for i in range(KILL_TEST_BULK_NOTES):
        add_note(col, f"bulk-{i}", "ばるく")
    auth = login(col, user, password, endpoint)
    worker = threading.Thread(target=col.sync_collection, args=(auth, False), daemon=True)
    worker.start()
    time.sleep(delay)
    Path(marker).write_text("1" if worker.is_alive() else "0")
    os._exit(0)


def run_local(args: argparse.Namespace, report: Report) -> None:
    work = Path(args.workdir)
    a_dir, b_dir = work / "a", work / "b"
    for d in (a_dir, b_dir):
        d.mkdir(parents=True)
    endpoint = args.endpoint

    print("== A: create notetype/deck, initial upload")
    col_a = Collection(str(a_dir / "collection.anki2"))
    ensure_notetype_and_deck(col_a)
    report.note("decks.id_for_name(missing)", col_a.decks.id_for_name("No::Such::Deck"))
    report.check(
        "decks.id_for_name does not create",
        col_a.decks.id_for_name("No::Such::Deck") is None and "No::Such::Deck" not in col_a.decks.all_names(),
    )
    auth_a = login(col_a, args.user, args.password, endpoint)
    report.note("auth.endpoint_after_login", auth_a.endpoint)
    out = col_a.sync_collection(auth_a, sync_media=False)
    report.note("A.first_sync.required", required_name(out))
    auth_a = apply_new_endpoint(auth_a, out, report, "A.first_sync")
    if required_name(out) not in ("FULL_SYNC", "FULL_UPLOAD"):
        raise SpikeFailure(
            f"A.first_sync returned {required_name(out)}; the sync server account must be empty "
            "(reset the anki-sync volume before running this check)"
        )
    full_sync(col_a, auth_a, out, upload=True)
    report.check("A notetype still present after full upload", col_a.models.by_name(NOTETYPE) is not None)
    sync_media_and_wait(col_a, auth_a, report, "A.first_media")

    print("== B: empty collection, full download")
    col_b = Collection(str(b_dir / "collection.anki2"))
    auth_b = login(col_b, args.user, args.password, endpoint)
    out = col_b.sync_collection(auth_b, sync_media=False)
    report.note("B.first_sync.required", required_name(out))
    full_sync(col_b, auth_b, out, upload=False)
    report.check("B has notetype after full download", col_b.models.by_name(NOTETYPE) is not None)
    report.check("B has deck after full download", col_b.decks.id_for_name(DECK) is not None)
    report.check("B collection usable after reopen", isinstance(col_b.note_count(), int))
    auth_b = normal_sync(col_b, auth_b, report, "B.after_full")

    print("== A: add note with media + HTML, inspect usn and search")
    mp3 = make_mp3(Path(tempfile.mkdtemp()) / "spike_audio.mp3", b"first")
    fname1 = col_a.media.add_file(str(mp3))
    (mp3.parent / "dup").mkdir()
    mp3_dup = make_mp3(mp3.parent / "dup" / "spike_audio.mp3", b"different-content")
    fname2 = col_a.media.add_file(str(mp3_dup))
    fname3 = col_a.media.add_file(str(mp3))
    report.note("add_file.first", fname1)
    report.note("add_file.same_name_different_content", fname2)
    report.note("add_file.same_name_same_content", fname3)
    nid_html = add_note(col_a, "<b>食べる</b>", "たべる", f"[sound:{fname1}]")
    nid_plain = add_note(col_a, "見る", "みる")
    nid_amp = add_note(col_a, "A&amp;B", "えーびー")

    usn = col_a.db.scalar("select usn from notes where id = ?", nid_html)
    report.note("notes.usn_for_unsynced_note", usn)
    report.check("unsynced note has usn = -1", usn == -1)

    report.note("search 'Word:食べる' hits", len(col_a.find_notes(f'"note:{NOTETYPE}" "Word:食べる"')))
    report.note("search 'Word:*食べる*' hits", len(col_a.find_notes(f'"note:{NOTETYPE}" "Word:*食べる*"')))
    report.note("search 'Word:A&B' hits", len(col_a.find_notes(f'"note:{NOTETYPE}" "Word:*A&B*"')))
    report.note("search 'Word:A&amp;B' hits", len(col_a.find_notes(f'"note:{NOTETYPE}" "Word:*A&amp;B*"')))
    report.check(
        "wildcard field search finds HTML-wrapped word",
        nid_html in col_a.find_notes(f'"note:{NOTETYPE}" "Word:*食べる*"'),
    )

    col_a.remove_notes([nid_amp])
    graves = col_a.db.all("select oid, type, usn from graves")
    report.note("graves after delete (oid,type,usn)", graves)
    report.check(
        "deleted note appears in graves with usn=-1", any(g == [nid_amp, GRAVE_TYPE_NOTE, -1] for g in graves)
    )

    auth_a = normal_sync(col_a, auth_a, report, "A.after_add")
    usn_after = col_a.db.scalar("select usn from notes where id = ?", nid_html)
    report.note("notes.usn_after_sync", usn_after)

    print("== B: pull note + media")
    auth_b = normal_sync(col_b, auth_b, report, "B.pull")
    report.check("B sees A's note", "<b>食べる</b>" in words(col_b))
    report.check("B does not see deleted note", "A&amp;B" not in words(col_b))
    b_media = Path(col_b.media.dir()) / fname1
    report.check("B received media file", b_media.exists(), str(b_media))
    report.check(
        "B media content matches", b_media.read_bytes() == (Path(col_a.media.dir()) / fname1).read_bytes()
    )

    print("== B edits, A pulls")
    nid_b_plain = col_b.find_notes(f'"note:{NOTETYPE}" "Word:見る"')[0]
    note = col_b.get_note(nid_b_plain)
    note["Meaning"] = "edited-by-B"
    col_b.update_note(note)
    auth_b = normal_sync(col_b, auth_b, report, "B.push_edit")
    auth_a = normal_sync(col_a, auth_a, report, "A.pull_edit")
    report.check("A sees B's edit", col_a.get_note(nid_plain)["Meaning"] == "edited-by-B")

    print("== A and B add different notes")
    add_note(col_a, "A-only", "えーおんりー")
    add_note(col_b, "B-only", "びーおんりー")
    auth_a = normal_sync(col_a, auth_a, report, "A.concurrent")
    auth_b = normal_sync(col_b, auth_b, report, "B.concurrent")
    auth_a = normal_sync(col_a, auth_a, report, "A.concurrent2")
    report.check("A has both", {"A-only", "B-only"} <= words(col_a))
    report.check("B has both", {"A-only", "B-only"} <= words(col_b))

    print("== sync_collection(sync_media=True) behavior")
    t0 = time.monotonic()
    out = col_a.sync_collection(auth_a, sync_media=True)
    report.note("sync_collection(sync_media=True).returned_after_s", round(time.monotonic() - t0, 3))
    report.note("sync_collection(sync_media=True).media_active_after", col_a.media_sync_status().active)
    while col_a.media_sync_status().active:
        time.sleep(MEDIA_POLL_INTERVAL)

    print("== kill process during sync, then reopen and sync")
    col_a.close()
    a_path = str(a_dir / "collection.anki2")
    marker = work / "kill_marker"
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(
        target=_killed_during_sync,
        args=(a_path, args.user, args.password, endpoint, args.kill_delay, str(marker)),
    )
    proc.start()
    proc.join(120)
    report.note("killed_process.exitcode", proc.exitcode)
    report.check("process exited while sync was still running", marker.read_text() == "1")
    col_a = Collection(a_path)
    report.check("collection reopens after hard exit during sync", isinstance(col_a.note_count(), int))
    report.note("kill-test note survived", "kill-test" in words(col_a))
    auth_a = normal_sync(col_a, auth_a, report, "A.after_kill")
    auth_b = normal_sync(col_b, auth_b, report, "B.after_kill")
    report.check("B sees note written before hard exit", "kill-test" in words(col_b))
    report.check("B sees bulk notes", f"bulk-{KILL_TEST_BULK_NOTES - 1}" in words(col_b))

    print("== media db filenames")
    report.note("media_dir", col_a.media.dir())
    report.note("collection_dir_files", sorted(p.name for p in a_dir.iterdir()))

    col_a.close()
    col_b.close()


def run_readonly(args: argparse.Namespace, report: Report) -> None:
    work = Path(args.workdir) / "readonly"
    work.mkdir(parents=True)
    col = Collection(str(work / "collection.anki2"))
    auth = login(col, args.user, args.password, args.endpoint)
    report.check("login", bool(auth.hkey))
    report.note("auth.endpoint_after_login", auth.endpoint)
    out = col.sync_collection(auth, sync_media=False)
    report.note("first_sync.required", required_name(out))
    auth = apply_new_endpoint(auth, out, report, "first_sync")
    if required_name(out) not in ("FULL_SYNC", "FULL_DOWNLOAD"):
        raise SpikeFailure(f"expected a full download for an empty collection, got {required_name(out)}")
    full_sync(col, auth, out, upload=False)
    report.check("collection usable after full download", isinstance(col.note_count(), int))
    report.note("notes_after_download", col.note_count())
    report.note("has_notetype_" + NOTETYPE, col.models.by_name(NOTETYPE) is not None)
    report.note("has_deck_" + DECK, col.decks.id_for_name(DECK) is not None)
    pending = col.db.scalar("select count() from notes where usn = -1")
    report.check("no local changes before no-op sync", pending == 0, f"pending={pending}")
    out = col.sync_collection(auth, sync_media=False)
    report.note("noop_sync.required", required_name(out))
    report.check("no-op sync reports NO_CHANGES", required_name(out) == "NO_CHANGES")
    col.close()


def check_sqlite_wal(report: Report, workdir: Path) -> None:
    files = sorted(p.name for p in (workdir / "a").iterdir()) if (workdir / "a").exists() else []
    report.note("sqlite_version", sqlite3.sqlite_version)
    report.note("collection_files_after_close", files)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("target", choices=["local", "readonly"])
    parser.add_argument("--endpoint", default=os.environ.get("ANKI_SYNC_ENDPOINT") or None)
    parser.add_argument("--user", default=os.environ.get("ANKI_USER"))
    parser.add_argument("--password", default=os.environ.get("ANKI_PASSWORD"))
    parser.add_argument("--workdir", default=None, help="defaults to a new temp dir")
    parser.add_argument("--keep", action="store_true", help="keep the work dir")
    parser.add_argument("--kill-delay", type=float, default=0.05)
    parser.add_argument("--report", type=Path, help="write the JSON report here")
    args = parser.parse_args()

    if not args.user or not args.password:
        parser.error("ANKI_USER / ANKI_PASSWORD (or --user / --password) are required")
    if not args.endpoint:
        parser.error("ANKI_SYNC_ENDPOINT (or --endpoint) is required")

    args.workdir = args.workdir or tempfile.mkdtemp(prefix="spike_sync_")
    report = Report()
    record_api_signatures(report)
    ok = False
    try:
        if args.target == "local":
            run_local(args, report)
            check_sqlite_wal(report, Path(args.workdir))
        else:
            run_readonly(args, report)
        ok = True
    except SpikeFailure as exc:
        print(f"SPIKE FAILED: {exc}", file=sys.stderr)
    finally:
        text = report.dump()
        if args.report:
            args.report.write_text(text + "\n", encoding="utf-8")
        if not args.keep:
            shutil.rmtree(args.workdir, ignore_errors=True)
    print("SPIKE OK" if ok else "SPIKE FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
