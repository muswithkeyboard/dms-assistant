"""Кассеты mock, повторы при 429/5xx, ошибки по-русски, отсутствие ключей и текстов в журнале; get_llm, цены."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from llm_fakes import (
    DOC_TEXT,
    SECRET,
    FakeAnthropic,
    FakeGigaChat,
    FakeOpenAI,
    Scripted,
    anthropic_status,
    anthropic_text,
    captured_logs,
    gigachat_status,
    no_sleep,
    openai_connection,
    openai_status,
    openai_text,
    patched_env,
)

from dms_core.llm import LlmError, Message, ToolSpec, Usage, get_llm
from dms_core.llm.anthropic import AnthropicLLM
from dms_core.llm.gigachat import GigaChatLLM
from dms_core.llm.mock import MockLLM, request_key
from dms_core.llm.openai_compat import OpenAICompatLLM
from dms_core.llm.pricing import cost_usd
from dms_core.llm.prompts import load

LOCAL = "http://127.0.0.1:11434/v1"


def _raises(fn) -> LlmError:
    try:
        fn()
    except LlmError as e:
        return e
    raise AssertionError("ожидалась LlmError")


# --- кассеты ---------------------------------------------------------------------------------------------------

def test_cassette_key_is_canonical_and_order_independent():
    a = request_key([Message("user", "x")], [ToolSpec("t", "d", {"b": 1, "a": 2})], {"type": "object"}, 100)
    b = request_key([Message("user", "x")], [ToolSpec("t", "d", {"a": 2, "b": 1})], {"type": "object"}, 100)
    c = request_key([Message("user", "y")], None, None, 100)
    assert a == b and a != c and len(a) == 64


def test_mock_records_then_replays_without_network():
    with tempfile.TemporaryDirectory() as d:
        script = Scripted(openai_text("ответ модели"))
        live = OpenAICompatLLM("qwen3:8b", base_url=LOCAL, client=FakeOpenAI(script))
        rec = MockLLM(d, inner=live)
        msgs = [Message("user", "вопрос")]
        t1 = rec.chat(msgs, max_tokens=50)
        files = list(Path(d).glob("*.json"))
        assert len(files) == 1 and files[0].stem == request_key(msgs, None, None, 50)
        data = json.loads(files[0].read_text(encoding="utf-8"))
        assert data["provider"] == "openai_compat" and "api_key" not in json.dumps(data)

        replay = MockLLM(d)                       # без живого провайдера
        t2 = replay.chat(msgs, max_tokens=50)
        assert t2.text == t1.text == "ответ модели" and t2.model == "qwen3:8b"
        assert len(script.calls) == 1             # второй раз сеть (фейк) не вызывалась
        e = _raises(lambda: replay.chat([Message("user", "другое")], max_tokens=50))
        assert "нет кассеты" in str(e)


def test_get_llm_selection():
    with tempfile.TemporaryDirectory() as d:
        with patched_env(DMS_LLM_PROVIDER=None):
            assert get_llm() is None
        with patched_env(DMS_LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY=None):
            assert get_llm() is None                                  # нет ключа — без модели
        with patched_env(DMS_LLM_PROVIDER="mock", DMS_LLM_CASSETTES=d):
            m = get_llm()
            assert isinstance(m, MockLLM) and m.dir == Path(d)
        with patched_env(DMS_LLM_PROVIDER="openai_compat", DMS_LLM_BASE_URL=LOCAL, DMS_LLM_MODEL="qwen3:8b",
                         OPENAI_API_KEY=None, DMS_LLM_RECORD="1", DMS_LLM_CASSETTES=d):
            m = get_llm()
            assert isinstance(m, MockLLM) and m.inner.provider == "openai_compat"
        with patched_env(DMS_LLM_PROVIDER="gigachat", GIGACHAT_CREDENTIALS=None):
            assert get_llm() is None
        e = _raises(lambda: get_llm("nope"))
        assert "Неизвестный провайдер" in str(e)


# --- повторы и ошибки ------------------------------------------------------------------------------------------

def test_retry_on_429_and_5xx_then_success():
    script = Scripted(openai_status(429), openai_status(503), openai_connection(), openai_text("ок"))
    llm = OpenAICompatLLM("qwen3:8b", base_url=LOCAL, client=FakeOpenAI(script))
    with no_sleep() as pauses:
        turn = llm.chat([Message("user", "?")])
    assert turn.text == "ок" and len(script.calls) == 4 and len(pauses) == 3
    assert pauses[0] < pauses[1] < pauses[2]                         # экспоненциальная пауза


def test_retry_gives_up_after_three():
    script = Scripted(*[anthropic_status(500)] * 5)
    llm = AnthropicLLM("claude-opus-5-5", client=FakeAnthropic(script))
    with no_sleep():
        e = _raises(lambda: llm.chat([Message("user", "?")]))
    assert len(script.calls) == 4                                    # 1 + 3 повтора
    assert "ошибка на стороне провайдера" in str(e) and "HTTP 500" in str(e) and "попыток: 4" in str(e)
    assert e.__cause__ is None and e.__suppress_context__


def test_no_retry_on_auth_and_bad_request():
    for exc, words in ((anthropic_status(401), "ключ не принят"), (anthropic_status(400), "отклонил запрос")):
        script = Scripted(exc, anthropic_text("never"))
        llm = AnthropicLLM("claude-opus-5-5", client=FakeAnthropic(script))
        with no_sleep():
            e = _raises(lambda llm=llm: llm.chat([Message("user", "?")]))
        assert len(script.calls) == 1 and words in str(e)


def test_unexpected_response_shape():
    script = Scripted(object())
    llm = OpenAICompatLLM("qwen3:8b", base_url=LOCAL, client=FakeOpenAI(script))
    e = _raises(lambda: llm.chat([Message("user", "?")]))
    assert "неожиданного вида" in str(e)


# --- безопасность журнала и ошибок -----------------------------------------------------------------------------

def test_logs_and_errors_have_no_key_or_document_text():
    msgs = [Message("system", "Роль"), Message("user", DOC_TEXT)]
    with captured_logs() as lines, no_sleep():
        ok = OpenAICompatLLM("qwen3:8b", api_key=SECRET, base_url=LOCAL,
                             client=FakeOpenAI(Scripted(openai_text(DOC_TEXT))))
        ok.chat(msgs)
        # тело ответа GigaChat попадает в str() исключения SDK — наружу оно не должно уйти
        bad = GigaChatLLM(credentials=SECRET, client=FakeGigaChat(Scripted(gigachat_status(401, DOC_TEXT))))
        e1 = _raises(lambda: bad.chat(msgs))
        bad2 = AnthropicLLM(api_key=SECRET, client=FakeAnthropic(Scripted(*[anthropic_status(429, DOC_TEXT)] * 4)))
        e2 = _raises(lambda: bad2.chat(msgs))
    blob = "\n".join(lines) + str(e1) + str(e2)
    assert lines and any("in=200 out=50" in s for s in lines)        # метаданные в журнале есть
    for secret in (SECRET, DOC_TEXT, "Роль", "AIR FLOW"):
        assert secret not in blob
    assert "ключ не принят" in str(e1) and "лимит запросов" in str(e2)


# --- промпты и цены --------------------------------------------------------------------------------------------

def test_prompts_load_with_version():
    text, ver = load("json_output")
    assert ver != "unknown" and "{{schema}}" in text and not text.startswith("<!--")
    _, sys_ver = load("system")
    assert sys_ver == "0.1"
    try:
        load("../secret")
        raise AssertionError("ожидалась ValueError")
    except ValueError:
        pass


def test_pricing():
    assert cost_usd("claude-opus-5-5", Usage(1_000_000, 100_000)) == 6.0
    assert cost_usd("qwen3:8b", Usage(10, 10)) == 0.0
    assert cost_usd("unknown-model", Usage(10, 10)) is None
