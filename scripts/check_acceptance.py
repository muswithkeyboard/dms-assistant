#!/usr/bin/env python3
"""Приёмка волны 1 (план §13): для каждого из К10–К19 нужные пункты и строки находятся инструментами.

Источник ожиданий — data/derived/gold/acceptance_k10_k19.json (scripts/import_gold.py, ADR 0008). Для каждой ссылки
пробуем найти её так, как будет искать агент, только инструментами договора этой ссылки (ADR 0005):
  1) get_clause по номеру (или пути) → среди результатов есть пункт с этим id или с подсказкой цитаты;
  2) search_clauses по подсказке цитаты (топ-10) → то же;
  3) для строк перечня — clinics_query по программе с названием клиники или видом помощи из подсказки
     → строка с этим source_ref;
  4) строки-заголовки видов помощи в перечне (Ренессанс) — care_matrix(program) → source_refs содержат ссылку.
Строгие правила (после ревизии волны 1):
  * id — точно или потомок через разделитель (p, c, l, #, !, @): «…#t11r1» не совпадает с «…#t11r16»;
  * по тексту — только пункт того же документа с тем же номером, и ≥ 0,7 слов подсказки — в тексте самого пункта;
  * строки перечня клиник — только по id (никакого совпадения по тексту);
  * ссылка на id, которого нет в базе (например, строка-заголовок таблицы), — «нет в базе», это не находка.
Итог — таблица по кейсам и data/derived/acceptance_w1.json; код возврата 1, если найдено < 100 % обязательных.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dms_core.config import derived_dir
from dms_core.tools import care_matrix, clinics_query, get_clause, search_clauses

_WORD = re.compile(r"[\wё]+", re.IGNORECASE)


def _words(s: str) -> list[str]:
    return [w.casefold().replace("ё", "е") for w in _WORD.findall(s or "")]


def _quote_in(hint: str, text: str) -> bool:
    hw = _words(hint)
    if not hw:
        return False
    tw = set(_words(text))
    return sum(w in tw for w in hw) / len(hw) >= 0.7


def _id_match(ref: str, got: str) -> bool:
    """Точное совпадение или потомок: следующий знак после ref — разделитель, а не продолжение номера."""
    if not ref or not got:
        return False
    return got == ref or (got.startswith(ref) and got[len(ref)] in "pcl#!@")


def _norm_num(n: str | None) -> str:
    return (n or "").strip().rstrip(".")


def _hit(ref: dict, items: list[dict]) -> bool:
    want = ref.get("clause_id") or ref.get("table_ref") or ref.get("ref") or ""
    clinic_ref = ref.get("doc_type") == "clinics"
    for it in items:
        src = it.get("source", {})
        ids = [it.get("clause_id"), src.get("clause_id"), src.get("source_ref"), src.get("row_id")]
        if any(_id_match(want, i) for i in ids if i):
            return True
        if clinic_ref or not ref.get("quote_hint"):
            continue
        same_doc = src.get("doc_id") == ref.get("doc_id")
        same_num = ref.get("clause") and _norm_num(it.get("number")) == _norm_num(ref.get("clause"))
        if same_doc and same_num and _quote_in(ref["quote_hint"], it.get("text") or ""):
            return True
    return False


def find(ref: dict) -> str | None:
    """Каким способом ссылка находится инструментами; None — не найдена."""
    cid = ref["contract_id"]
    num, path, hint = ref.get("clause"), ref.get("path"), ref.get("quote_hint") or ""
    if num or path:
        got = get_clause(cid, number=None if path else num, path=path)["clauses"]
        if got and _hit(ref, got):
            return "get_clause"
    if hint:
        hits = search_clauses(cid, hint, k=10, program=ref.get("program"))["hits"]
        if _hit(ref, hits):
            return "search_clauses"
        hits = search_clauses(cid, hint, k=10)["hits"]
        if _hit(ref, hits):
            return "search_clauses(без программы)"
    if ref.get("table_ref"):
        # агент ищет клинику по названию или по виду помощи (подсказка — то или другое, плюс «also» из эталона)
        names = [hint, *[a for a in ref.get("also", []) if a != ref.get("program")]]
        for name in [n for n in names if n]:
            if _hit(ref, clinics_query(cid, program=ref.get("program"), name=name, limit=200)["rows"]):
                return "clinics_query(name)"
            if _hit(ref, clinics_query(cid, program=ref.get("program"), care_type=name, limit=200)["rows"]):
                return "clinics_query(care_type)"
        # строка-заголовок вида помощи: её цитирует матрица «программа × вид помощи»
        for m in care_matrix(cid, program=ref.get("program"))["rows"]:
            if any(_id_match(ref.get("table_ref") or "", r) for r in m["source"]["source_refs"]):
                return "care_matrix"
    return None


def main() -> int:
    acc = json.loads((derived_dir() / "gold" / "acceptance_k10_k19.json").read_text(encoding="utf-8"))
    report, total_req, found_req = {}, 0, 0
    for case, refs in acc["cases"].items():
        rows = []
        for ref in refs:
            how = find(ref)
            rows.append({"ref": ref.get("ref"), "clause": ref.get("clause"), "required": ref.get("required"),
                         "found_by": how})
            if ref.get("required"):
                total_req += 1
                found_req += how is not None
        req = [r for r in rows if r["required"]]
        ok = sum(r["found_by"] is not None for r in req)
        report[case] = {"required_found": ok, "required_total": len(req),
                        "optional_found": sum(r["found_by"] is not None for r in rows if not r["required"]),
                        "optional_total": sum(1 for r in rows if not r["required"]), "refs": rows}
        miss = [f"{r['clause'] or r['ref']}" for r in req if r["found_by"] is None]
        print(f"{case}: обязательных {ok}/{len(req)}" + (f"; не найдено: {', '.join(miss)}" if miss else ""))
    out = derived_dir() / "acceptance_w1.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nИтог: обязательных найдено {found_req}/{total_req}; отчёт: {out}")
    return 0 if found_req == total_req else 1


if __name__ == "__main__":
    sys.exit(main())
