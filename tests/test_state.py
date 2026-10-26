import pickle

from anki_helpers import card

from ankibot.state import Preview, PreviewStatus, expire, get_previews, load_persistence_file, reset_saving

TTL = 24 * 3600


def preview(mid, created_at=0.0, status=PreviewStatus.PENDING):
    return Preview(
        chat_id=1,
        message_id=mid,
        body_html=f"body {mid}",
        card=card("食べる", "たべる"),
        created_at=created_at,
    ).with_status(status)


def test_expire_removes_only_old_pending():
    user_data = {}
    previews = get_previews(user_data)
    previews[1] = preview(1, created_at=0)
    previews[2] = preview(2, created_at=TTL)
    previews[3] = preview(3, created_at=0, status=PreviewStatus.SAVING)
    assert expire(user_data, now=TTL + 1, ttl_seconds=TTL) == [1]
    assert set(get_previews(user_data)) == {2, 3}


def test_is_expired_boundary():
    p = preview(1, created_at=100)
    assert not p.is_expired(now=100 + TTL - 1, ttl_seconds=TTL)
    assert p.is_expired(now=100 + TTL, ttl_seconds=TTL)


def test_reset_saving_turns_saving_into_pending():
    user_data = {}
    previews = get_previews(user_data)
    previews[1] = preview(1, status=PreviewStatus.SAVING)
    previews[2] = preview(2)
    reset = reset_saving(user_data)
    assert [p.message_id for p in reset] == [1]
    assert all(p.status is PreviewStatus.PENDING for p in get_previews(user_data).values())


def test_preview_survives_pickle():
    p = preview(7, status=PreviewStatus.SAVING)
    assert pickle.loads(pickle.dumps(p)) == p


def test_corrupt_persistence_file_is_moved_aside(tmp_path):
    path = tmp_path / "bot_state.pickle"
    path.write_bytes(b"not a pickle")
    moved = load_persistence_file(path, stamp="20261026-090000")
    assert moved == tmp_path / "bot_state.pickle.corrupt-20261026-090000"
    assert not path.exists() and moved.read_bytes() == b"not a pickle"


def test_valid_or_missing_persistence_file_is_kept(tmp_path):
    path = tmp_path / "bot_state.pickle"
    assert load_persistence_file(path, stamp="x") is None
    path.write_bytes(pickle.dumps({"user_data": {}}))
    assert load_persistence_file(path, stamp="x") is None
    assert path.exists()
