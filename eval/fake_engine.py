"""Фейковый движок для проверки конвейера оценки, пока dms_core.engine.answer — заглушка (CONTRACTS §7).

Ведёт себя как честный extractive-режим без модели: метки нет, статус «данных недостаточно», ссылок нет,
след пустой. Эталонов не читает — поэтому его прогон показывает нижнюю границу, а не «идеальный» отчёт.
"""
from __future__ import annotations

from dms_core.constants import AnswerMode, Confidence, Scenario
from dms_core.schemas import Answer, AnswerContext, ChatContext


def answer(question: str, context: ChatContext, *, llm=None, store=None, trace: dict | None = None) -> Answer:
    if trace is not None:
        trace.update({"kept": 0, "dropped": 0, "draft_refs": [], "run_refs": []})
    return Answer(scenario=Scenario.general, context=AnswerContext(**context.model_dump()),
                  summary="Фейковый движок: ответ не строится.", confidence=Confidence.insufficient,
                  mode=AnswerMode.extractive, limitations=["фейковый движок eval/fake_engine.py"])
