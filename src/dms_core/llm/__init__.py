"""Провайдеры LLM: anthropic, openai_compat, gigachat, mock (план §6.4, ADR 0002, CONTRACTS §7).

    get_llm(provider=None, model=None, base_url=None) -> LLM | None

Выбор — DMS_LLM_PROVIDER, DMS_LLM_MODEL, DMS_LLM_BASE_URL (из окружения или .env). None — провайдер не задан
или нет ключа: сервис работает в режимах demo_saved / extractive. DMS_LLM_RECORD=1 поверх живого провайдера —
запись кассет (mock.py). Ключи: ANTHROPIC_API_KEY, OPENAI_API_KEY (+ OPENAI_BASE_URL), GIGACHAT_CREDENTIALS.
"""
from __future__ import annotations

from ._common import env, log
from .base import LLM, LlmError, LlmTurn, Message, ToolCall, ToolSpec, Usage

PROVIDERS = ("anthropic", "openai_compat", "gigachat", "mock")


def _live(provider: str, model: str | None, base_url: str | None) -> LLM | None:
    model = model or env("DMS_LLM_MODEL") or None
    if provider == "anthropic":
        key = env("ANTHROPIC_API_KEY")
        if not key:
            return None
        from .anthropic import AnthropicLLM
        return AnthropicLLM(model, api_key=key, base_url=base_url or env("DMS_LLM_BASE_URL") or None)
    if provider == "openai_compat":
        url = base_url or env("DMS_LLM_BASE_URL") or env("OPENAI_BASE_URL") or None
        key = env("OPENAI_API_KEY")
        # Без ключа допустим только свой сервер (Ollama, vLLM): официальному OpenAI ключ обязателен.
        if not key and not url:
            return None
        if not model:
            log.warning("llm provider=openai_compat: не задана модель (DMS_LLM_MODEL)")
            return None
        from .openai_compat import OpenAICompatLLM
        return OpenAICompatLLM(model, api_key=key or None, base_url=url)
    if provider == "gigachat":
        creds = env("GIGACHAT_CREDENTIALS")
        if not creds:
            return None
        from .gigachat import GigaChatLLM
        return GigaChatLLM(model, credentials=creds, base_url=base_url or env("DMS_LLM_BASE_URL") or None,
                           scope=env("GIGACHAT_SCOPE") or None)
    raise LlmError(f"Неизвестный провайдер модели: {provider!r} (допустимо: {', '.join(PROVIDERS)})")


def get_llm(provider: str | None = None, model: str | None = None, base_url: str | None = None) -> LLM | None:
    provider = (provider or env("DMS_LLM_PROVIDER")).strip()
    if not provider:
        return None
    from .mock import MockLLM

    if provider == "mock":
        return MockLLM()
    live = _live(provider, model, base_url)
    if live is None:
        log.info("llm provider=%s: нет ключа или модели — без модели", provider)
        return None
    if env("DMS_LLM_RECORD") == "1":
        return MockLLM(inner=live)
    return live


__all__ = ["LLM", "PROVIDERS", "LlmError", "LlmTurn", "Message", "ToolCall", "ToolSpec", "Usage", "get_llm"]
