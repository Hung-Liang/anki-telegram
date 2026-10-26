import os

from ankibot.healthcheck import Heartbeat, is_healthy, main


async def test_heartbeat_writes_only_after_a_successful_probe(tmp_path):
    path = tmp_path / "heartbeat"
    calls = []

    async def failing():
        calls.append("fail")
        raise ConnectionError("telegram unreachable")

    assert not await Heartbeat(path, failing).beat()
    assert not path.exists()

    async def ok():
        calls.append("ok")

    assert await Heartbeat(path, ok).beat()
    assert path.exists()
    assert calls == ["fail", "ok"]


async def test_failed_probe_leaves_old_heartbeat_untouched(tmp_path):
    path = tmp_path / "heartbeat"
    path.write_text("old")
    os.utime(path, (1000, 1000))

    async def failing():
        raise TimeoutError

    await Heartbeat(path, failing).beat()
    assert path.stat().st_mtime == 1000


async def test_run_beats_every_interval(tmp_path):
    sleeps = []
    beats = []

    async def probe():
        beats.append(1)
        if len(beats) == 3:
            raise KeyboardInterrupt  # stop the loop

    async def sleep(seconds):
        sleeps.append(seconds)

    hb = Heartbeat(tmp_path / "heartbeat", probe, interval=60, sleep=sleep)
    try:
        await hb.run()
    except KeyboardInterrupt:
        pass
    assert sleeps == [60, 60]


def test_is_healthy_checks_age(tmp_path):
    path = tmp_path / "heartbeat"
    assert not is_healthy(path, max_age=180, now=2000)
    path.write_text("x")
    os.utime(path, (1900, 1900))
    assert is_healthy(path, max_age=180, now=2000)
    assert not is_healthy(path, max_age=180, now=2081)


def test_main_exit_code(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat"
    monkeypatch.setenv("HEARTBEAT_FILE", str(path))
    assert main() == 1
    path.write_text("x")
    assert main() == 0
