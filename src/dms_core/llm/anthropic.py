"""Провайдер Anthropic (Messages API, SDK anthropic 2.x; ADR 0002).

Перевод нейтрального формата (сверено с исходниками SDK: resources/messages/messages.py, types/tool_param.py,
types/tool_use_block.py, types/tool_result_block_param.py, types/output_config_param.py):
- system-сообщения → параметр `system` (одна строка);
- assistant с вызовами → блоки `{"type": "tool_use", id, name, input}`;
- tool → блок `{"type": "tool_result", tool_use_id, content}` в сообщении role=user; подряд идущие результаты —
  в одном сообщении (так модель видит параллельные вызовы как один ход);
- инструменты → `{"name", "description", "input_schema"}`;
- итог по схеме → `output_config={"format": {"type": "json_schema", "schema": ...}}` (structured outputs).
Температура не передаётся: в SDK 2.x у messages.create нет параметра temperature, а текущие модели отвечают
400 на temperature/top_p/top_k. Блоки рассуждений (thinking) из ответа хранятся в памяти экземпляра по id
вызова и возвращаются в историю без изменений — иначе следующий ход с tool_result теряет рассуждение.
"""
from __future__ import annotations

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
    parse_retry_after,
    status_failure,
    strictify,
    structured_mode,
    system_text,
    timeout_s,
)

DEFAULT_MODEL = "claude-opus-5-5"


class AnthropicLLM(BaseProvider):
    provider = "anthropic"

    def __init__(self, model: str | None = None, *, api_key: str | None = None, base_url: str | None = None,
                 client: Any = None) -> None:
        self.model = model or DEFAULT_MODEL
        if client is None:
            import anthropic

            # Повторы делает наш with_retries (не больше 3), поэтому у SDK они выключены.
            client = anthropic.Anthropic(api_key=api_key, base_url=base_url or None,
                                         timeout=timeout_s(), max_retries=0)
        self._client = client
        self._raw_by_call: dict[str, list[dict[str, Any]]] = {}   # id tool_use → блоки ответа целиком

    # --- перевод запроса
    def _prepare(self, messages: list[Message], tools: list[ToolSpec], json_schema: dict[str, Any] | None,
                 max_tokens: int) -> Prepared:
        params: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens,
                                  "messages": to_anthropic_messages(messages, self._raw_by_call)}
        system = system_text(messages)
        if system:
            params["system"] = system
        if tools:
            params["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters}
                               for t in tools]
        structured = json_schema is not None and structured_mode() != "off"
        if json_schema is not None:
            if structured:
                params["output_config"] = {"format": {"type": "json_schema", "schema": strictify(json_schema)}}
            else:
                from ._common import schema_instruction
                params["system"] = system_text(messages, schema_instruction(json_schema))
        return Prepared(params, structured)

    def _send(self, prep: Prepared) -> Any:
        return self._client.messages.create(**prep.params)

    # --- разбор ответа
    def _parse(self, raw: Any, prep: Prepared) -> LlmTurn:
        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in raw.content:
            if block.type == "text":
                texts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(block.id, block.name, dict(block.input or {})))
        if calls:
            dumped = [b.model_dump(mode="json", exclude_none=True) for b in raw.content]
            for c in calls:
                self._raw_by_call[c.id] = dumped
        usage = Usage(raw.usage.input_tokens or 0, raw.usage.output_tokens or 0)
        return LlmTurn("".join(texts).strip(), calls, usage, raw.model or self.model, raw.stop_reason or "")

    def _classify(self, exc: BaseException) -> Failure:
        import anthropic

        if isinstance(exc, anthropic.APITimeoutError):
            return Failure(True, None, "timeout")
        if isinstance(exc, anthropic.APIConnectionError):
            return Failure(True, None, "network")
        if isinstance(exc, anthropic.APIStatusError):
            return status_failure(exc.status_code, parse_retry_after(getattr(exc.response, "headers", None)))
        return Failure(False, None, "unknown")


def to_anthropic_messages(messages: list[Message], raw_by_call: dict[str, list[dict[str, Any]]] | None = None
                          ) -> list[dict[str, Any]]:
    """Нейтральные сообщения → messages Anthropic. system сюда не входит (он — отдельный параметр)."""
    raw_by_call = raw_by_call or {}
    out: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        # Подряд идущие сообщения одной роли склеиваются: tool_result-ы одного хода — в одном user-сообщении.
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for m in messages:
        if m.role == "system":
            continue
        if m.role == "user":
            push("user", [{"type": "text", "text": m.content}])
        elif m.role == "assistant":
            cached = raw_by_call.get(m.tool_calls[0].id) if m.tool_calls else None
            if cached is not None:
                push("assistant", [dict(b) for b in cached])
                continue
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.args} for c in m.tool_calls]
            if blocks:
                push("assistant", blocks)
        elif m.role == "tool":
            if not m.tool_call_id:
                raise LlmError("Модель anthropic: результат инструмента без id вызова")
            push("user", [{"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}])
    return out
