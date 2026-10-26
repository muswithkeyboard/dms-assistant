"""Числа и даты в тексте для проверок «число не из инструментов» (агент, сборка ответа, проверка ссылок).

Правило (план §6.4, ревизия w2 дефекты 3 и 7): модель не считает и не придумывает чисел — каждое число и дата
утверждения или краткого вывода должны встретиться в результатах инструментов, тексте документа, вопросе или
контексте. Здесь — только приведение к общей форме, чтобы «31.01.2026», «2026-01-31» и «31 января 2026» были одним
значением, а «56 760,00», «56.760,00» и 56760.0 — одним числом.

Что не считается числом утверждения: номера пунктов и подпунктов («п. 2.6.1», «пункт 3», «раздел 4», «2.6.1»
из трёх и более частей) — это адрес ссылки, а не факт.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "мая": 5, "июн": 6, "июл": 7, "август": 8,
          "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}
_MONTH_RE = (r"(январ[яь]|феврал[яь]|марта?|апрел[яь]|ма[яй]|июн[яь]|июл[яь]|августа?|сентябр[яь]|"
             r"октябр[яь]|ноябр[яь]|декабр[яь])\b")
_DATE_WORDS = re.compile(rf"\b(\d{{1,2}})\s+{_MONTH_RE}(?:\s+(\d{{4}}))?(?:\s*г(?:ода|\.)?)?", re.IGNORECASE)
_DATE_RU = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
# Адрес пункта в утверждении: «п. 2.6», «пп. 3», «пункт 4.1», «подпункт 2», «раздел 5», «ст. 7».
_CLAUSE_ADDR = re.compile(r"(?:\bп{1,2}\.|\bпункт\w*|\bподпункт\w*|\bраздел\w*|\bст\.|\bстать\w*)\s*"
                          r"\d+(?:\.\d+)*\.?", re.IGNORECASE)
_MULTI_DOT = re.compile(r"\b\d+(?:\.\d+){2,}\.?")                     # 2.6.1 — номер пункта, не сумма
_SPACE = "[   ]"
_NUM = re.compile(
    r"\d{1,3}(?:\.\d{3})+,\d+"                     # 56.760,00 — точка разрядов, запятая дробной части
    rf"|\d{{1,3}}(?:{_SPACE}\d{{3}})+(?:[.,]\d+)?"  # 1 608 508,10
    r"|\d+(?:[.,]\d+)?"                             # 250000, 10,5, 3
)


def _month(word: str) -> int:
    w = word.casefold()
    for stem, n in sorted(MONTHS.items(), key=lambda kv: -len(kv[0])):
        if w.startswith(stem):
            return n
    raise ValueError(word)


def norm_num(s: str) -> str:
    """«56 760,00» / «56.760,00» / «56760.0» → «56760»; «10,5» → «10.5»."""
    s = re.sub(_SPACE, "", s)
    if "," in s and "." in s:                       # 56.760,00: точки — разряды
        s = s.replace(".", "")
    s = s.replace(",", ".")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.lstrip("0") or "0"


def numbers(text: str, *, default_year: int | None = None, skip_clause_numbers: bool = True) -> set[str]:
    """Значимые числа текста: даты → ГГГГ-ММ-ДД, суммы → «56760», дроби → «10.5», однозначные тоже.

    Дата словами без года («15 марта») — с годом default_year, если он задан, иначе как «03-15» (не совпадёт
    ни с одной полной датой: такое число в утверждении не подтвердится). Номера пунктов (skip_clause_numbers)
    не считаются.
    """
    out: set[str] = set()
    rest = text or ""

    def words(m: re.Match[str]) -> str:
        day, month, year = int(m.group(1)), _month(m.group(2)), m.group(3)
        y = int(year) if year else default_year
        try:
            out.add(dt.date(y, month, day).isoformat() if y else f"{month:02d}-{day:02d}")
        except ValueError:
            out.add(f"{month:02d}-{day:02d}")
        return " "

    rest = _DATE_WORDS.sub(words, rest)
    for m in _DATE_RU.finditer(rest):
        out.add(f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}")
    rest = _DATE_RU.sub(" ", rest)
    for m in _DATE_ISO.finditer(rest):
        out.add(m.group(0))
    rest = _DATE_ISO.sub(" ", rest)
    if skip_clause_numbers:
        rest = _CLAUSE_ADDR.sub(" ", rest)
        rest = _MULTI_DOT.sub(" ", rest)
    for m in _NUM.finditer(rest):
        out.add(norm_num(m.group(0)))
    return out


# Поля результатов инструментов, которые не являются фактами: адреса и служебные оценки (дефект 7 — число
# из ref «c01.contract#t136r3» не подтверждает «136 дней»).
ID_KEYS = frozenset({"ref", "refs", "doc_id", "id", "row_id", "clause_id", "source", "source_ref", "source_refs",
                     "name_score", "query_terms", "code", "truncated", "status"})


def known_numbers(values: list[Any], *, default_year: int | None = None) -> set[str]:
    """Числа значащих полей (тексты, суммы, даты, номера пунктов) результатов инструментов и строк контекста."""
    known: set[str] = set()

    def walk(v: Any) -> None:
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, int | float):
            known.add(norm_num(repr(float(v))) if isinstance(v, float) else str(v))
        elif isinstance(v, dt.date):
            known.add(v.isoformat())
        elif isinstance(v, str):
            # номера пунктов в тексте документа — тоже значения (утверждение может их не повторять)
            known.update(numbers(v, default_year=default_year, skip_clause_numbers=False))
        elif isinstance(v, dict):
            for k, x in v.items():
                if k not in ID_KEYS:
                    walk(x)
        elif isinstance(v, list | tuple | set):
            for x in v:
                walk(x)

    for r in values:
        walk(r)
    return known
