"""Поиск по пунктам одного договора: BM25 + морфология (план §4, ADR 0005)."""
from .search import expand_query, search_clauses

__all__ = ["expand_query", "search_clauses"]
