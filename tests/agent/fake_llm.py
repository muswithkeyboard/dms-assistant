"""Фейковая модель для тестов агента без сети: отвечает по сценарию ходов и записывает каждый запрос.

Ход сценария — LlmTurn или функция (messages, tools, json_schema) → LlmTurn (чтобы ответ зависел от результатов
инструментов). Сценарий кончился — LlmError (как провайдер без кассеты).
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from dms_core.llm.base import LlmError, LlmTurn, Message, ToolCall, ToolSpec, Usage

Step = LlmTurn | Callable[[list[Message], list[ToolSpec] | None, dict[str, Any] | None], LlmTurn]


@dataclass
class Call:
    messages: list[Message]
    tools: list[ToolSpec] | None
    json_schema: dict[str, Any] | None


@dataclass
class FakeLLM:
    script: list[Step]
    provider: str = "fake"
    model: str = "fake-1"
    calls: list[Call] = field(default_factory=list)

    def chat(self, messages: list[Message], tools: list[ToolSpec] | None = None, *,
             json_schema: dict[str, Any] | None = None, max_tokens: int = 4096) -> LlmTurn:
        self.calls.append(Call(list(messages), tools, json_schema))
        if not self.script:
            raise LlmError("сценарий фейковой модели закончился")
        step = self.script.pop(0)
        return step(messages, tools, json_schema) if callable(step) else step


_n = 0


def tool(tool_name: str, /, **args: Any) -> LlmTurn:
    """Ход «вызвать инструмент»."""
    global _n
    _n += 1
    return LlmTurn(text="", tool_calls=[ToolCall(id=f"call{_n}", name=tool_name, args=args)], usage=Usage(10, 5),
                   model="fake-1")


def tools(*calls: tuple[str, dict[str, Any]]) -> LlmTurn:
    """Ход с несколькими вызовами сразу."""
    global _n
    out = []
    for name, args in calls:
        _n += 1
        out.append(ToolCall(id=f"call{_n}", name=name, args=args))
    return LlmTurn(text="", tool_calls=out, usage=Usage(10, 5), model="fake-1")


def final(obj: dict[str, Any] | str) -> LlmTurn:
    """Ход «итоговый ответ» (dict → JSON; строка — как есть, для невалидного JSON)."""
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return LlmTurn(text=text, tool_calls=[], usage=Usage(10, 20), model="fake-1")


def tool_results(messages: list[Message]) -> list[dict[str, Any]]:
    """Результаты инструментов, которые агент вернул модели (role=tool)."""
    return [json.loads(m.content) for m in messages if m.role == "tool"]
