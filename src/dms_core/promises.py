"""Фильтр запрещённых обещаний (ADR 0004, план §6.9, `docs/rules.md` п. 3). Правила — config/forbidden_promises.yaml.

Шаблон письма или итоговый текст ответа не должен обещать покрытие, сроки, согласование или «клиника добавлена».
Паттерн из `allow_in` допустим только в перечисленных шаблонах (например, «клиника добавлена» — в письме HR
после подтверждения СК).
"""
from __future__ import annotations

import re
from functools import cache

from .config import load_yaml


@cache
def _compiled() -> tuple[tuple[str, re.Pattern[str]], ...]:
    cfg = load_yaml("forbidden_promises.yaml") or {}
    return tuple((p, re.compile(p, re.IGNORECASE)) for p in cfg.get("patterns", []))


def _allowed(pattern: str, template_id: str | None) -> bool:
    allow = (load_yaml("forbidden_promises.yaml") or {}).get("allow_in", {}) or {}
    return template_id is not None and template_id in (allow.get(pattern) or [])


def find_promises(text: str, template_id: str | None = None) -> list[str]:
    """Найденные запрещённые фрагменты текста (в порядке появления); пусто — обещаний нет."""
    # Несколько правил могут совпасть с одним и тем же местом («гарантир…» и «гаранти…») — такой фрагмент
    # выдаётся один раз (ключ — начало и сам фрагмент).
    hits: set[tuple[int, str]] = set()
    for pattern, rx in _compiled():
        if _allowed(pattern, template_id):
            continue
        hits.update((m.start(), m.group(0)) for m in rx.finditer(text))
    return [frag for _, frag in sorted(hits)]
