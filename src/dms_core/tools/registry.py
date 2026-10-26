"""Реестр инструментов агента (docs/CONTRACTS.md §7, план §6.3, ADR 0005).

`bind_tools(contract_id, store)` возвращает описания инструментов для модели и исполнитель вызовов, в котором договор
уже привязан замыканием. Изоляция — кодом, а не промптом:
- в JSON Schema инструментов нет параметра `contract_id` (additionalProperties: false);
- исполнитель отвергает любой лишний аргумент, в том числе `contract_id`, — вызов не выполняется;
- каждый инструмент вызывается с привязанным `cid`, поэтому в результат попадают только строки этого договора.

Результаты компактные: длинные тексты обрезаются с пометкой «…[обрезано]», длинные списки — с `truncated`. У каждого
элемента — `ref` (clause_id пункта, id строки клиник, source_ref матрицы, doc_id договора для карточки): агент
собирает их в ToolRecord.refs, а citations.verify пропускает только ссылки из этого набора.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..llm.base import ToolSpec
from ..store.db import KitStore
from . import clauses, clinics, compare, contract
from ._common import resolve

TEXT_LIMIT = 700           # знаков текста пункта в результате
PATH_LIMIT = 160           # знаков пути пункта
RESULT_LIMIT = 14_000      # знаков JSON результата одного вызова (дальше — обрезка списков)
TRUNC = "…[обрезано]"

Executor = Callable[[str, dict[str, Any]], dict[str, Any]]


def _cut(s: str | None, n: int) -> str | None:
    if s is None or len(s) <= n:
        return s
    return s[:n].rstrip() + TRUNC


def _clause(c: dict[str, Any]) -> dict[str, Any]:
    out = {"ref": c["clause_id"], "doc_id": c["source"]["doc_id"], "number": c["number"],
           "path": _cut(c["path"], PATH_LIMIT), "program": c["program"], "text": _cut(c["text"], TEXT_LIMIT)}
    if c.get("footnotes"):
        out["footnotes"] = [_cut(f, 300) for f in c["footnotes"]]
    return out


def _clinic(r: dict[str, Any]) -> dict[str, Any]:
    keep = ("program", "care_type", "clinic_name", "legal_name", "address", "city", "code", "franchise_pct",
            "by_referral", "access_note", "name_score")
    out = {"ref": r["source"]["row_id"], "doc_id": r["source"]["doc_id"]}
    out.update({k: r[k] for k in keep if r.get(k) not in (None, "", False) or k == "by_referral"})
    if "access_note" in out:
        out["access_note"] = _cut(out["access_note"], 300)
    return out


def _source_ref(src: dict[str, Any]) -> str | None:
    """Ссылка на карточку договора — doc_id документа «договор» (карточку строит адаптер из него)."""
    return src.get("doc_id")


# ---------------------------------------------------------------- обёртки инструментов (договор уже привязан)

def _t_contract_card(cid: str, st: KitStore) -> dict[str, Any]:
    r = contract.contract_card(cid, store=st)
    c = r["contract"]
    card = {k: c[k] for k in ("legal_entity", "number", "insurer", "date", "period_from", "period_to", "headcount")}
    card["franchise_declared"] = c["franchise"]["declared"]
    card["programs"] = [p["name"] for p in c["programs"]]
    card["payments_count"] = len(c["payments"])
    return {"ref": _source_ref(r["source"]), "contract": card, "documents": r["documents"],
            "missing_docs": r["missing_docs"]}


def _t_list_programs(cid: str, st: KitStore) -> dict[str, Any]:
    r = contract.list_programs(cid, store=st)
    progs = [{k: v for k, v in p.items() if v not in (None, [], "")} for p in r["programs"]]
    for p in progs:
        if "notes" in p:
            p["notes"] = _cut(p["notes"], 300)
    return {"ref": _source_ref(r["source"]), "programs": progs}


def _t_search_clauses(cid: str, st: KitStore, query: str, program: str | None = None,
                      doc_types: list[str] | None = None, k: int = 8) -> dict[str, Any]:
    k = max(1, min(int(k), 15))
    r = clauses.search_clauses(cid, query, k=k, program=program, doc_types=doc_types, store=st)
    return {"query_terms": sorted(r["query_terms"]), "hits": [_clause(h) for h in r["hits"]]}


def _t_get_clause(cid: str, st: KitStore, number: str | None = None, path: str | None = None) -> dict[str, Any]:
    if not number and not path:
        raise ValueError("нужен номер пункта (number) или путь (path)")
    r = clauses.get_clause(cid, number=number or None, path=path or None, store=st)
    found = [_clause(c) for c in r["clauses"][:10]]
    return {"clauses": found, "total": len(r["clauses"]), "truncated": len(r["clauses"]) > 10}


def _t_care_matrix(cid: str, st: KitStore, program: str | None = None) -> dict[str, Any]:
    r = clinics.care_matrix(cid, program, store=st)
    rows = [{"ref": (m["source"]["source_refs"] or [None])[0], "refs": m["source"]["source_refs"],
             "program": m["program"], "care_type": m["care_type"], "present": m["present"]} for m in r["rows"]]
    return {"rows": rows, "programs_found": sorted({m["program"] for m in rows})}


def _t_clinics_query(cid: str, st: KitStore, program: str | None = None, care_type: str | None = None,
                     name: str | None = None, city: str | None = None, address: str | None = None,
                     franchise: float | str | None = None, by_referral: bool | None = None,
                     limit: int = 20) -> dict[str, Any]:
    limit = max(1, min(int(limit), 50))
    r = clinics.clinics_query(cid, program=program, care_type=care_type, name=name, city=city, address=address,
                              franchise=franchise, by_referral=by_referral, limit=limit, store=st)
    return {"rows": [_clinic(x) for x in r["rows"]], "total": r["total"], "truncated": r["truncated"]}


def _t_compare_programs(cid: str, st: KitStore, a: str, b: str) -> dict[str, Any]:
    return compare.compare_programs(cid, a, b, store=st)


def _t_payments_next(cid: str, st: KitStore, date: str) -> dict[str, Any]:
    r = contract.payments_next(cid, date, store=st)
    r["ref"] = _source_ref(r.pop("source"))
    return r


def _t_date_calc(cid: str, st: KitStore, op: str, date: str | None = None, other: str | None = None,
                 days: int = 0, months: int = 0) -> dict[str, Any]:
    r = contract.date_calc(cid, op, date=date, other=other, days=int(days), months=int(months), store=st)
    if "source" in r:
        r["ref"] = _source_ref(r.pop("source"))
    return r


# ---------------------------------------------------------------- описания для модели (без contract_id)

def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


_S = {"type": "string"}
_PROGRAM = {"type": "string", "description": "Название программы, как в list_programs"}

_TOOLS: dict[str, tuple[Callable[..., dict[str, Any]], str, dict[str, Any]]] = {
    "contract_card": (_t_contract_card,
                      ("Карточка выбранного договора: юрлицо, номер, страховщик, срок, программы, загруженные "
                      "документы со статусами и непредоставленные документы."), _obj({})),
    "list_programs": (_t_list_programs, "Программы договора: название, премия на человека, численность, регионы.",
                      _obj({})),
    "search_clauses": (_t_search_clauses,
                       ("Поиск пунктов документов договора по словам (леммы и синонимы видов помощи). Пиши запрос "
                       "языком документа: «снятие зубных отложений», «исключения», «франшиза». Возвращает пункты "
                       "с ref, номером, путём и текстом."),
                       _obj({"query": _S, "program": _PROGRAM,
                             "doc_types": {"type": "array", "items": {"type": "string", "enum": [
                                 "contract", "amendment", "program", "clinics", "memo", "insurer_reply", "other"]},
                                 "description": "Ограничить типами документов"},
                             "k": {"type": "integer", "minimum": 1, "maximum": 15}}, ["query"])),
    "get_clause": (_t_get_clause, "Пункт по номеру («2.6.1») или по пути / окончанию пути.",
                   _obj({"number": _S, "path": _S})),
    "care_matrix": (_t_care_matrix,
                    ("Матрица «программа × вид помощи»: present=true — вид помощи в программе есть, false — в "
                    "документе программы указано, что его нет. Вида нет в строках — в документе не указан."),
                    _obj({"program": _PROGRAM})),
    "clinics_query": (_t_clinics_query,
                      ("Строки перечня клиник договора по фильтрам: программа, вид помощи (код или слова), "
                      "нечёткое имя клиники, город, адрес, франшиза, «по направлению». Возвращает total."),
                      _obj({"program": _PROGRAM, "care_type": _S, "name": _S, "city": _S, "address": _S,
                            "franchise": {"type": ["number", "string"]}, "by_referral": {"type": "boolean"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 50}})),
    "compare_programs": (_t_compare_programs,
                         "Разница двух программ договора: виды помощи и клиники, которые есть только в одной.",
                         _obj({"a": _PROGRAM, "b": _PROGRAM}, ["a", "b"])),
    "payments_next": (_t_payments_next,
                      "Ближайший взнос по графику с датой ≥ date, оставшиеся взносы и их сумма (считает код).",
                      _obj({"date": {"type": "string", "description": "ГГГГ-ММ-ДД"}}, ["date"])),
    "date_calc": (_t_date_calc,
                  ("Арифметика дат: add (date + days + months), diff (дней от date до other), in_period (входит ли "
                  "date в срок страхования договора), period (срок договора и дней до конца от date)."),
                  _obj({"op": {"type": "string", "enum": ["add", "diff", "in_period", "period"]},
                        "date": {"type": "string", "description": "ГГГГ-ММ-ДД"}, "other": _S,
                        "days": {"type": "integer"}, "months": {"type": "integer"}}, ["op"])),
}

TOOL_NAMES = tuple(_TOOLS)


def _check_args(name: str, args: dict[str, Any]) -> str | None:
    """Аргументы по схеме инструмента: только известные параметры, обязательные на месте. Ошибка — по-русски."""
    schema = _TOOLS[name][2]
    extra = sorted(set(args) - set(schema["properties"]))
    if "contract_id" in args:
        return "параметр contract_id не принимается: договор выбран в чате и привязан к инструментам"
    if extra:
        return f"неизвестные параметры: {', '.join(extra)}"
    missing = [p for p in schema["required"] if args.get(p) in (None, "")]
    if missing:
        return f"нет обязательных параметров: {', '.join(missing)}"
    return None


def _fit(result: dict[str, Any], limit: int = RESULT_LIMIT) -> dict[str, Any]:
    """Обрезка самого длинного списка результата, пока JSON не уложится в limit (с пометкой truncated)."""
    while len(json.dumps(result, ensure_ascii=False, default=str)) > limit:
        lists = [(k, v) for k, v in result.items() if isinstance(v, list) and len(v) > 1]
        if not lists:
            break
        key, longest = max(lists, key=lambda kv: len(kv[1]))
        result[key] = longest[: max(1, len(longest) * 2 // 3)]
        result["truncated"] = True
    return result


def bind_tools(contract_id: str, store: KitStore | None = None) -> tuple[list[ToolSpec], Executor]:
    """Инструменты, привязанные к одному договору. Исполнитель: (имя, аргументы модели) → JSON-результат.

    Ошибки (неизвестный инструмент, лишний/недостающий параметр, ValueError/KeyError инструмента) возвращаются
    как {"error": "..."} — модель видит причину и может исправить вызов; исключения наружу не выходят.
    """
    cid, st = resolve(contract_id, store)
    st.contract(cid)   # договор должен существовать: KeyError — сразу, до модели
    specs = [ToolSpec(name=n, description=d, parameters=p) for n, (_, d, p) in _TOOLS.items()]

    def execute(name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name not in _TOOLS:
            return {"error": f"нет инструмента «{name}». Доступны: {', '.join(TOOL_NAMES)}"}
        if not isinstance(args, dict):
            return {"error": "аргументы инструмента — JSON-объект"}
        problem = _check_args(name, args)
        if problem:
            return {"error": problem}
        fn = _TOOLS[name][0]
        try:
            # договор подставляет код (cid из замыкания), а не модель
            return _fit(fn(cid, st, **args))
        except (ValueError, KeyError, TypeError) as exc:
            return {"error": f"{name}: {exc}"}

    return specs, execute


def collect_refs(obj: Any) -> set[str]:
    """Все ref / refs результата инструмента (рекурсивно) — набор, по которому citations.verify пускает ссылки."""
    found: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "ref" and isinstance(v, str):
                found.add(v)
            elif k == "refs" and isinstance(v, list):
                found |= {x for x in v if isinstance(x, str)}
            else:
                found |= collect_refs(v)
    elif isinstance(obj, list):
        for v in obj:
            found |= collect_refs(v)
    return found
