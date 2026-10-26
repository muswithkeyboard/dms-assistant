"""Недостающие обязательные факты → уточняющие вопросы (`docs/rules.md` п. 2, план §6.6–6.8).

Список фактов и тексты вопросов — только из config/required_facts.yaml. Условия `when`:
- `context_is_group` — чат по всей ГК, договор не выбран;
- `program_affects_answer` — программа неизвестна, а у договора (или хотя бы у одного договора ГК) программ больше
  одной; если хранилище недоступно — считаем, что влияет (лучше спросить, чем перенести условия программы);
- `refusal_mentioned` — в обращении сообщается об отказе (слот `refusal`);
- неизвестное условие — вопрос задаётся (осторожный выбор).
Слот считается известным, если он есть в slots, а для `program` / `contract` — ещё и в контексте чата.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from ..config import load_yaml
from ..constants import Scenario
from ..schemas import ChatContext
from ..store.db import KitStore

# Слот закрывается другим слотом маршрутизатора: «стоматология» — и вид помощи, и услуга для SC 01.
SLOT_ALIASES = {"service": ("care_type",), "care_type": ("service",)}


def _program_count(context: ChatContext, store: KitStore | None) -> int | None:
    try:
        from ..tools._common import default_store

        st = store or default_store()
        if context.contract_id:
            return len(st.contract(context.contract_id).programs)
        if context.group_id:
            return max((len(c.programs) for c in st.contracts(context.group_id)), default=0)
    except (KeyError, ValueError, OSError, sqlite3.Error):   # нет базы / договора — решение «спросить» ниже
        return None
    return None


def _known(slot: str, slots: dict[str, Any], context: ChatContext) -> bool:
    if slots.get(slot):
        return True
    if any(slots.get(a) for a in SLOT_ALIASES.get(slot, ())):
        return True
    if slot == "program" and context.program:
        return True
    return slot == "contract" and bool(context.contract_id)


def _condition(when: str | None, slots: dict[str, Any], context: ChatContext, store: KitStore | None) -> bool:
    if not when:
        return True
    if when == "context_is_group":
        return context.contract_id is None and context.group_id is not None
    if when == "program_affects_answer":
        n = _program_count(context, store)
        return n is None or n > 1
    if when == "refusal_mentioned":
        return bool(slots.get("refusal"))
    if when == "clinic_unknown":
        # клиника названа — вывод «клиники нет в программе» возможен и без услуги (вопрос — не блокирующий)
        return not slots.get("clinic")
    if when == "clinic_known":
        return bool(slots.get("clinic"))
    return True


def split_missing(scenario: Scenario | str, slots: dict[str, Any], context: ChatContext, *,
                  store: KitStore | None = None) -> tuple[list[str], list[str]]:
    """(блокирующие, остальные) вопросы по недостающим фактам — config/required_facts.yaml, поле blocking.
    Блокирующий — без факта вывода нет (вопрос вместо ответа); остальные — ответ даётся, вопрос — в «неизвестно»."""
    spec = (load_yaml("required_facts.yaml") or {}).get(str(getattr(scenario, "value", scenario)), []) or []
    blocking: list[str] = []
    advisory: list[str] = []
    for item in spec:
        slot = item["slot"]
        if _known(slot, slots, context):
            continue
        if _condition(item.get("when"), slots, context, store):
            (blocking if item.get("blocking", True) else advisory).append(item["question"])
    return blocking, advisory


def missing_facts(scenario: Scenario | str, slots: dict[str, Any], context: ChatContext, *,
                  store: KitStore | None = None) -> list[str]:
    """Все вопросы по недостающим фактам (блокирующие, затем остальные) — порядок config/required_facts.yaml."""
    blocking, advisory = split_missing(scenario, slots, context, store=store)
    return blocking + advisory
