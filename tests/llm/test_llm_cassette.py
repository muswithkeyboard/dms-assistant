"""Воспроизведение кассеты, записанной на живой Ollama qwen3:8b (свой вопрос без текстов комплекта).

Запись: DMS_LLM_PROVIDER=openai_compat DMS_LLM_MODEL=qwen3:8b DMS_LLM_BASE_URL=http://127.0.0.1:11434/v1
DMS_LLM_RECORD=1 — этот же запрос через get_llm(). Тест работает без сети.
"""
from __future__ import annotations

from llm_fakes import patched_env

from dms_core.config import REPO_ROOT
from dms_core.llm import Message, ToolSpec, get_llm

MAX_TOKENS = 2048
TOOLS = [ToolSpec("date_calc", "Посчитать дату: к дате start прибавить days дней. Арифметику дат делай только "
                               "этим инструментом.",
                  {"type": "object", "properties": {"start": {"type": "string", "description": "дата ГГГГ-ММ-ДД"},
                                                    "days": {"type": "integer"}}, "required": ["start", "days"]})]
MESSAGES = [Message("system", "Ты помощник. Для расчёта дат вызывай инструмент."),
            Message("user", "Какая дата будет через 45 дней после 2026-03-10?")]


def test_replay_recorded_ollama_tool_call():
    with patched_env(DMS_LLM_PROVIDER="mock", DMS_LLM_CASSETTES=str(REPO_ROOT / "eval" / "cassettes"),
                     DMS_LLM_RECORD=None):
        turn = get_llm().chat(MESSAGES, TOOLS, max_tokens=MAX_TOKENS)
    assert turn.model == "qwen3:8b" and turn.stop_reason == "tool_calls"
    assert [c.name for c in turn.tool_calls] == ["date_calc"]
    assert turn.tool_calls[0].args == {"start": "2026-03-10", "days": 45}
