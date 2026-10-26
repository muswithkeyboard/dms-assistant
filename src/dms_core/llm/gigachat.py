"""Провайдер GigaChat (SDK gigachat 0.2.x, функции; ADR 0002).

Перевод (сверено с SDK: models/chat.py — Chat, Messages, MessagesRole, Function, FunctionCall;
resources/chat.py — `client.chat(payload)` → ChatCompletion; exceptions.py):
- system/user → `{"role", "content"}`;
- инструменты → `functions=[{"name", "description", "parameters"}]`, `function_call="auto"`;
- вызов модели приходит в `message.function_call {name, arguments: dict}` (один вызов на ход),
  `finish_reason="function_call"`; id вызова — `functions_state_id` ответа (или gc-<n>, если его нет);
- assistant с вызовом → `{"role": "assistant", "content", "function_call": {...}, "functions_state_id"}`;
- tool → `{"role": "function", "name", "content": "<JSON-строка>"}` (GigaChat ждёт JSON; не JSON — оборачиваем
  в {"result": ...}).
Итог по схеме: response_format у GigaChat помечен в SDK как бета, поэтому по умолчанию (auto) — инструкция
config/prompts/json_output.md и извлечение JSON; DMS_LLM_STRUCTURED=on включает response_format (без функций).
Внимание: str() исключений SDK содержит тело ответа и заголовки — наружу они не передаются (`_classify`).
"""
from __future__ import annotations

import json
from typing import Any

from ._common import (
    BaseProvider,
    Failure,
    LlmTurn,
    Message,
    Prepared,
    ToolCall,
    ToolSpec,
    Usage,
    safe_args,
    schema_instruction,
    status_failure,
    structured_mode,
    temperature,
    timeout_s,
)

DEFAULT_MODEL = "GigaChat-2-Max"


class GigaChatLLM(BaseProvider):
    provider = "gigachat"

    def __init__(self, model: str | None = None, *, credentials: str | None = None, base_url: str | None = None,
                 scope: str | None = None, client: Any = None) -> None:
        self.model = model or DEFAULT_MODEL
        if client is None:
            from gigachat import GigaChat

            kw: dict[str, Any] = {"credentials": credentials, "model": self.model, "timeout": timeout_s(),
                                  "max_retries": 0}
            if base_url:
                kw["base_url"] = base_url
            if scope:
                kw["scope"] = scope
            # Прочие параметры (GIGACHAT_VERIFY_SSL_CERTS, GIGACHAT_CA_BUNDLE_FILE…) SDK читает из окружения сам.
            client = GigaChat(**kw)
        self._client = client
        self._n = 0

    def _prepare(self, messages: list[Message], tools: list[ToolSpec], json_schema: dict[str, Any] | None,
                 max_tokens: int) -> Prepared:
        structured = json_schema is not None and not tools and structured_mode() == "on"
        extra = schema_instruction(json_schema) if json_schema is not None and not structured else ""
        payload: dict[str, Any] = {"model": self.model, "messages": to_gigachat_messages(messages, extra),
                                   "max_tokens": max_tokens}
        t = temperature()
        if t is not None:
            payload["temperature"] = t
        if tools:
            payload["functions"] = [{"name": s.name, "description": s.description, "parameters": s.parameters}
                                    for s in tools]
            payload["function_call"] = "auto"
        if structured:
            payload["response_format"] = {"type": "json_schema", "schema": json_schema}
        return Prepared(payload, structured)

    def _send(self, prep: Prepared) -> Any:
        return self._client.chat(prep.params)

    def _parse(self, raw: Any, prep: Prepared) -> LlmTurn:
        choice = raw.choices[0]
        msg = choice.message
        calls: list[ToolCall] = []
        if msg.function_call is not None:
            self._n += 1
            call_id = msg.functions_state_id or f"gc-{self._n}"
            calls.append(ToolCall(call_id, msg.function_call.name, safe_args(msg.function_call.arguments)))
        usage = Usage(raw.usage.prompt_tokens or 0, raw.usage.completion_tokens or 0)
        return LlmTurn((msg.content or "").strip(), calls, usage, raw.model or self.model,
                       choice.finish_reason or "")

    def _classify(self, exc: BaseException) -> Failure:
        import httpx
        from gigachat import exceptions as gx

        if isinstance(exc, gx.RateLimitError):
            return status_failure(429, exc.retry_after or None)
        if isinstance(exc, gx.ResponseError):
            return status_failure(exc.status_code)
        if isinstance(exc, httpx.TimeoutException):
            return Failure(True, None, "timeout")
        if isinstance(exc, httpx.TransportError):
            return Failure(True, None, "network")
        return Failure(False, None, "unknown")


def _json_content(text: str) -> str:
    try:
        json.loads(text)
        return text
    except (TypeError, ValueError):
        return json.dumps({"result": text}, ensure_ascii=False)


def to_gigachat_messages(messages: list[Message], extra_system: str = "") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role in ("system", "user"):
            out.append({"role": m.role, "content": m.content})
        elif m.role == "assistant":
            item: dict[str, Any] = {"role": "assistant", "content": m.content}
            if m.tool_calls:
                c = m.tool_calls[0]           # у GigaChat один вызов функции на ход
                item["function_call"] = {"name": c.name, "arguments": c.args}
                if not c.id.startswith("gc-"):
                    item["functions_state_id"] = c.id
            out.append(item)
        elif m.role == "tool":
            out.append({"role": "function", "name": m.name or "", "content": _json_content(m.content)})
    if extra_system:
        for item in out:
            if item["role"] == "system":
                item["content"] = f"{item['content']}\n\n{extra_system}"
                break
        else:
            out.insert(0, {"role": "system", "content": extra_system})
    return out
