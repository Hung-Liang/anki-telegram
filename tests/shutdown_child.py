"""Child process for test_shutdown: runs the real serve() until the parent sends SIGTERM.

argv: <data dir> <mode>; mode "hang" makes every sync after the startup one block forever.
"""

import asyncio
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from anki_helpers import card, make_collection  # noqa: E402
from telegram_fake import FakeTelegram  # noqa: E402
from test_bot import StubBackend  # noqa: E402
from test_runtime import runtime_settings  # noqa: E402
from test_tts import FakeCommunicate  # noqa: E402

from ankibot.__main__ import configure_logging  # noqa: E402
from ankibot.runtime import Runtime, serve  # noqa: E402
from ankibot.tts import EdgeTTS  # noqa: E402


class HangAfterFirst(StubBackend):
    def sync_collection(self, auth):
        if self.syncs >= 1:
            self.syncs += 1
            threading.Event().wait()
        return super().sync_collection(auth)


async def main(data_dir: Path, mode: str) -> None:
    make_collection(data_dir)
    settings = runtime_settings(data_dir, shutdown_sync_timeout_seconds=1, heartbeat_file=data_dir / "hb")
    backend = HangAfterFirst() if mode == "hang" else StubBackend()
    tts = EdgeTTS(data_dir / "tts", "ja-JP-NanamiNeural", 1, communicate=FakeCommunicate([]))
    runtime = Runtime(settings, request=FakeTelegram(), backend=lambda service, s: backend, tts=tts)
    served = asyncio.create_task(serve(settings, runtime))
    await runtime.started.wait()
    while backend.syncs < 1:
        await asyncio.sleep(0.01)
    await runtime.sync.add_note(card("食べる", "たべる"))
    print("ready", flush=True)
    await served


if __name__ == "__main__":
    configure_logging("INFO")
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2]))
