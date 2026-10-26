"""Провайдер mock: ответы из кассет, без сети (`docs/rules.md` п. 6, CONTRACTS §7).

Ключ кассеты — sha256 канонического JSON нейтрального запроса (messages, tools, json_schema, max_tokens;
sort_keys, без пробелов). Модель в ключ не входит: одна и та же кассета воспроизводит записанный ход
независимо от DMS_LLM_MODEL; какая модель отвечала — поле `turn.model` кассеты.
Каталог — DMS_LLM_CASSETTES (по умолчанию eval/cassettes/; кассеты с текстами комплекта — в
data/derived/cassettes/, вне git). Запись: DMS_LLM_RECORD=1 и живой провайдер — get_llm оборачивает его
в MockLLM(inner=...): есть кассета — ответ из неё, нет — вызов живой модели и запись.
В кассету не пишутся ключи и параметры клиента — только нейтральный запрос и ответ.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..config import REPO_ROOT
from ._common import LlmError, LlmTurn, Message, ToolCall, ToolSpec, Usage, env, log

FORMAT = 1


def default_cassette_dir() -> Path:
    raw = env("DMS_LLM_CASSETTES")
    return Path(raw) if raw else REPO_ROOT / "eval" / "cassettes"


def request_dict(messages: list[Message], tools: list[ToolSpec] | None, json_schema: dict[str, Any] | None,
                 max_tokens: int) -> dict[str, Any]:
    return {"messages": [asdict(m) for m in messages],
            "tools": [asdict(t) for t in tools] if tools else None,
            "json_schema": json_schema,
            "max_tokens": max_tokens}


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def request_key(messages: list[Message], tools: list[ToolSpec] | None = None,
                json_schema: dict[str, Any] | None = None, max_tokens: int = 4096) -> str:
    return hashlib.sha256(canonical(request_dict(messages, tools, json_schema, max_tokens)).encode()).hexdigest()


def turn_to_dict(t: LlmTurn) -> dict[str, Any]:
    return asdict(t)


def turn_from_dict(d: dict[str, Any]) -> LlmTurn:
    return LlmTurn(text=d.get("text", ""),
                   tool_calls=[ToolCall(c["id"], c["name"], dict(c.get("args") or {})) for c in d.get("tool_calls", [])],
                   usage=Usage(**(d.get("usage") or {})),
                   model=d.get("model", "mock"),
                   stop_reason=d.get("stop_reason", ""))


class MockLLM:
    provider = "mock"

    def __init__(self, cassette_dir: Path | str | None = None, *, inner: Any = None) -> None:
        self.dir = Path(cassette_dir) if cassette_dir else default_cassette_dir()
        self.inner = inner
        self.model = inner.model if inner is not None else "mock"

    def path_for(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def chat(self, messages: list[Message], tools: list[ToolSpec] | None = None, *,
             json_schema: dict[str, Any] | None = None, max_tokens: int = 4096) -> LlmTurn:
        req = request_dict(messages, tools, json_schema, max_tokens)
        key = hashlib.sha256(canonical(req).encode()).hexdigest()
        path = self.path_for(key)
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            log.info("llm provider=mock cassette=%s hit", key[:12])
            return turn_from_dict(data["turn"])
        if self.inner is None:
            log.warning("llm provider=mock cassette=%s miss", key[:12])
            raise LlmError(f"Модель mock: нет кассеты {key[:12]} для этого запроса "
                           "(запишите прогон: DMS_LLM_RECORD=1 и живой провайдер)")
        turn = self.inner.chat(messages, tools, json_schema=json_schema, max_tokens=max_tokens)
        self._write(path, {"format": FORMAT, "key": key, "provider": self.inner.provider,
                           "request": req, "turn": turn_to_dict(turn)})
        log.info("llm provider=mock cassette=%s recorded via=%s", key[:12], self.inner.provider)
        return turn

    def _write(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, path)
