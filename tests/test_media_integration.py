"""Generated audio reaches the second collection through local-sync (`make test-integration`)."""

import pytest
from anki_helpers import card
from test_sync_integration import devices  # noqa: F401 - pytest fixture
from test_tts import FakeCommunicate

from ankibot.anki_service import AddStatus
from ankibot.tts import EdgeTTS

pytestmark = pytest.mark.integration

VOICE = "ja-JP-NanamiNeural"


async def test_tts_audio_is_a_sound_tag_and_syncs_to_the_other_collection(devices, tmp_path):  # noqa: F811
    a, b = devices
    # Deterministic content: the server keeps media across runs, so a reused name must keep its bytes.
    tts = EdgeTTS(tmp_path / "tts", VOICE, 5, communicate=FakeCommunicate([]))
    path = await tts.synth("はしる")
    result = await a.manager.add_note(card("走る", "はしる", audio=str(path)))
    assert result.status is AddStatus.ADDED
    await a.sync()
    await b.sync()

    field = await b.thread.run(lambda: b.service.collection.get_note(result.note_id)["Audio"])
    assert field == f"[sound:{path.name}]"
    received = await b.media_path(path.name)
    assert received.read_bytes() == path.read_bytes() == f"{VOICE}:はしる".encode()
