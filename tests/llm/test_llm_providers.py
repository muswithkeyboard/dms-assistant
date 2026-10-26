"""Перевод нейтральных Message/ToolSpec в форматы API и разбор ответов — на придуманных образцах, без сети."""
from __future__ import annotations

import json

from llm_fakes import (
    FakeAnthropic,
    FakeGigaChat,
    FakeOpenAI,
    Scripted,
    anthropic_text,
    anthropic_tool_use,
    gigachat_function_call,
    gigachat_text,
    openai_text,
    openai_tool_call,
    patched_env,
)

from dms_core.llm import Message, ToolCall, ToolSpec
from dms_core.llm.anthropic import AnthropicLLM, to_anthropic_messages
from dms_core.llm.gigachat import GigaChatLLM, to_gigachat_messages
from dms_core.llm.openai_compat import OpenAICompatLLM, to_openai_messages

TOOLS = [ToolSpec("search_clauses", "Поиск пунктов договора",
                  {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})]
SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}


def _dialog() -> list[Message]:
    call_a = ToolCall("id_a", "search_clauses", {"query": "чистка"})
    call_b = ToolCall("id_b", "search_clauses", {"query": "налёт"})
    return [Message("system", "Роль."), Message("user", "Вопрос?"),
            Message("assistant", "Ищу.", tool_calls=[call_a, call_b]),
            Message("tool", '{"hits": 1}', tool_call_id="id_a", name="search_clauses"),
            Message("tool", '{"hits": 0}', tool_call_id="id_b", name="search_clauses")]


# --- Anthropic -------------------------------------------------------------------------------------------------

def test_anthropic_messages_translation():
    out = to_anthropic_messages(_dialog())
    assert [m["role"] for m in out] == ["user", "assistant", "user"]       # system — отдельный параметр
    assert out[1]["content"][1] == {"type": "tool_use", "id": "id_a", "name": "search_clauses",
                                    "input": {"query": "чистка"}}
    # оба результата параллельных вызовов — в одном user-сообщении
    assert [b["tool_use_id"] for b in out[2]["content"]] == ["id_a", "id_b"]
    assert all(b["type"] == "tool_result" for b in out[2]["content"])


def test_anthropic_tool_call_roundtrip_keeps_thinking_blocks():
    script = Scripted(anthropic_tool_use(), anthropic_text('{"summary": "есть"}'))
    llm = AnthropicLLM("claude-opus-5-5", client=FakeAnthropic(script))
    with patched_env(DMS_LLM_STRUCTURED=None):
        turn = llm.chat([Message("system", "Роль."), Message("user", "Чистка?")], TOOLS)
        assert turn.stop_reason == "tool_use"
        assert turn.tool_calls == [ToolCall("toolu_1", "search_clauses", {"query": "чистка зубов"})]
        assert turn.text == "Ищу пункт."
        assert (turn.usage.input_tokens, turn.usage.output_tokens) == (120, 40)
        p1 = script.calls[0]
        assert p1["system"] == "Роль." and p1["tools"][0]["input_schema"]["required"] == ["query"]
        assert "temperature" not in p1                                     # у текущих моделей запрещена

        msgs = [Message("system", "Роль."), Message("user", "Чистка?"),
                Message("assistant", turn.text, tool_calls=turn.tool_calls),
                Message("tool", '{"hits": []}', tool_call_id="toolu_1", name="search_clauses")]
        final = llm.chat(msgs, TOOLS, json_schema=SCHEMA)
    assert json.loads(final.text) == {"summary": "есть"}
    p2 = script.calls[1]
    # блок thinking с подписью возвращён в историю без изменений
    assert p2["messages"][1]["content"][0] == {"type": "thinking", "thinking": "", "signature": "sig-abc"}
    fmt = p2["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["additionalProperties"] is False


# --- OpenAI-совместимый ----------------------------------------------------------------------------------------

def test_openai_messages_translation():
    out = to_openai_messages(_dialog(), "ИТОГ JSON")
    assert out[0] == {"role": "system", "content": "Роль.\n\nИТОГ JSON"}
    tc = out[2]["tool_calls"][0]
    assert tc["type"] == "function" and json.loads(tc["function"]["arguments"]) == {"query": "чистка"}
    assert out[3] == {"role": "tool", "tool_call_id": "id_a", "content": '{"hits": 1}'}


def test_openai_tool_call_and_json_by_instruction_for_local_server():
    script = Scripted(openai_tool_call(), openai_text('<think>рассуждение</think>\n```json\n{"summary": "да"}\n```'))
    llm = OpenAICompatLLM("qwen3:8b", base_url="http://127.0.0.1:11434/v1", client=FakeOpenAI(script))
    with patched_env(DMS_LLM_STRUCTURED=None, DMS_LLM_TEMPERATURE=None):
        turn = llm.chat([Message("user", "Чистка?")], TOOLS)
        final = llm.chat([Message("user", "Чистка?")], TOOLS, json_schema=SCHEMA)
    assert turn.tool_calls == [ToolCall("call_1", "search_clauses", {"query": "чистка зубов"})]
    p1 = script.calls[0]
    assert p1["temperature"] == 0 and p1["max_tokens"] == 4096 and p1["tools"][0]["type"] == "function"
    assert final.text == '{"summary": "да"}'
    p2 = script.calls[1]
    assert "response_format" not in p2                       # локальный сервер: инструкция, а не формат
    assert '"summary"' in p2["messages"][0]["content"]       # схема в системной инструкции


def test_openai_official_uses_response_format():
    script = Scripted(openai_text('{"summary": "да"}'))
    llm = OpenAICompatLLM("some-model", api_key="x", client=FakeOpenAI(script))
    with patched_env(DMS_LLM_STRUCTURED=None):
        llm.chat([Message("user", "?")], json_schema=SCHEMA)
    p = script.calls[0]
    assert p["response_format"]["type"] == "json_schema" and "max_completion_tokens" in p


def test_openai_invalid_arguments_and_unknown_tool_are_kept():
    script = Scripted(openai_tool_call(arguments="{query: oops", name="delete_everything"))
    llm = OpenAICompatLLM("qwen3:8b", base_url="http://127.0.0.1:11434/v1", client=FakeOpenAI(script))
    turn = llm.chat([Message("user", "?")], TOOLS)
    assert turn.tool_calls[0].name == "delete_everything"
    assert "__error__" in turn.tool_calls[0].args


# --- GigaChat --------------------------------------------------------------------------------------------------

def test_gigachat_messages_translation():
    out = to_gigachat_messages(_dialog()[:4] + [Message("tool", "не JSON", tool_call_id="x", name="search_clauses")])
    assert out[2]["function_call"] == {"name": "search_clauses", "arguments": {"query": "чистка"}}
    assert out[2]["functions_state_id"] == "id_a"
    assert out[3] == {"role": "function", "name": "search_clauses", "content": '{"hits": 1}'}
    assert json.loads(out[4]["content"]) == {"result": "не JSON"}


def test_gigachat_function_call_and_json():
    script = Scripted(gigachat_function_call(), gigachat_text('Ответ: {"summary": "нет"}'))
    llm = GigaChatLLM(client=FakeGigaChat(script))
    with patched_env(DMS_LLM_STRUCTURED=None, DMS_LLM_TEMPERATURE=None):
        turn = llm.chat([Message("user", "Чистка?")], TOOLS)
        final = llm.chat([Message("user", "Чистка?")], json_schema=SCHEMA)
    assert turn.tool_calls == [ToolCall("state-77", "search_clauses", {"query": "чистка"})]
    p = script.calls[0]
    assert p["function_call"] == "auto" and p["functions"][0]["name"] == "search_clauses"
    assert p["temperature"] == 0
    assert final.text == '{"summary": "нет"}'


# --- общий разбор JSON -----------------------------------------------------------------------------------------

def test_invalid_json_gets_one_repair_then_marked():
    script = Scripted(openai_text("не знаю"), openai_text("всё ещё текст"))
    llm = OpenAICompatLLM("qwen3:8b", base_url="http://127.0.0.1:11434/v1", client=FakeOpenAI(script))
    turn = llm.chat([Message("user", "?")], json_schema=SCHEMA)
    assert turn.stop_reason == "invalid_json" and turn.text == "всё ещё текст"
    assert len(script.calls) == 2                                   # ровно один ремонт
    assert script.calls[1]["messages"][-1]["role"] == "user"        # просьба вернуть только JSON
    assert turn.usage.input_tokens == 400                           # токены обоих вызовов


def test_repair_succeeds():
    script = Scripted(openai_text('{"other": 1}'), openai_text('{"summary": "ок"}'))
    llm = OpenAICompatLLM("qwen3:8b", base_url="http://127.0.0.1:11434/v1", client=FakeOpenAI(script))
    turn = llm.chat([Message("user", "?")], json_schema=SCHEMA)
    assert json.loads(turn.text) == {"summary": "ок"} and turn.stop_reason == "stop"
