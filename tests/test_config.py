import pytest
from pydantic import ValidationError

from ankibot.config import AnkiSettings


@pytest.mark.parametrize("endpoint", [None, "", "   "])
def test_sync_endpoint_is_required(monkeypatch, endpoint):
    monkeypatch.delenv("ANKI_SYNC_ENDPOINT", raising=False)
    values = {"anki_user": "user", "anki_password": "pass"}
    if endpoint is not None:
        values["anki_sync_endpoint"] = endpoint
    with pytest.raises(ValidationError, match="anki_sync_endpoint"):
        AnkiSettings(**values)


def test_sync_endpoint_is_stripped():
    settings = AnkiSettings(
        anki_user="user", anki_password="pass", anki_sync_endpoint=" http://anki-sync:8080/ "
    )
    assert settings.anki_sync_endpoint == "http://anki-sync:8080/"
