"""Heartbeat for the Docker healthcheck.

The bot calls Telegram's getMe every minute and touches the heartbeat file only when it
succeeds. `python -m ankibot.healthcheck` (the image's HEALTHCHECK) checks the file's age.
This proves that the event loop is alive and Telegram's API is reachable; it does not prove
that polling still receives updates, nor that syncing works.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_FILE = Path("/tmp/ankibot-heartbeat")
INTERVAL_SECONDS = 60
PROBE_TIMEOUT_SECONDS = 30
# Three missed beats.
MAX_AGE_SECONDS = 180


class Heartbeat:
    def __init__(
        self,
        path: Path,
        probe: Callable[[], Awaitable[Any]],
        *,
        interval: float = INTERVAL_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._path = path
        self._probe = probe
        self._interval = interval
        self._sleep = sleep

    async def beat(self) -> bool:
        try:
            await asyncio.wait_for(self._probe(), PROBE_TIMEOUT_SECONDS)
        except Exception as exc:
            log.warning("heartbeat probe failed: %r", exc)
            return False
        tmp = self._path.with_name(f".{self._path.name}.tmp")
        tmp.write_text(f"{time.time():.0f}\n")
        os.replace(tmp, self._path)
        return True

    async def run(self) -> None:
        while True:
            await self.beat()
            await self._sleep(self._interval)


def is_healthy(path: Path, max_age: float = MAX_AGE_SECONDS, now: float | None = None) -> bool:
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return False
    return (time.time() if now is None else now) - mtime <= max_age


def heartbeat_file() -> Path:
    return Path(os.environ.get("HEARTBEAT_FILE") or DEFAULT_FILE)


def main() -> int:
    return 0 if is_healthy(heartbeat_file()) else 1


if __name__ == "__main__":
    sys.exit(main())
