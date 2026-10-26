"""Optional translation (DeepL or Gemini, Traditional Chinese by default), cached in /data/translations.db."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import SecretStr

log = logging.getLogger(__name__)

FREE_ENDPOINT = "https://api-free.deepl.com/v2/translate"
PRO_ENDPOINT = "https://api.deepl.com/v2/translate"
DEFAULT_TARGET = "ZH-HANT"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_LANGUAGE_NAMES = {"EN": "English", "JA": "Japanese"}
_TARGET_NAMES = {"ZH-HANT": "Traditional Chinese as used in Taiwan"}
GEMINI_INSTRUCTION = (
    "You translate for a Japanese vocabulary flashcard app. Translate each string in the JSON array "
    "from {language} into {target}. When the strings are English glosses "
    "of a Japanese word, use the context to choose the fitting meaning. Translate only: no "
    "explanations, romanization or original text. Return a JSON array of strings with exactly the "
    "same number of items, in the same order."
)


class Worker(Protocol):
    async def run(self, fn: Callable[..., Any], *args: Any) -> Any: ...


class TranslationCache:
    """Successful translations keyed by (source_lang, target_lang, text); lookup thread only."""

    def __init__(self, path: Path) -> None:
        self._con = sqlite3.connect(path)
        self._con.execute(
            "create table if not exists translations ("
            "source_lang text not null, target_lang text not null, text text not null, "
            "translation text not null, primary key (source_lang, target_lang, text)) without rowid"
        )
        self._con.commit()

    def get_many(self, source: str, target: str, texts: Sequence[str]) -> dict[str, str]:
        found = {}
        for text in dict.fromkeys(texts):
            row = self._con.execute(
                "select translation from translations where source_lang = ? and target_lang = ? and text = ?",
                (source, target, text),
            ).fetchone()
            if row:
                found[text] = row[0]
        return found

    def put_many(self, source: str, target: str, pairs: Sequence[tuple[str, str]]) -> None:
        self._con.executemany(
            "insert or replace into translations (source_lang, target_lang, text, translation) "
            "values (?, ?, ?, ?)",
            [(source, target, text, tl) for text, tl in pairs],
        )
        self._con.commit()

    def close(self) -> None:
        self._con.close()


class CachedTranslator:
    """Cache lookup and failure handling shared by the providers; subclasses send and parse requests."""

    name = ""

    def __init__(
        self,
        api_key: SecretStr,
        timeout_seconds: float,
        cache: TranslationCache,
        thread: Worker,
        *,
        target: str = DEFAULT_TARGET,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.target_lang = target
        # Cache key for the target; Gemini keeps its results apart from DeepL's.
        self.target = target
        # JMdict glosses are English, so an English target leaves them as they are.
        self.translates_glosses = not is_english(target)
        self._key = api_key
        self._timeout = timeout_seconds
        self._cache = cache
        self._thread = thread
        self._client = client or httpx.AsyncClient()

    async def translate(
        self, texts: Sequence[str], source_lang: str, context: str | None = None
    ) -> list[str] | None:
        """Translations aligned with `texts`, or None on any failure (nothing is cached then)."""
        cached = await self._thread.run(self._cache.get_many, source_lang, self.target, list(texts))
        missing = [t for t in dict.fromkeys(texts) if t not in cached]
        if missing:
            fresh = await self._request(missing, source_lang, context)
            if fresh is None:
                return None
            pairs = list(zip(missing, fresh, strict=True))
            await self._thread.run(self._cache.put_many, source_lang, self.target, pairs)
            cached.update(pairs)
        return [cached[t] for t in texts]

    async def _request(self, texts: list[str], source_lang: str, context: str | None) -> list[str] | None:
        try:
            # One overall limit covering connect and read; no retry.
            async with asyncio.timeout(self._timeout):
                response = await self._send(texts, source_lang, context)
            response.raise_for_status()
            out = self._parse(response)
        except TimeoutError:
            log.warning("%s request timed out after %ss", self.name, self._timeout)
            return None
        except httpx.HTTPStatusError as exc:
            log.warning("%s request failed: HTTP %s", self.name, exc.response.status_code)
            return None
        except httpx.TimeoutException as exc:
            log.warning("%s request timed out: %s", self.name, type(exc).__name__)
            return None
        except httpx.HTTPError as exc:
            log.warning("%s request failed: %s", self.name, type(exc).__name__)
            return None
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            log.warning("%s returned an unexpected response: %s", self.name, type(exc).__name__)
            return None
        if len(out) != len(texts):
            log.warning("%s returned %d translations for %d texts", self.name, len(out), len(texts))
            return None
        return out

    async def _send(self, texts: list[str], source_lang: str, context: str | None) -> httpx.Response:
        raise NotImplementedError

    def _parse(self, response: httpx.Response) -> list[str]:
        raise NotImplementedError

    async def aclose(self) -> None:
        await self._client.aclose()


class DeepLTranslator(CachedTranslator):
    name = "DeepL"

    def __init__(self, api_key: SecretStr, *args: Any, **kwargs: Any) -> None:
        super().__init__(api_key, *args, **kwargs)
        # Free API keys end with ":fx".
        self._endpoint = FREE_ENDPOINT if api_key.get_secret_value().endswith(":fx") else PRO_ENDPOINT

    async def _send(self, texts: list[str], source_lang: str, context: str | None) -> httpx.Response:
        body: dict[str, Any] = {"text": texts, "source_lang": source_lang, "target_lang": self.target_lang}
        if context:
            body["context"] = context
        headers = {"Authorization": f"DeepL-Auth-Key {self._key.get_secret_value()}"}
        return await self._client.post(self._endpoint, json=body, headers=headers, timeout=self._timeout)

    def _parse(self, response: httpx.Response) -> list[str]:
        return [item["text"] for item in response.json()["translations"]]


class GeminiTranslator(CachedTranslator):
    name = "Gemini"

    def __init__(
        self, api_key: SecretStr, model: str, *args: Any, target_name: str = "", **kwargs: Any
    ) -> None:
        super().__init__(api_key, *args, **kwargs)
        self.target = f"{self.target_lang}:gemini"
        self._target_name = target_name or _TARGET_NAMES.get(self.target_lang.upper(), self.target_lang)
        self._endpoint = GEMINI_ENDPOINT.format(model=model)

    async def _send(self, texts: list[str], source_lang: str, context: str | None) -> httpx.Response:
        language = _LANGUAGE_NAMES.get(source_lang, source_lang)
        prompt = (f"Context: {context}\n" if context else "") + json.dumps(texts, ensure_ascii=False)
        body = {
            "systemInstruction": {
                "parts": [{"text": GEMINI_INSTRUCTION.format(language=language, target=self._target_name)}]
            },
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": {"type": "ARRAY", "items": {"type": "STRING"}},
            },
        }
        # The key goes in a header, never in the URL, so it cannot leak through request logging.
        headers = {"x-goog-api-key": self._key.get_secret_value()}
        return await self._client.post(self._endpoint, json=body, headers=headers, timeout=self._timeout)

    def _parse(self, response: httpx.Response) -> list[str]:
        text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        out = json.loads(text)
        if not isinstance(out, list) or not all(isinstance(t, str) for t in out):
            raise TypeError("expected a JSON array of strings")
        return out


def is_set(key: SecretStr | None) -> bool:
    return key is not None and bool(key.get_secret_value().strip())


def is_english(target: str) -> bool:
    """DeepL takes EN-US / EN-GB as English targets."""
    return target.upper().split("-")[0] == "EN"


def make_translator(
    *,
    deepl_key: SecretStr | None,
    deepl_timeout: float,
    gemini_key: SecretStr | None,
    gemini_model: str,
    gemini_timeout: float,
    cache: TranslationCache | None,
    thread: Worker | None,
    target: str = DEFAULT_TARGET,
    target_name: str = "",
) -> CachedTranslator | None:
    """None (English only, no requests at all) when neither DEEPL_API_KEY nor GEMINI_API_KEY is set."""
    if is_set(deepl_key) and is_set(gemini_key):
        raise ValueError("set only one of DEEPL_API_KEY and GEMINI_API_KEY")
    if not is_set(deepl_key) and not is_set(gemini_key):
        return None
    if cache is None or thread is None:
        raise ValueError("a translation cache and the lookup thread are required when translation is enabled")
    if gemini_key is not None and is_set(gemini_key):
        return GeminiTranslator(
            gemini_key, gemini_model, gemini_timeout, cache, thread, target=target, target_name=target_name
        )
    assert deepl_key is not None
    return DeepLTranslator(deepl_key, deepl_timeout, cache, thread, target=target)
