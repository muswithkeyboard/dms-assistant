"""Старшинство документов внутри договора (допущение A3, ADR 0006 п. 7). Правило — только config/precedence.yaml.

Меньше level — старше: допсоглашение (1) > договор (2) > программа (3) > перечень клиник (4) > памятка (5) > …
Типы из `not_contract_terms` (каталог страховщика) — не условия договора: в ответах о договоре не используются
(`docs/rules.md` п. 0а).
"""
from __future__ import annotations

from typing import Any

from .config import load_yaml
from .constants import DocType
from .schemas import Document


def _cfg() -> dict[str, Any]:
    return load_yaml("precedence.yaml") or {}


def level(doc_type: DocType | str) -> int:
    """Уровень типа документа из конфига; неизвестный тип — уровень `other`."""
    levels: dict[str, int] = _cfg().get("levels", {})
    key = str(getattr(doc_type, "value", doc_type))
    return int(levels.get(key, levels.get("other", 50)))


def doc_level(doc: Document) -> int:
    """Уровень конкретного документа: явный `precedence_level` (из разбора) важнее уровня типа."""
    return doc.precedence_level if doc.precedence_level is not None else level(doc.doc_type)


def is_contract_terms(doc_type: DocType | str) -> bool:
    """Документ этого типа содержит условия договора (не каталог и т. п. из `not_contract_terms`)."""
    key = str(getattr(doc_type, "value", doc_type))
    return key not in set(_cfg().get("not_contract_terms", []))


def senior(level_a: int, level_b: int) -> int | None:
    """Какая сторона старше: 0 — a, 1 — b, None — уровни равны (тогда это противоречие, ТЗ п. 8.1)."""
    if level_a == level_b:
        return None
    return 0 if level_a < level_b else 1
