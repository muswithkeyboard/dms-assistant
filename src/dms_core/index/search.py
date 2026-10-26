"""Поиск по пунктам ОДНОГО договора: BM25 + леммы pymorphy3 + синонимы видов помощи (план §4, §6.4; ADR 0005).

Изоляция: индекс строится из `store.clauses(contract_id)` — только пункты документов этого договора; общие документы
ГК (contract_id=None) и пункты других договоров в индекс не попадают физически. Кэш индексов — по паре
(хранилище, договор) с меткой `store.revision(contract_id)`: повторный импорт договора сбрасывает его индекс.

Расширение запроса (config/care_types.yaml): у каждого вида помощи есть название и синонимы. Если все леммы
какого-то названия/синонима есть в запросе («чистка зубов» ⊇ {«чистка»}), в запрос добавляются леммы всех
названий и синонимов этого вида («снятие налёта», «AIR FLOW», «гигиена полости рта», «стоматология» …) с весом
SYNONYM_WEIGHT — найденное по словам самого вопроса остаётся выше найденного по синонимам.
"""
from __future__ import annotations

import weakref
from collections.abc import Iterable
from dataclasses import dataclass, field

from ..config import load_yaml
from ..schemas import Clause
from ..store.db import KitStore, _require_contract_id, program_matches
from .bm25 import BM25
from .morph import lemmas

SYNONYM_WEIGHT = 0.5    # вес лемм, добавленных синонимами вида помощи (слова вопроса — 1.0)


@dataclass
class ContractIndex:
    contract_id: str
    revision: int | None
    clauses: list[Clause]
    bm25: BM25
    doc_types: dict[str, str] = field(default_factory=dict)     # doc_id → doc_type


_CACHE: weakref.WeakKeyDictionary[KitStore, dict[str, ContractIndex]] = weakref.WeakKeyDictionary()


def clause_terms(c: Clause) -> list[str]:
    """Что индексируется у пункта: номер, путь, текст и сноски (сноски вклеены в пункт, docs/CONTRACTS.md §1)."""
    parts = [c.number or "", c.path, c.text, *c.footnotes]
    return lemmas(" ".join(p for p in parts if p))


def build_index(store: KitStore, contract_id: str) -> ContractIndex:
    cid = _require_contract_id(contract_id)
    clauses = store.clauses(cid)
    doc_types = {d.id: d.doc_type.value for d in store.documents(cid)}
    return ContractIndex(cid, store.revision(cid), clauses, BM25([clause_terms(c) for c in clauses]), doc_types)


def get_index(store: KitStore, contract_id: str) -> ContractIndex:
    """Индекс договора из кэша; пересборка, если договор переимпортирован."""
    cid = _require_contract_id(contract_id)
    per_store = _CACHE.setdefault(store, {})
    idx = per_store.get(cid)
    if idx is None or idx.revision != store.revision(cid):
        idx = per_store[cid] = build_index(store, cid)
    return idx


def invalidate(contract_id: str | None = None) -> int:
    """Явный сброс индекса договора во всех хранилищах (None — всех договоров). Метка ревизии и так пересобирает
    индекс после save(); явный сброс нужен подключению клиента (ADR 0011), чтобы память старого индекса
    освобождалась сразу и после удаления договора. Возвращает число сброшенных индексов."""
    n = 0
    for per_store in list(_CACHE.values()):
        for cid in [c for c in per_store if contract_id is None or c == contract_id]:
            per_store.pop(cid, None)
            n += 1
    return n


def _care_phrases() -> list[list[list[str]]]:
    """Для каждого вида помощи — список фраз (название + синонимы) в виде лемм."""
    out = []
    for spec in (load_yaml("care_types.yaml") or {}).get("care_types", {}).values():
        phrases = [spec.get("name", ""), *spec.get("synonyms", [])]
        out.append([lm for lm in (lemmas(str(p)) for p in phrases) if lm])
    return out


def expand_query(query: str) -> dict[str, float]:
    """Запрос → {лемма: вес}. Слова вопроса — вес 1.0, леммы синонимов сработавшего вида помощи — SYNONYM_WEIGHT."""
    base = lemmas(query)
    weights = dict.fromkeys(base, 1.0)
    present = set(base)
    for phrases in _care_phrases():
        if any(set(p) <= present for p in phrases):
            for p in phrases:
                for lm in p:
                    weights.setdefault(lm, SYNONYM_WEIGHT)
    return weights


def search_clauses(store: KitStore, contract_id: str, query: str, k: int = 10, program: str | None = None,
                   doc_types: Iterable[str] | None = None) -> list[tuple[Clause, float]]:
    """Топ-k пунктов договора по запросу.

    program — пункты этой программы и общие пункты договора (program=None: тело договора, общие разделы);
    общий блок нескольких программ (program=«Стандарт, Бизнес») подходит любой из них;
    пункты других программ того же договора исключаются. doc_types — только документы этих типов.
    """
    idx = get_index(store, contract_id)
    allowed: set[int] | None = None
    if program is not None or doc_types is not None:
        types = {str(getattr(t, "value", t)) for t in doc_types} if doc_types is not None else None
        allowed = {
            i for i, c in enumerate(idx.clauses)
            if (program is None or c.program is None or program_matches(c.program, program))
            and (types is None or idx.doc_types.get(c.doc_id) in types)
        }
    hits = idx.bm25.top(expand_query(query), k, allowed)
    return [(idx.clauses[i], round(score, 4)) for i, score in hits]
