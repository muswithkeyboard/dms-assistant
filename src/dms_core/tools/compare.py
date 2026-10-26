"""Инструмент compare_programs (план §6.3): чем различаются две программы одного договора.

Сравниваются (только строки договора `contract_id`, ADR 0005):
- матрица видов помощи (config/care_types → CareMatrix): вид помощи есть в одной программе и нет / не указан в другой;
- перечень клиник: пара «клиника × вид помощи» есть в одной программе и нет в другой.
Клиника сравнивается по нормализованному имени (`normalize_name`: без ОПФ, кавычек, регистра), вид помощи — по
строке документа без регистра. Тексты условий программ сравнивает модель через search_clauses (по программе).
"""
from __future__ import annotations

from typing import Any

from ..store.db import KitStore
from ._common import resolve
from .clinics import normalize_name


def compare_programs(contract_id: str, a: str, b: str, *, limit: int = 30,
                     store: KitStore | None = None) -> dict[str, Any]:
    """Разница программ a и b договора: виды помощи и клиники «только в a» / «только в b»."""
    cid, st = resolve(contract_id, store)
    if not a or not b:
        raise ValueError("нужны названия двух программ")

    def care(program: str) -> dict[str, dict[str, Any]]:
        return {m.care_type.casefold(): {"care_type": m.care_type, "present": m.present,
                                          "refs": list(m.source_refs)} for m in st.care(cid, program)}

    def clinics(program: str) -> dict[tuple[str, str], dict[str, Any]]:
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for r in st.clinics(cid, program=program):
            key = (normalize_name(r.clinic_name), r.care_type.casefold())
            out.setdefault(key, {"clinic_name": r.clinic_name, "care_type": r.care_type, "address": r.address,
                                 "ref": r.id})
        return out

    care_a, care_b = care(a), care(b)
    care_diff = []
    for key in sorted(set(care_a) | set(care_b)):
        pa, pb = care_a.get(key), care_b.get(key)
        in_a, in_b = bool(pa and pa["present"]), bool(pb and pb["present"])
        if in_a != in_b:
            src = pa or pb or {}
            care_diff.append({
                "care_type": src.get("care_type"),
                a: "есть" if in_a else ("нет" if pa else "не указано"),
                b: "есть" if in_b else ("нет" if pb else "не указано"),
                "refs": sorted({*(pa or {}).get("refs", []), *(pb or {}).get("refs", [])}),
            })

    cl_a, cl_b = clinics(a), clinics(b)
    only_a = [cl_a[k] for k in sorted(set(cl_a) - set(cl_b))]
    only_b = [cl_b[k] for k in sorted(set(cl_b) - set(cl_a))]
    return {
        "programs": [a, b],
        "programs_found": {a: bool(care_a or cl_a), b: bool(care_b or cl_b)},
        "care_diff": care_diff,
        "clinics_only_in_a": only_a[:limit], "clinics_only_in_a_total": len(only_a),
        "clinics_only_in_b": only_b[:limit], "clinics_only_in_b_total": len(only_b),
        "clinics_common_total": len(set(cl_a) & set(cl_b)),
    }
