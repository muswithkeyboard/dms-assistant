"""OpenAI-совместимый провайдер: OpenAI, YandexGPT, vLLM, Ollama (`/v1`) — по base_url (ADR 0002).

Перевод (сверено с SDK openai 3.x: resources/chat/completions/completions.py,
types/chat/chat_completion_message_function_tool_call.py):
- system/user → `{"role", "content"}`;
- assistant с вызовами → `{"role": "assistant", "content", "tool_calls": [{"id", "type": "function",
  "function": {"name", "arguments": "<JSON-строка>"}}]}`;
- tool → `{"role": "tool", "tool_call_id", "content"}`;
- инструменты → `{"type": "function", "function": {"name", "description", "parameters"}}`;
- аргументы вызова приходят строкой JSON → `safe_args` (не JSON → {"__error__": ...}).
Итог по схеме: `response_format={"type": "json_schema", ...}` — только у официального OpenAI (режим auto);
у локальных серверов (Ollama и др.) ограничение формата мешает модели вызывать инструменты, поэтому там —
инструкция config/prompts/json_output.md и извлечение JSON из текста. DMS_LLM_STRUCTURED=on|off меняет выбор.
"""
from __future__ import annotations

import json
from typing import Any

from ._common import (
    BaseProvider,
    Failure,
    LlmError,
    LlmTurn,
    Message,
    Prepared,
    ToolCall,
    ToolSpec,
    Usage,
    env,
    parse_retry_after,
    safe_args,
    schema_instruction,
    status_failure,
    strip_think,
    structured_mode,
    temperature,
    timeout_s,
)


def is_official_openai(base_url: str | None) -> bool:
    return not base_url or "api.openai.com" in base_url


class OpenAICompatLLM(BaseProvider):
    provider = "openai_compat"

    def __init__(self, model: str, *, api_key: str | None = None, base_url: str | None = None,
                 client: Any = None) -> None:
        self.model = model
        self.base_url = base_url or None
        if client is None:
            import openai

            client = openai.OpenAI(api_key=api_key or "not-needed", base_url=self.base_url,
                                   timeout=timeout_s(), max_retries=0)
        self._client = client

    def _prepare(self, messages: list[Message], tools: list[ToolSpec], json_schema: dict[str, Any] | None,
                 max_tokens: int) -> Prepared:
        mode = structured_mode()
        official = is_official_openai(self.base_url)
        structured = json_schema is not None and (mode == "on" or (mode == "auto" and official))
        extra_system = schema_instruction(json_schema) if json_schema is not None and not structured else ""
        params: dict[str, Any] = {"model": self.model, "messages": to_openai_messages(messages, extra_system)}
        # Официальный OpenAI: max_completion_tokens (max_tokens устарел и не принимается моделями рассуждений);
        # совместимые серверы (Ollama, vLLM, YandexGPT) понимают max_tokens.
        params["max_completion_tokens" if official else "max_tokens"] = max_tokens
        t = temperature()
        if t is not None:
            params["temperature"] = t
        effort = env("DMS_LLM_REASONING", "").strip().lower()
        if effort:
            # Режим «размышлений» моделей рассуждений (qwen3 и др.). Ollama /v1 понимает reasoning_effort: none —
            # без рассуждения ответ в 5–30 раз короче и быстрее (замер на qwen3:8b, M1: 26 с → 0,7 с), что решает
            # для слабого сервера. По умолчанию не передаётся.
            params["reasoning_effort"] = effort
        if tools:
            params["tools"] = [{"type": "function",
                                "function": {"name": s.name, "description": s.description,
                                             "parameters": s.parameters}} for s in tools]
        if structured:
            params["response_format"] = {"type": "json_schema",
                                         "json_schema": {"name": "answer", "schema": json_schema, "strict": False}}
        return Prepared(params, structured)

    def _send(self, prep: Prepared) -> Any:
        return self._client.chat.completions.create(**prep.params)

    def _parse(self, raw: Any, prep: Prepared) -> LlmTurn:
        if not raw.choices:
            raise LlmError(f"Модель {self.provider}: пустой ответ (нет choices)")
        choice = raw.choices[0]
        msg = choice.message
        calls: list[ToolCall] = []
        for i, tc in enumerate(msg.tool_calls or []):
            fn = getattr(tc, "function", None)
            if fn is None:          # custom-инструменты OpenAI нами не объявляются
                continue
            calls.append(ToolCall(tc.id or f"call_{i}", fn.name, safe_args(fn.arguments)))
        u = raw.usage
        usage = Usage(getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0)
        return LlmTurn(strip_think(msg.content or ""), calls, usage, raw.model or self.model,
                       choice.finish_reason or "")

    def _classify(self, exc: BaseException) -> Failure:
        import openai

        if isinstance(exc, openai.APITimeoutError):
            return Failure(True, None, "timeout")
        if isinstance(exc, openai.APIConnectionError):
            return Failure(True, None, "network")
        if isinstance(exc, openai.APIStatusError):
            return status_failure(exc.status_code, parse_retry_after(getattr(exc.response, "headers", None)))
        return Failure(False, None, "unknown")


def to_openai_messages(messages: list[Message], extra_system: str = "") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    system_done = False
    for m in messages:
        if m.role == "system":
            content = m.content + (f"\n\n{extra_system}" if extra_system and not system_done else "")
            system_done = system_done or bool(extra_system)
            out.append({"role": "system", "content": content})
        elif m.role == "user":
            out.append({"role": "user", "content": m.content})
        elif m.role == "assistant":
            item: dict[str, Any] = {"role": "assistant", "content": m.content or None}
            if m.tool_calls:
                item["tool_calls"] = [{"id": c.id, "type": "function",
                                       "function": {"name": c.name,
                                                    "arguments": json.dumps(c.args, ensure_ascii=False)}}
                                      for c in m.tool_calls]
            out.append(item)
        elif m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id or "", "content": m.content})
    if extra_system and not system_done:
        out.insert(0, {"role": "system", "content": extra_system})
    return out
