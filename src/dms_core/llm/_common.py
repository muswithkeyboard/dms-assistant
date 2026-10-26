"""Общая часть провайдеров: окружение и .env, повторы, журнал без содержимого, извлечение JSON (ADR 0002).

Правила безопасности слоя (`docs/rules.md` п. 4, бриф w2-llm):
- ключи читаются только здесь (`env`) и только провайдерами; в журнал и в тексты ошибок не попадают;
- журнал (`log_call`) — только провайдер, модель, токены, время, попытка, код ответа — без сообщений и ответов;
- `LlmError` строится из фиксированных фраз и кода HTTP; исходное исключение SDK не присоединяется
  (`raise ... from None`), потому что его текст может содержать тело ответа (у GigaChat — всегда).
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from ..config import REPO_ROOT
from .base import LlmError, LlmTurn, Message, ToolCall, ToolSpec, Usage

log = logging.getLogger("dms_core.llm")

MAX_RETRIES = 3            # повторов после первой попытки (бриф: не больше 3)
BACKOFF_BASE_S = 1.0       # пауза 1, 2, 4 с (+ случайная добавка до 0,5 с)
BACKOFF_MAX_S = 20.0       # потолок паузы, в том числе для Retry-After
DEFAULT_TIMEOUT_S = 120.0  # таймаут одного запроса; переопределяется DMS_LLM_TIMEOUT

# Файл .env в корне app/ (вне git). Тесты подменяют путь, чтобы не читать настоящий .env пользователя.
DOTENV_PATH: Path = REPO_ROOT / ".env"

_sleep: Callable[[float], None] = time.sleep   # тесты подменяют, чтобы повторы шли без ожидания


# --- окружение -------------------------------------------------------------------------------------------------

@cache
def _dotenv(path: str) -> dict[str, str]:
    """Простой разбор .env: KEY=VALUE, комментарии #, кавычки вокруг значения. Без зависимостей."""
    out: dict[str, str] = {}
    p = Path(path)
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        elif " #" in v:
            v = v.split(" #", 1)[0].rstrip()
        out[k] = v
    return out


def env(name: str, default: str = "") -> str:
    """Значение из окружения процесса, иначе из .env, иначе default. Пустая строка = не задано."""
    v = os.environ.get(name)
    if v:
        return v
    return _dotenv(str(DOTENV_PATH)).get(name, "") or default


def timeout_s() -> float:
    try:
        return float(env("DMS_LLM_TIMEOUT", str(DEFAULT_TIMEOUT_S)))
    except ValueError:
        return DEFAULT_TIMEOUT_S


def temperature() -> float | None:
    """Температура 0 (ADR 0002). DMS_LLM_TEMPERATURE=none — не передавать (модели, где параметр запрещён)."""
    raw = env("DMS_LLM_TEMPERATURE", "0").strip().lower()
    if raw in ("none", "off", "-"):
        return None
    try:
        return float(raw)
    except ValueError:
        return 0.0


_STRUCTURED_OVERRIDE: ContextVar[str | None] = ContextVar("dms_llm_structured", default=None)


def structured_mode() -> str:
    """DMS_LLM_STRUCTURED: auto (по возможностям провайдера) | on | off (только инструкция + извлечение JSON).
    Внутри `structured_override(...)` — значение переопределения (только для текущего потока/контекста)."""
    forced = _STRUCTURED_OVERRIDE.get()
    if forced in ("auto", "on", "off"):
        return forced
    v = env("DMS_LLM_STRUCTURED", "auto").strip().lower()
    return v if v in ("auto", "on", "off") else "auto"


@contextmanager
def structured_override(mode: str) -> Iterator[None]:
    """Структурный режим только для вызовов внутри блока (guided: короткие вызовы без инструментов со строгой
    схемой — constrained decoding у Ollama /v1 через response_format). Глобальное окружение не меняется,
    поэтому параллельные прогоны агента (с инструментами) это не задевает."""
    token = _STRUCTURED_OVERRIDE.set(mode)
    try:
        yield
    finally:
        _STRUCTURED_OVERRIDE.reset(token)


# --- повторы и ошибки ------------------------------------------------------------------------------------------

@dataclass
class Failure:
    """Классификация исключения SDK: можно ли повторить, код HTTP, пауза из Retry-After."""
    retryable: bool
    status: int | None = None
    kind: str = "api"            # api | network | timeout | auth | bad_request | unknown
    retry_after: float | None = None


_KIND_RU = {
    "auth": "ключ не принят провайдером (проверьте .env)",
    "bad_request": "провайдер отклонил запрос",
    "network": "нет связи с провайдером",
    "timeout": "провайдер не ответил за отведённое время",
    "rate": "превышен лимит запросов провайдера",
    "server": "ошибка на стороне провайдера",
    "api": "ошибка провайдера",
    "unknown": "непредвиденная ошибка вызова модели",
}


def error_text(provider: str, f: Failure, attempts: int) -> str:
    """Понятная ошибка по-русски: провайдер, вид сбоя, код, число попыток. Без текстов запроса и ключей."""
    kind = f.kind
    if f.status == 429:
        kind = "rate"
    elif f.status is not None and f.status >= 500:
        kind = "server"
    code = f" (HTTP {f.status})" if f.status else ""
    tries = f"; попыток: {attempts}" if attempts > 1 else ""
    return f"Модель {provider}: {_KIND_RU.get(kind, _KIND_RU['unknown'])}{code}{tries}"


def status_failure(status: int | None, retry_after: float | None = None) -> Failure:
    """Повторяем 408, 409, 429 и 5xx; 401/403 — ошибка ключа; прочие 4xx — неверный запрос, без повтора."""
    if status is None:
        return Failure(False, None, "unknown")
    if status in (401, 403):
        return Failure(False, status, "auth")
    if status in (408, 409, 429) or status >= 500:
        return Failure(True, status, "api", retry_after)
    return Failure(False, status, "bad_request")


def parse_retry_after(headers: Any) -> float | None:
    try:
        v = headers.get("retry-after") if headers is not None else None
        return float(v) if v else None
    except (TypeError, ValueError, AttributeError):
        return None




def with_retries[T](provider: str, model: str, call: Callable[[], T],
                 classify: Callable[[BaseException], Failure]) -> T:
    """Вызов с повторами при 429/5xx/сети (не больше MAX_RETRIES), экспоненциальная пауза с потолком.

    Каждая неудачная попытка пишется в журнал: провайдер, модель, код, номер попытки — без содержимого.
    Итог неудачи — LlmError без цепочки исключений (текст исключения SDK может содержать тело ответа).
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return call()
        except LlmError:
            raise
        except Exception as exc:  # noqa: BLE001 — классифицируем любые сбои SDK и сети
            f = classify(exc)
            log.warning("llm сбой provider=%s model=%s kind=%s status=%s attempt=%d retry=%s",
                        provider, model, f.kind, f.status, attempt, f.retryable and attempt <= MAX_RETRIES)
            if not f.retryable or attempt > MAX_RETRIES:
                raise LlmError(error_text(provider, f, attempt)) from None
            pause = min(BACKOFF_BASE_S * 2 ** (attempt - 1) + random.uniform(0, 0.5), BACKOFF_MAX_S)
            if f.retry_after:
                pause = min(max(pause, f.retry_after), BACKOFF_MAX_S)
            _sleep(pause)


def log_call(provider: str, model: str, usage: Usage, started: float, stop_reason: str, n_tools: int) -> None:
    """Единственная запись об успешном вызове: только метаданные (`docs/rules.md` п. 4)."""
    log.info("llm provider=%s model=%s in=%d out=%d ms=%d stop=%s tool_calls=%d",
             provider, model, usage.input_tokens, usage.output_tokens,
             int((time.monotonic() - started) * 1000), stop_reason, n_tools)


# --- JSON итогового ответа -------------------------------------------------------------------------------------

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def strip_think(text: str) -> str:
    """Убирает рассуждения в <think>…</think> (qwen3, deepseek через OpenAI-совместимый адрес)."""
    return _THINK_RE.sub("", text or "").strip()


def extract_json(text: str) -> dict[str, Any] | None:
    """Первый JSON-объект в тексте: целиком, внутри ```json```, или первый разбираемый {...}. Иначе None."""
    text = strip_think(text)
    if not text:
        return None
    candidates = [text] + [m.group(1) for m in _FENCE_RE.finditer(text)]
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = dec.raw_decode(text, i)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def json_matches(obj: dict[str, Any], schema: dict[str, Any]) -> bool:
    """Минимальная проверка без jsonschema: есть все обязательные поля верхнего уровня.

    Полную проверку по схеме делает agent.py (pydantic DraftAnswer); здесь — только «похоже ли на ответ»."""
    return all(k in obj for k in schema.get("required", []))


def strictify(schema: dict[str, Any]) -> dict[str, Any]:
    """Копия схемы с additionalProperties=false у всех объектов (требование structured outputs Anthropic)."""
    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: walk(v) for k, v in node.items()}
            if out.get("type") == "object" and "additionalProperties" not in out:
                out["additionalProperties"] = False
            return out
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node
    return walk(schema)


def safe_args(raw: Any) -> dict[str, Any]:
    """Аргументы вызова инструмента → dict. Не JSON / не объект → {"__error__": ...}: исполнитель инструмента
    вернёт модели ошибку, и она сможет повторить вызов (сам вызов не теряется — у Anthropic на каждый tool_use
    обязателен tool_result)."""
    if isinstance(raw, dict):
        return dict(raw)
    if raw is None or raw == "":
        return {}
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return {"__error__": "аргументы вызова — не JSON"}
    return obj if isinstance(obj, dict) else {"__error__": "аргументы вызова — не объект JSON"}


# --- общий каркас провайдера -----------------------------------------------------------------------------------

@dataclass
class Prepared:
    """Что провайдер отправит: готовые параметры API и признак structured output."""
    params: dict[str, Any]
    structured: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class BaseProvider:
    """Каркас chat(): перевод → вызов с повторами → разбор → журнал → проверка JSON (+ один ремонт).

    Наследник реализует `_prepare`, `_send`, `_parse`, `_classify`. Разбор итогового JSON общий:
    - если запрошен json_schema и модель не вызвала инструменты, из текста извлекается JSON-объект,
      и `text` заменяется на его каноническую запись;
    - если JSON нет или нет обязательных полей — один повторный запрос без инструментов с просьбой
      вернуть только JSON (config/prompts/json_repair.md); снова неудача — stop_reason="invalid_json",
      `text` — как вернула модель, решение за agent.py.
    """
    provider = "base"
    model = ""

    def chat(self, messages: list[Message], tools: list[ToolSpec] | None = None, *,
             json_schema: dict[str, Any] | None = None, max_tokens: int = 4096) -> LlmTurn:
        turn = self._once(messages, tools, json_schema, max_tokens)
        if json_schema is None or turn.tool_calls:
            return self._mark_unknown_tools(turn, tools)
        obj = extract_json(turn.text)
        if obj is not None and json_matches(obj, json_schema):
            turn.text = json.dumps(obj, ensure_ascii=False)
            return turn
        # Один ремонт: модель видит свой ответ и просьбу вернуть только JSON по той же схеме.
        from .prompts import load
        repair, _ = load("json_repair")
        retry_msgs = list(messages) + [Message("assistant", turn.text), Message("user", repair)]
        fixed = self._once(retry_msgs, None, json_schema, max_tokens)
        fixed.usage = Usage(turn.usage.input_tokens + fixed.usage.input_tokens,
                            turn.usage.output_tokens + fixed.usage.output_tokens)
        obj = extract_json(fixed.text)
        if obj is not None and json_matches(obj, json_schema):
            fixed.text = json.dumps(obj, ensure_ascii=False)
            return fixed
        log.warning("llm provider=%s model=%s: итог не JSON по схеме после ремонта", self.provider, self.model)
        fixed.stop_reason = "invalid_json"
        return fixed

    def _once(self, messages: list[Message], tools: list[ToolSpec] | None,
              json_schema: dict[str, Any] | None, max_tokens: int) -> LlmTurn:
        prep = self._prepare(messages, tools or [], json_schema, max_tokens)
        started = time.monotonic()
        raw = with_retries(self.provider, self.model, lambda: self._send(prep), self._classify)
        try:
            turn = self._parse(raw, prep)
        except LlmError:
            raise
        except Exception:  # noqa: BLE001 — ответ не того вида: без подробностей, они могут содержать текст
            log.warning("llm provider=%s model=%s: ответ неожиданного вида", self.provider, self.model)
            raise LlmError(f"Модель {self.provider}: ответ неожиданного вида") from None
        log_call(self.provider, turn.model, turn.usage, started, turn.stop_reason, len(turn.tool_calls))
        return turn

    def _mark_unknown_tools(self, turn: LlmTurn, tools: list[ToolSpec] | None) -> LlmTurn:
        """Вызов несуществующего инструмента не выбрасывается: агент отвечает модели {"error": ...}
        (у Anthropic без tool_result следующий запрос недопустим). Здесь — только запись в журнал."""
        names = {t.name for t in tools or []}
        unknown = [c.name for c in turn.tool_calls if c.name not in names]
        if unknown:
            log.warning("llm provider=%s model=%s: вызов неизвестного инструмента (%d)",
                        self.provider, self.model, len(unknown))
        return turn

    # --- реализуют провайдеры
    def _prepare(self, messages: list[Message], tools: list[ToolSpec], json_schema: dict[str, Any] | None,
                 max_tokens: int) -> Prepared:  # pragma: no cover
        raise NotImplementedError

    def _send(self, prep: Prepared) -> Any:  # pragma: no cover
        raise NotImplementedError

    def _parse(self, raw: Any, prep: Prepared) -> LlmTurn:  # pragma: no cover
        raise NotImplementedError

    def _classify(self, exc: BaseException) -> Failure:  # pragma: no cover
        raise NotImplementedError


def schema_instruction(json_schema: dict[str, Any]) -> str:
    """Инструкция «итог — только JSON по схеме» для провайдеров без structured output (config/prompts)."""
    from .prompts import load
    text, _ = load("json_output")
    return text.replace("{{schema}}", json.dumps(json_schema, ensure_ascii=False, sort_keys=True))


def system_text(messages: list[Message], extra: str = "") -> str:
    parts = [m.content for m in messages if m.role == "system" and m.content]
    if extra:
        parts.append(extra)
    return "\n\n".join(parts)


__all__ = ["BaseProvider", "Failure", "LlmError", "LlmTurn", "Message", "Prepared", "ToolCall", "ToolSpec",
           "Usage", "env", "extract_json", "log", "safe_args", "schema_instruction", "status_failure",
           "strictify", "strip_think", "system_text", "temperature", "timeout_s", "with_retries"]
