from __future__ import annotations

import asyncio
import logging
import sys

from ankibot.anki_service import AnkiConfigError, DataLockError, data_lock
from ankibot.config import Settings
from ankibot.i18n import TextsError
from ankibot.runtime import serve

log = logging.getLogger("ankibot")


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and Bot API URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> int:
    settings = Settings()
    configure_logging(settings.log_level)
    try:
        with data_lock(settings.lock_path):
            asyncio.run(serve(settings))
    except (AnkiConfigError, DataLockError, TextsError) as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
