"""Фейковые клиенты SDK и придуманные образцы ответов API для тестов слоя моделей (без сети).

Образцы ответов собраны вручную по типам SDK (anthropic.types.Message, openai ChatCompletion,
gigachat.models.ChatCompletion) — это свои данные, не записи настоящих API.
"""
from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterator
from typing import Any

import dms_core.llm._common as common

SECRET = "sk-test-SECRET-0123456789"
DOC_TEXT = "Пункт 2.6.1. Страховщик оплачивает снятие зубных отложений методом AIR FLOW"


@contextlib.contextmanager
def patched_env(**values: str | None) -> Iterator[None]:
    """Временные переменные окружения; .env пользователя не читается (DOTENV_PATH → несуществующий файл)."""
    old = {k: os.environ.get(k) for k in values}
    old_dotenv = common.DOTENV_PATH
    common.DOTENV_PATH = common.Path("/nonexistent/.env")
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        common.DOTENV_PATH = old_dotenv
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def no_sleep() -> Iterator[list[float]]:
    pauses: list[float] = []
    old = common._sleep
    common._sleep = pauses.append
    try:
        yield pauses
    finally:
        common._sleep = old


@contextlib.contextmanager
def captured_logs() -> Iterator[list[str]]:
    lines: list[str] = []

    class H(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    h = H(level=logging.DEBUG)
    lg = logging.getLogger()
    old_level = lg.level
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    try:
        yield lines
    finally:
        lg.removeHandler(h)
        lg.setLevel(old_level)


class Scripted:
    """Возвращает ответы/исключения по очереди и запоминает параметры каждого вызова."""

    def __init__(self, *outcomes: Any) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[Any] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(kwargs if kwargs else (args[0] if args else None))
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


class FakeAnthropic:
    def __init__(self, script: Scripted) -> None:
        self.messages = type("M", (), {"create": staticmethod(script)})()


class FakeOpenAI:
    def __init__(self, script: Scripted) -> None:
        completions = type("C", (), {"create": staticmethod(script)})()
        self.chat = type("Ch", (), {"completions": completions})()


class FakeGigaChat:
    def __init__(self, script: Scripted) -> None:
        self.chat = script


# --- образцы ответов -------------------------------------------------------------------------------------------

def anthropic_tool_use() -> Any:
    from anthropic.types import Message as AMessage

    return AMessage.model_validate({
        "id": "msg_01", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "stop_reason": "tool_use", "stop_sequence": None,
        "usage": {"input_tokens": 120, "output_tokens": 40},
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig-abc"},
            {"type": "text", "text": "Ищу пункт."},
            {"type": "tool_use", "id": "toolu_1", "name": "search_clauses", "input": {"query": "чистка зубов"}},
        ]})


def anthropic_text(text: str) -> Any:
    from anthropic.types import Message as AMessage

    return AMessage.model_validate({
        "id": "msg_02", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 300, "output_tokens": 80},
        "content": [{"type": "text", "text": text}]})


def openai_tool_call(arguments: str = '{"query": "чистка зубов"}', name: str = "search_clauses") -> Any:
    from openai.types.chat import ChatCompletion

    return ChatCompletion.model_validate({
        "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "qwen3:8b",
        "choices": [{"index": 0, "finish_reason": "tool_calls",
                     "message": {"role": "assistant", "content": "",
                                 "tool_calls": [{"id": "call_1", "type": "function",
                                                 "function": {"name": name, "arguments": arguments}}]}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})


def openai_text(text: str) -> Any:
    from openai.types.chat import ChatCompletion

    return ChatCompletion.model_validate({
        "id": "chatcmpl-2", "object": "chat.completion", "created": 1, "model": "qwen3:8b",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 50, "total_tokens": 250}})


def gigachat_function_call() -> Any:
    from gigachat.models import ChatCompletion

    return ChatCompletion.model_validate({
        "created": 1, "model": "GigaChat-2-Max", "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": "function_call",
                     "message": {"role": "assistant", "content": "",
                                 "function_call": {"name": "search_clauses", "arguments": {"query": "чистка"}},
                                 "functions_state_id": "state-77"}}],
        "usage": {"prompt_tokens": 90, "completion_tokens": 15, "total_tokens": 105}})


def gigachat_text(text: str) -> Any:
    from gigachat.models import ChatCompletion

    return ChatCompletion.model_validate({
        "created": 1, "model": "GigaChat-2-Max", "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})


# --- исключения SDK --------------------------------------------------------------------------------------------

def anthropic_status(code: int, body_text: str = "") -> BaseException:
    import anthropic
    import httpx2

    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    resp = httpx2.Response(code, request=req, headers={"retry-after": "1"})
    cls = {429: anthropic.RateLimitError, 500: anthropic.InternalServerError, 401: anthropic.AuthenticationError,
           400: anthropic.BadRequestError}.get(code, anthropic.APIStatusError)
    return cls(f"Error code: {code} {body_text}", response=resp, body={"error": body_text})


def openai_status(code: int, body_text: str = "") -> BaseException:
    import httpx2
    import openai

    req = httpx2.Request("POST", "http://127.0.0.1:11434/v1/chat/completions")
    resp = httpx2.Response(code, request=req)
    cls = {429: openai.RateLimitError, 503: openai.InternalServerError, 400: openai.BadRequestError}.get(
        code, openai.APIStatusError)
    return cls(f"Error code: {code} {body_text}", response=resp, body={"error": body_text})


def openai_connection() -> BaseException:
    import httpx2
    import openai

    return openai.APIConnectionError(request=httpx2.Request("POST", "http://127.0.0.1:11434/v1/chat/completions"))


def gigachat_status(code: int, body_text: str = "") -> BaseException:
    from gigachat import exceptions as gx

    cls = {429: gx.RateLimitError, 500: gx.ServerError, 401: gx.AuthenticationError}.get(code, gx.ResponseError)
    return cls("https://gigachat/api/v1/chat/completions", code, body_text.encode(), None)
