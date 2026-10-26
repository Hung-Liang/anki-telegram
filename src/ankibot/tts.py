"""Text-to-speech. EdgeTTS uses an unofficial endpoint, so it sits behind the TTSProvider interface."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

import edge_tts

log = logging.getLogger(__name__)

ATTEMPTS = 3
BACKOFF_SECONDS = (1, 2)


class TTSProvider(Protocol):
    async def synth(self, text: str) -> Path: ...


class TTSError(Exception):
    pass


class EdgeTTS:
    def __init__(
        self,
        out_dir: Path,
        voice: str,
        timeout_seconds: float,
        *,
        communicate: Callable[[str, str], Any] = edge_tts.Communicate,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._dir = out_dir
        self._voice = voice
        self._timeout = timeout_seconds
        self._communicate = communicate
        self._sleep = sleep

    def path_for(self, text: str) -> Path:
        # The voice is part of the key, otherwise changing TTS_VOICE would keep serving old files.
        digest = hashlib.sha1(f"{self._voice}\0{text}".encode()).hexdigest()[:20]
        return self._dir / f"tts-{digest}.mp3"

    async def synth(self, text: str) -> Path:
        path = self.path_for(text)
        if path.exists():
            return path
        self._dir.mkdir(parents=True, exist_ok=True)
        last: BaseException | None = None
        for attempt in range(ATTEMPTS):
            if attempt:
                await self._sleep(BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS)) - 1])
            try:
                await asyncio.wait_for(self._write(text, path), self._timeout)
                return path
            except Exception as exc:
                last = exc
                log.warning("edge-tts attempt %d for %r failed: %r", attempt + 1, text, exc)
        raise TTSError(f"edge-tts failed {ATTEMPTS} times for {text!r}") from last

    async def _write(self, text: str, path: Path) -> None:
        # Write next to the target and rename, so a half-written file is never picked up.
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            await self._communicate(text, self._voice).save(str(tmp))
            if not tmp.exists() or tmp.stat().st_size == 0:
                raise TTSError("edge-tts produced no audio")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
