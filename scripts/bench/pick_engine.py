"""Движок для scripts/eval.py с моделируемым выбором клиники менеджером (ADR 0010): ответ → если в нём есть выбор
клиники, выбирается первый строго совпавший по названию кандидат (review_eval.simulated_picks без разметки) и
ответ пересчитывается с clinic_picks. Разметки «нет в перечне» в эталонах нет — «none» здесь не моделируется.

Запуск из app/: uv run python scripts/eval.py --engine scripts.bench.pick_engine:answer --run-id <id>
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_eval import simulated_picks


def answer(question: str, context: Any, **kw: Any) -> Any:
    from dms_core import engine

    out = engine.answer(question, context, **kw)
    picks = simulated_picks({}, out.clinic_choices) if out.clinic_choices else {}
    if not picks:
        return out
    if isinstance(kw.get("trace"), dict):
        kw["trace"].clear()
    return engine.answer(question, context, clinic_picks={c: v for c, (v, _h) in picks.items()}, **kw)
