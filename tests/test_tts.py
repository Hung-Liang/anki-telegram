import asyncio

import pytest

from ankibot.tts import EdgeTTS, TTSError


class FakeCommunicate:
    """Stands in for edge_tts.Communicate; `script` decides what each attempt does."""

    instances = []

    def __init__(self, script):
        self.script = script

    def __call__(self, text, voice):
        outer = self

        class Instance:
            async def save(self, path):
                FakeCommunicate.instances.append((text, voice, path))
                action = outer.script.pop(0) if outer.script else "ok"
                if action == "hang":
                    await asyncio.Event().wait()
                if action == "error":
                    raise ConnectionError("edge down")
                if action == "partial":
                    with open(path, "wb") as fh:
                        fh.write(b"half")
                    raise ConnectionError("dropped mid-stream")
                with open(path, "wb") as fh:
                    fh.write(f"{voice}:{text}".encode())

        return Instance()


@pytest.fixture(autouse=True)
def reset_instances():
    FakeCommunicate.instances = []


def make(tmp_path, script=(), voice="ja-JP-NanamiNeural", timeout=1.0):
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    tts = EdgeTTS(tmp_path / "tts", voice, timeout, communicate=FakeCommunicate(list(script)), sleep=sleep)
    return tts, sleeps


async def test_synth_writes_file_named_by_voice_and_text(tmp_path):
    tts, _ = make(tmp_path)
    path = await tts.synth("たべる")
    assert path.read_bytes() == "ja-JP-NanamiNeural:たべる".encode()
    assert path.parent == tmp_path / "tts" and path.suffix == ".mp3"
    other_voice, _ = make(tmp_path, voice="ja-JP-KeitaNeural")
    assert other_voice.path_for("たべる") != path
    assert tts.path_for("みる") != path


async def test_existing_file_is_reused(tmp_path):
    tts, _ = make(tmp_path)
    first = await tts.synth("たべる")
    second = await tts.synth("たべる")
    assert first == second
    assert len(FakeCommunicate.instances) == 1


async def test_retries_with_backoff_then_succeeds(tmp_path):
    tts, sleeps = make(tmp_path, script=["error", "error", "ok"])
    path = await tts.synth("たべる")
    assert path.exists()
    assert sleeps == [1, 2]


async def test_gives_up_after_three_attempts_and_leaves_no_file(tmp_path):
    tts, sleeps = make(tmp_path, script=["error", "partial", "error"])
    with pytest.raises(TTSError):
        await tts.synth("たべる")
    assert len(FakeCommunicate.instances) == 3
    assert list((tmp_path / "tts").iterdir()) == []


async def test_each_attempt_is_bounded_by_the_timeout(tmp_path):
    tts, _ = make(tmp_path, script=["hang", "hang", "hang"], timeout=0.05)
    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(TTSError):
        await tts.synth("たべる")
    assert loop.time() - start < 1
    assert list((tmp_path / "tts").iterdir()) == []
