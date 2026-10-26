"""Инструменты карточки договора: contract_card, list_programs, payments_next, date_calc (план §6.3).

Арифметика дат и сумм — здесь, а не в модели (план §6.4, К10, К16).
"""
from __future__ import annotations

import calendar
import datetime as dt
from typing import Any

from ..store.db import KitStore
from ._common import contract_source, resolve


def contract_card(contract_id: str, *, store: KitStore | None = None) -> dict[str, Any]:
    """Карточка договора + перечень загруженных документов со статусами + не предоставленные документы."""
    cid, st = resolve(contract_id, store)
    c = st.contract(cid)
    return {
        "contract": c.model_dump(mode="json"),
        "documents": [{"doc_id": d.id, "title": d.title, "doc_type": d.doc_type.value, "status": d.status.value}
                      for d in st.documents(cid)],
        "missing_docs": list(c.missing_docs),
        "source": contract_source(st, cid),
    }


def list_programs(contract_id: str, *, store: KitStore | None = None) -> dict[str, Any]:
    """Программы договора: название, премия на человека, численность, регионы."""
    cid, st = resolve(contract_id, store)
    c = st.contract(cid)
    return {"programs": [p.model_dump(mode="json") for p in c.programs], "source": contract_source(st, cid)}


def payments_next(contract_id: str, date: dt.date | str, *, store: KitStore | None = None) -> dict[str, Any]:
    """Ближайший взнос с датой ≥ date, оставшиеся взносы и их сумма (график взносов карточки договора).

    Взносы без даты не участвуют в выборе «ближайшего», но перечислены в `undated`.
    """
    cid, st = resolve(contract_id, store)
    on = _date(date)
    payments = st.contract(cid).payments
    dated = sorted((p for p in payments if p.due is not None), key=lambda p: p.due or on)
    rest = [p for p in dated if p.due is not None and p.due >= on]
    amounts = [p.amount for p in rest]
    return {
        "date": on.isoformat(),
        "next": rest[0].model_dump(mode="json") if rest else None,
        "days_to_next": (rest[0].due - on).days if rest and rest[0].due else None,
        "remaining": [p.model_dump(mode="json") for p in rest],
        "remaining_sum": round(sum(a for a in amounts if a is not None), 2) if amounts else 0.0,
        "remaining_sum_complete": all(a is not None for a in amounts),
        "undated": [p.model_dump(mode="json") for p in payments if p.due is None],
        "source": contract_source(st, cid),
    }


def date_calc(contract_id: str, op: str, *, date: dt.date | str | None = None, other: dt.date | str | None = None,
              days: int = 0, months: int = 0, store: KitStore | None = None) -> dict[str, Any]:
    """Арифметика дат в рамках договора.

    op: add — date + days + months; diff — дней от date до other; in_period — попадает ли date в срок страхования
    договора (period_from…period_to включительно); period — срок договора и дней до его окончания от date.
    """
    cid, st = resolve(contract_id, store)
    if op == "add":
        res = _add_months(_date(_need(date, "date")), months) + dt.timedelta(days=days)
        return {"op": op, "result": res.isoformat()}
    if op == "diff":
        return {"op": op, "result": (_date(_need(other, "other")) - _date(_need(date, "date"))).days}
    c = st.contract(cid)
    src = contract_source(st, cid)
    if op == "in_period":
        d = _date(_need(date, "date"))
        if c.period_from is None or c.period_to is None:
            return {"op": op, "result": None, "reason": "срок страхования в карточке договора не указан",
                    "source": src}
        return {"op": op, "result": c.period_from <= d <= c.period_to, "period_from": c.period_from.isoformat(),
                "period_to": c.period_to.isoformat(), "source": src}
    if op == "period":
        d = _date(date) if date is not None else None
        return {"op": op, "period_from": c.period_from.isoformat() if c.period_from else None,
                "period_to": c.period_to.isoformat() if c.period_to else None,
                "days_left": (c.period_to - d).days if d and c.period_to else None, "source": src}
    raise ValueError(f"неизвестная операция date_calc: {op}")


def _need(value: Any, name: str) -> Any:
    if value is None:
        raise ValueError(f"date_calc: нужен аргумент {name}")
    return value


def _date(value: dt.date | str) -> dt.date:
    """Дата из date/ISO-строки/«дд.мм.гггг»."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"не распознана дата: {value!r}")


def _add_months(d: dt.date, months: int) -> dt.date:
    """Сдвиг на месяцы с обрезкой дня до конца месяца (31.01 + 1 мес = 28/29.02)."""
    if not months:
        return d
    total = d.month - 1 + months
    y, m = d.year + total // 12, total % 12 + 1
    return d.replace(year=y, month=m, day=min(d.day, calendar.monthrange(y, m)[1]))
