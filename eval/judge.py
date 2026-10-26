"""Модель-судья ключевых фактов (план §10.1). Выключена по умолчанию: scripts/eval.py --judge-provider ….

Промпт — config/prompts/eval_judge.md (правило 5). Судья не видит эталонного ответа и документов: только список
фактов кейса и текст ответа ассистента; результат — доля названных фактов, рядом с грубой оценкой metrics.fact_present.
"""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

SCHEMA = {"type": "object", "properties": {"present": {"type": "array", "items": {"type": "boolean"}}},
          "required": ["present"]}


def make_judge(llm: Any):
    from dms_core.config import prompt, prompt_version
    from dms_core.llm.base import Message

    system = prompt("eval_judge")
    version = prompt_version("eval_judge")

    def judge(facts: Sequence[str], answer_text: str) -> dict:
        listing = "\n".join(f"{i}. {f}" for i, f in enumerate(facts, 1))
        user = f"Ключевые факты:\n{listing}\n\nОтвет ассистента:\n<<<\n{answer_text}\n>>>"
        turn = llm.chat([Message("system", system), Message("user", user)], None, json_schema=SCHEMA, max_tokens=512)
        present = parse_present(turn.text, len(facts))
        found = sum(present)
        return {"total": len(facts), "found": found, "share": found / len(facts) if facts else None,
                "prompt_version": version, "model": getattr(llm, "model", "?")}

    return judge


def parse_present(text: str, n: int) -> list[bool]:
    """{"present": [...]} из ответа модели (допускаем обрамление ```json). Длина не совпала — ValueError."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        raise ValueError("судья вернул не JSON")
    present = json.loads(m.group())["present"]
    if len(present) != n or not all(isinstance(x, bool) for x in present):
        raise ValueError("судья вернул список другой длины")
    return present
