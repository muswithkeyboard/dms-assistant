"""Инструменты агента v1 поверх KitStore (план §6.3, ADR 0005).

У каждого `contract_id` — первый аргумент (без него ValueError), хранилище — именованный `store=` (по умолчанию
data/derived/kit.sqlite). Результат — JSON-совместимые данные с источником у каждого факта.
"""
from .clauses import get_clause, search_clauses
from .clinics import care_matrix, clinics_query
from .contract import contract_card, date_calc, list_programs, payments_next

__all__ = ["care_matrix", "clinics_query", "contract_card", "date_calc", "get_clause", "list_programs",
           "payments_next", "search_clauses"]
