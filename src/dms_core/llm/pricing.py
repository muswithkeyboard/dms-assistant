"""Цены моделей за 1 млн токенов, USD (для docs/model_comparison.md и журнала трат; ADR 0002).

config/*.yaml заморожен, поэтому таблица — здесь. Неизвестная модель → None («цена не известна», а не 0).
Источники и даты:
- Anthropic — таблица моделей и цен первой стороны (справка Claude API, срез 06.10.2026); цены для запросов
  до 100 тыс. токенов, без кэша и пакетной скидки.
- Локальные модели Ollama — 0: считаются на своей машине, оплаты API нет.
- OpenAI, YandexGPT, GigaChat — не заполнены: нет проверенного источника на дату этапа; добавить с
  источником при выборе моделей для сравнения (иначе cost_usd вернёт None).
"""
from __future__ import annotations

from .base import Usage

# модель → (вход, выход) USD за 1 млн токенов
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    # Anthropic, срез 06.10.2026
    "claude-fable-5-1": (10.00, 50.00),
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-haiku-4-5": (1.00, 5.00),
    # локально (Ollama)
    "qwen3:8b": (0.0, 0.0),
    "mock": (0.0, 0.0),
}


def price(model: str) -> tuple[float, float] | None:
    return PRICES_PER_MTOK.get(model)


def cost_usd(model: str, usage: Usage) -> float | None:
    """Стоимость хода по фактическим токенам из ответа API (usage), не по оценке токенизатора."""
    p = price(model)
    if p is None:
        return None
    return round((usage.input_tokens * p[0] + usage.output_tokens * p[1]) / 1_000_000, 6)
