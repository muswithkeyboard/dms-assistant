"""Инструменты по пунктам: search_clauses, get_clause (план §6.3). Только пункты одного договора (ADR 0005)."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..index.search import expand_query
from ..index.search import search_clauses as _search
from ..store.db import KitStore
from ._common import clause_out, resolve


def search_clauses(contract_id: str, query: str, *, k: int = 10, program: str | None = None,
                   doc_types: Iterable[str] | None = None, store: KitStore | None = None) -> dict[str, Any]:
    """Топ-k пунктов договора (BM25 + леммы + синонимы видов помощи) с номерами и путями."""
    cid, st = resolve(contract_id, store)
    hits = _search(st, cid, query, k=k, program=program, doc_types=doc_types)
    return {
        "query_terms": expand_query(query),
        "hits": [clause_out(c, score=score) for c, score in hits],
    }


def get_clause(contract_id: str, number: str | None = None, path: str | None = None, *,
               store: KitStore | None = None) -> dict[str, Any]:
    """Пункт по номеру или пути; номера повторяются в разных документах — возвращаются все совпадения."""
    cid, st = resolve(contract_id, store)
    return {"clauses": [clause_out(c) for c in st.clause(cid, number=number, path=path)]}
