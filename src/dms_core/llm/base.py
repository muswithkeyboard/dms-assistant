"""Интерфейс модели (ADR 0002, docs/CONTRACTS.md §7). Весь доступ к LLM — только через dms_core.llm.

Провайдер выбирается переменными DMS_LLM_PROVIDER (anthropic | openai_compat | gigachat | mock), DMS_LLM_MODEL,
DMS_LLM_BASE_URL; ключи — в .env, читаются только провайдерами. Температура 0. Тексты документов уходят провайдеру
только как результаты инструментов одного договора (ADR 0005); в журнал не пишутся.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class Message:
    """Сообщение диалога в нейтральном формате; провайдер переводит в свой."""
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)   # role=assistant: что модель вызвала
    tool_call_id: str | None = None                             # role=tool: на какой вызов ответ
    name: str | None = None                                     # role=tool: имя инструмента


@dataclass
class ToolSpec:
    """Описание инструмента для модели: имя, описание по-русски, JSON Schema параметров (без contract_id)."""
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LlmTurn:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    model: str
    stop_reason: str = ""


class LlmError(RuntimeError):
    """Сбой провайдера (сеть, лимит, неверный ответ). Сообщение — по-русски, без текстов документов и ключей."""


class LLM(Protocol):
    provider: str
    model: str

    def chat(self, messages: list[Message], tools: list[ToolSpec] | None = None, *,
             json_schema: dict[str, Any] | None = None, max_tokens: int = 4096) -> LlmTurn:
        """Один ход модели. json_schema — требовать итоговый ответ строго по схеме (если провайдер умеет;
        иначе — инструкция в промпте и проверка в agent.py)."""
        ...
