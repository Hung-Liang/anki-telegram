import asyncio
import json
import logging

import httpx
import pytest
from pydantic import SecretStr

from ankibot.threads import DedicatedThread
from ankibot.translator import (
    FREE_ENDPOINT,
    PRO_ENDPOINT,
    DeepLTranslator,
    GeminiTranslator,
    TranslationCache,
    is_english,
    make_translator,
)

FREE_KEY = "secret-key-123:fx"
PRO_KEY = "secret-pro-456"


class Server:
    def __init__(self):
        self.requests = []
        self.responses = []  # httpx.Response, Exception, or "hang"

    async def handler(self, request):
        self.requests.append(request)
        item = self.responses.pop(0) if self.responses else None
        if item == "hang":
            await asyncio.sleep(10)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, httpx.Response):
            return item
        body = json.loads(request.content)
        return httpx.Response(200, json={"translations": [{"text": f"中:{t}"} for t in body["text"]]})


@pytest.fixture
async def make(tmp_path):
    thread = DedicatedThread("lookup-translator-test")
    caches = []

    async def _make(key=FREE_KEY, timeout=1.0, **kwargs):
        server = Server()
        cache = await thread.run(TranslationCache, tmp_path / "translations.db")
        caches.append(cache)
        client = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
        translator = DeepLTranslator(SecretStr(key), timeout, cache, thread, client=client, **kwargs)
        return translator, server

    yield _make
    for cache in caches:
        await thread.run(cache.close)
    thread.shutdown()


async def test_all_senses_in_one_request_in_order(make):
    translator, server = await make()
    out = await translator.translate(["to eat", "to live on"], "EN", context="食べる（たべる）")
    assert out == ["中:to eat", "中:to live on"]
    assert len(server.requests) == 1
    request = server.requests[0]
    body = json.loads(request.content)
    assert body == {
        "text": ["to eat", "to live on"],
        "source_lang": "EN",
        "target_lang": "ZH-HANT",
        "context": "食べる（たべる）",
    }
    assert request.headers["Authorization"] == f"DeepL-Auth-Key {FREE_KEY}"


async def test_free_and_pro_endpoints(make):
    free, free_server = await make(FREE_KEY)
    await free.translate(["a"], "EN")
    assert str(free_server.requests[0].url) == FREE_ENDPOINT
    pro, pro_server = await make(PRO_KEY)
    await pro.translate(["b"], "EN")
    assert str(pro_server.requests[0].url) == PRO_ENDPOINT


async def test_cache_hit_sends_no_request(make):
    translator, server = await make()
    await translator.translate(["to eat"], "EN")
    out = await translator.translate(["to eat"], "EN", context="other context")
    assert out == ["中:to eat"]
    assert len(server.requests) == 1


async def test_partial_cache_only_requests_missing(make):
    translator, server = await make()
    await translator.translate(["to eat"], "EN")
    out = await translator.translate(["to eat", "to drink"], "EN")
    assert out == ["中:to eat", "中:to drink"]
    assert json.loads(server.requests[1].content)["text"] == ["to drink"]


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(500, json={"message": "oops"}),
        httpx.Response(456, json={"message": "Quota exceeded"}),
        httpx.ConnectTimeout("slow"),
        "hang",
        httpx.Response(200, json={"unexpected": True}),
    ],
)
async def test_failures_return_none_and_are_not_cached(make, failure, caplog):
    translator, server = await make(timeout=0.05)
    server.responses = [failure]
    with caplog.at_level(logging.WARNING):
        assert await translator.translate(["to eat"], "EN") is None
    assert "DeepL" in caplog.text
    assert FREE_KEY not in caplog.text and "secret-key" not in caplog.text
    assert await translator.translate(["to eat"], "EN") == ["中:to eat"]
    assert len(server.requests) == 2


async def test_http_status_is_logged(make, caplog):
    translator, server = await make()
    server.responses = [httpx.Response(456, json={"message": "Quota exceeded"})]
    with caplog.at_level(logging.WARNING):
        await translator.translate(["x"], "EN")
    assert "456" in caplog.text


def factory(deepl=None, gemini=None, cache=None, thread=None):
    return make_translator(
        deepl_key=deepl,
        deepl_timeout=10,
        gemini_key=gemini,
        gemini_model="gemini-test",
        gemini_timeout=10,
        cache=cache,
        thread=thread,
    )


@pytest.mark.parametrize("key", [None, SecretStr(""), SecretStr("   ")])
def test_no_key_means_no_translator(key, tmp_path):
    assert factory(deepl=key, gemini=key) is None


def test_both_keys_is_an_error():
    with pytest.raises(ValueError, match="only one"):
        factory(deepl=SecretStr(FREE_KEY), gemini=SecretStr(GEMINI_KEY))


async def test_factory_picks_the_configured_provider(tmp_path):
    thread = DedicatedThread("lookup-factory-test")
    cache = await thread.run(TranslationCache, tmp_path / "translations.db")
    try:
        gemini = factory(gemini=SecretStr(GEMINI_KEY), cache=cache, thread=thread)
        deepl = factory(deepl=SecretStr(FREE_KEY), cache=cache, thread=thread)
        assert isinstance(gemini, GeminiTranslator) and isinstance(deepl, DeepLTranslator)
        await gemini.aclose()
        await deepl.aclose()
    finally:
        await thread.run(cache.close)
        thread.shutdown()


GEMINI_KEY = "gemini-secret-789"


def gemini_reply(items):
    text = json.dumps(items, ensure_ascii=False)
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})


class GeminiServer:
    def __init__(self):
        self.requests = []
        self.responses = []

    async def handler(self, request):
        self.requests.append(request)
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        body = json.loads(request.content)
        items = json.loads(body["contents"][0]["parts"][0]["text"].splitlines()[-1])
        return gemini_reply([f"中:{t}" for t in items])


@pytest.fixture
async def make_gemini(tmp_path):
    thread = DedicatedThread("lookup-gemini-test")
    caches = []

    async def _make(timeout=1.0, **kwargs):
        server = GeminiServer()
        cache = await thread.run(TranslationCache, tmp_path / "translations.db")
        caches.append(cache)
        client = httpx.AsyncClient(transport=httpx.MockTransport(server.handler))
        translator = GeminiTranslator(
            SecretStr(GEMINI_KEY), "gemini-test", timeout, cache, thread, client=client, **kwargs
        )
        return translator, server

    yield _make
    for cache in caches:
        await thread.run(cache.close)
    thread.shutdown()


async def test_gemini_request_shape(make_gemini):
    translator, server = await make_gemini()
    out = await translator.translate(["to eat", "to live on"], "EN", "食べる（たべる）")
    assert out == ["中:to eat", "中:to live on"]
    request = server.requests[0]
    assert request.url.path == "/v1beta/models/gemini-test:generateContent"
    assert request.headers["x-goog-api-key"] == GEMINI_KEY
    assert GEMINI_KEY not in str(request.url)
    body = json.loads(request.content)
    prompt = body["contents"][0]["parts"][0]["text"]
    assert prompt.startswith("Context: 食べる（たべる）\n")
    assert "English" in body["systemInstruction"]["parts"][0]["text"]
    assert body["generationConfig"]["responseMimeType"] == "application/json"


async def test_gemini_japanese_source_without_context(make_gemini):
    translator, server = await make_gemini()
    assert await translator.translate(["猫だ！"], "JA") == ["中:猫だ！"]
    body = json.loads(server.requests[0].content)
    assert not body["contents"][0]["parts"][0]["text"].startswith("Context:")
    assert "Japanese" in body["systemInstruction"]["parts"][0]["text"]


async def test_gemini_cache_is_separate_from_deepl(make, make_gemini):
    deepl, _ = await make()
    assert await deepl.translate(["to eat"], "EN") == ["中:to eat"]
    gemini, server = await make_gemini()
    await gemini.translate(["to eat"], "EN")
    assert len(server.requests) == 1
    await gemini.translate(["to eat"], "EN")
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}}),
        gemini_reply(["only one"]),
        httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}),
        httpx.Response(200, json={"candidates": [{"finishReason": "SAFETY"}]}),
        httpx.Response(200, json={"candidates": []}),
        gemini_reply([1, 2]),
        httpx.ConnectTimeout("slow"),
    ],
)
async def test_gemini_failures_return_none_and_are_not_cached(make_gemini, failure, caplog):
    translator, server = await make_gemini()
    server.responses = [failure]
    with caplog.at_level(logging.WARNING):
        assert await translator.translate(["a", "b"], "EN") is None
    assert "Gemini" in caplog.text
    assert GEMINI_KEY not in caplog.text
    assert await translator.translate(["a", "b"], "EN") == ["中:a", "中:b"]


async def test_gemini_rate_limit_status_is_logged(make_gemini, caplog):
    translator, server = await make_gemini()
    server.responses = [httpx.Response(429, json={})]
    with caplog.at_level(logging.WARNING):
        await translator.translate(["x"], "EN")
    assert "HTTP 429" in caplog.text


async def test_deepl_target_is_configurable_and_cached_separately(make):
    zh, _ = await make()
    await zh.translate(["to eat"], "EN")
    ko, server = await make(target="KO")
    await ko.translate(["to eat"], "EN")
    assert json.loads(server.requests[0].content)["target_lang"] == "KO"


async def test_gemini_target_name_goes_into_the_prompt(make_gemini):
    default, server = await make_gemini()
    await default.translate(["猫"], "JA")
    assert (
        "Traditional Chinese as used in Taiwan"
        in json.loads(server.requests[0].content)["systemInstruction"]["parts"][0]["text"]
    )
    named, server = await make_gemini(target="KO", target_name="Korean")
    await named.translate(["猫"], "JA")
    assert len(server.requests) == 1
    assert "into Korean." in json.loads(server.requests[0].content)["systemInstruction"]["parts"][0]["text"]
    coded, server = await make_gemini(target="DE")
    await coded.translate(["犬"], "JA")
    assert "into DE." in json.loads(server.requests[0].content)["systemInstruction"]["parts"][0]["text"]


@pytest.mark.parametrize(
    ("target", "english"), [("EN", True), ("en-us", True), ("EN-GB", True), ("ZH-HANT", False)]
)
def test_is_english(target, english):
    assert is_english(target) is english


async def test_factory_passes_the_target(tmp_path):
    thread = DedicatedThread("lookup-factory-target-test")
    cache = await thread.run(TranslationCache, tmp_path / "translations.db")
    try:
        translator = make_translator(
            deepl_key=SecretStr(FREE_KEY),
            deepl_timeout=10,
            gemini_key=None,
            gemini_model="gemini-test",
            gemini_timeout=10,
            cache=cache,
            thread=thread,
            target="EN-US",
        )
        assert translator is not None
        assert translator.target_lang == "EN-US" and not translator.translates_glosses
        await translator.aclose()
    finally:
        await thread.run(cache.close)
        thread.shutdown()
