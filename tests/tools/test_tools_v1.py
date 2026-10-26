"""Инструменты v1: источник у каждого результата, contract_id обязателен, нет строк чужого договора (ADR 0005)."""
from __future__ import annotations

import datetime as dt
import json

from dms_core import tools
from dms_core.store import KitStore
from dms_core.tools.clinics import name_score, normalize_name
from fixtures.synth_kit import make_group_docs, make_parsed


def _store() -> KitStore:
    st = KitStore(":memory:")
    st.save(make_parsed("c01"))
    st.save(make_parsed("c02"))
    g, docs, cls = make_group_docs()
    st.save_group(g, docs, cls)
    return st


def _raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc:
        return
    raise AssertionError(f"ожидалось {exc.__name__}")


def _all_calls(st: KitStore, cid: str) -> list[dict]:
    return [
        tools.contract_card(cid, store=st),
        tools.list_programs(cid, store=st),
        tools.search_clauses(cid, "чистка зубов франшиза", k=50, store=st),
        tools.get_clause(cid, number="3.1", store=st),
        tools.care_matrix(cid, "Стандарт", store=st),
        tools.clinics_query(cid, program="Стандарт", store=st),
        tools.clinics_query(cid, name="Медси", store=st),
        tools.clinics_query(cid, name="Улыбка", store=st),
        tools.payments_next(cid, "2026-03-01", store=st),
        tools.date_calc(cid, "in_period", date="2026-05-05", store=st),
    ]


def _contract_ids(obj) -> set[str]:
    """Все contract_id и префиксы doc_id в ответе инструмента (рекурсивно)."""
    found: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "contract_id" and isinstance(v, str):
                found.add(v)
            elif k in ("doc_id", "clause_id", "row_id") and isinstance(v, str):
                found.add(v.split(".")[0])
            else:
                found |= _contract_ids(v)
    elif isinstance(obj, list):
        for v in obj:
            found |= _contract_ids(v)
    return found


def test_tools_isolation_two_near_identical_contracts():
    st = _store()
    for cid in ("c01", "c02"):
        for res in _all_calls(st, cid):
            json.dumps(res, ensure_ascii=False)                      # JSON-совместимо
            assert _contract_ids(res) <= {cid}, res
    assert tools.clinics_query("c01", name="Медси", store=st)["rows"] == []
    assert tools.clinics_query("c02", name="Медси", store=st)["total"] == 3


def test_tools_require_contract_id():
    st = _store()
    for fn in (tools.contract_card, tools.list_programs, tools.care_matrix, tools.clinics_query):
        _raises(ValueError, fn, "", store=st)
    _raises(ValueError, tools.search_clauses, None, "зуб", store=st)
    _raises(ValueError, tools.get_clause, "", "1.1", store=st)
    _raises(ValueError, tools.payments_next, "", "2026-01-01", store=st)
    _raises(ValueError, tools.date_calc, "", "add", date="2026-01-01", store=st)


def test_results_carry_source():
    st = _store()
    hit = tools.search_clauses("c01", "чистка зубов", k=1, store=st)["hits"][0]
    assert hit["source"]["doc_id"] == "c01.program" and hit["source"]["number"] == "3.1" and hit["score"] > 0
    assert "AIR FLOW" in hit["text"]
    row = tools.clinics_query("c01", care_type="dental", store=st)["rows"][0]
    assert row["source"] == {"doc_id": "c01.clinics", "contract_id": "c01", "source_ref": "t0:r1",
                             "row_id": "c01.clinics#t0r1"}
    assert tools.contract_card("c01", store=st)["source"]["doc_id"] == "c01.contract"
    card = tools.contract_card("c01", store=st)
    assert {d["doc_id"] for d in card["documents"]} == {"c01.contract", "c01.program", "c01.clinics"}
    assert tools.care_matrix("c01", store=st)["rows"][0]["source"]["source_refs"] == ["t0:r1"]
    assert tools.list_programs("c01", store=st)["programs"][0]["premium_per_person"] == 50_000.0
    got = tools.get_clause("c01", path="Стоматология › 3.1", store=st)["clauses"]
    assert len(got) == 1 and got[0]["source"]["path"].endswith("3.1")


def test_clinics_query_filters():
    st = _store()
    q = tools.clinics_query
    assert normalize_name("ООО «Медси-2»") == "медси 2"
    assert q("c02", name="медси", store=st)["total"] == 3             # по юрлицу «АО «Медси»»
    assert q("c02", name="Медси на Белоруской", store=st)["total"] == 3  # опечатка — rapidfuzz
    assert q("c02", name="Совсем другая клиника", store=st)["total"] == 0
    assert q("c01", care_type="стоматология", store=st)["total"] == 1   # синоним → название вида помощи
    assert q("c01", care_type="на дому", store=st)["total"] == 1
    assert q("c01", care_type="home", store=st)["total"] == 1           # код из care_types.yaml
    assert q("c01", city="москва", store=st)["total"] == 3
    assert q("c01", city="Казань", store=st)["total"] == 0
    assert q("c01", address="Тверская", store=st)["total"] == 3
    assert q("c01", address="Твеская", store=st)["total"] == 3          # опечатка — rapidfuzz
    assert q("c01", franchise=10, store=st)["total"] == 3
    assert q("c01", franchise="—", store=st)["total"] == 0
    assert q("c02", franchise="—", store=st)["total"] == 3              # «—» ≠ 0 %
    assert q("c02", franchise=0, store=st)["total"] == 0
    assert q("c02", franchise="не указано", store=st)["total"] == 0
    assert q("c01", by_referral=True, store=st)["total"] == 1
    res = q("c01", limit=1, store=st)
    assert res["total"] == 3 and res["truncated"] and len(res["rows"]) == 1
    assert name_score("", make_parsed("c01").clinics[0]) == 0.0


def test_payments_and_dates():
    st = _store()
    p = tools.payments_next("c01", dt.date(2026, 3, 1), store=st)
    assert p["next"]["n"] == 2 and p["days_to_next"] == 136 and p["remaining_sum"] == 250_000.0
    assert p["remaining_sum_complete"] and p["source"]["doc_id"] == "c01.contract"
    p = tools.payments_next("c01", "01.01.2026", store=st)
    assert p["next"]["n"] == 1 and p["remaining_sum"] == 500_000.0
    assert tools.payments_next("c01", "2027-01-01", store=st)["next"] is None
    d = tools.date_calc
    assert d("c01", "add", date="2026-01-31", months=1, store=st)["result"] == "2026-02-28"
    assert d("c01", "add", date="2026-12-31", days=1, store=st)["result"] == "2027-01-01"
    assert d("c01", "diff", date="2026-01-01", other="2026-03-01", store=st)["result"] == 59
    assert d("c01", "in_period", date="2027-01-01", store=st)["result"] is False
    assert d("c01", "period", date="2026-12-01", store=st)["days_left"] == 30
    _raises(ValueError, d, "c01", "bogus", store=st)
    _raises(ValueError, d, "c01", "add", store=st)
    _raises(ValueError, d, "c01", "add", date="31/01/2026", store=st)


def test_tools_program_list_and_multi_care_row_ids():
    """Инструменты: фильтр программы по перечню «Стандарт, Бизнес»; id строки с несколькими видами помощи."""
    st = KitStore(":memory:")
    p = make_parsed("c01")
    base = p.clinics[0]
    for k, care in enumerate(["Стоматологическая помощь", "Телемедицина"]):
        p.clinics.append(base.model_copy(update={"id": f"c01.clinics#t0r9#{k}", "program": "Стандарт, Бизнес",
                                                 "care_type": care, "source_ref": "t0:r9"}))
    st.save(p)
    st.save(make_parsed("c02"))
    rows = tools.clinics_query("c01", program="Бизнес", store=st)["rows"]
    assert [r["source"]["row_id"] for r in rows] == ["c01.clinics#t0r9#0", "c01.clinics#t0r9#1"]
    assert tools.clinics_query("c01", program="Бизнес", care_type="телемедицина", store=st)["total"] == 1
    assert tools.clinics_query("c01", program="Стандарт", care_type="dental", store=st)["total"] == 2
    assert tools.clinics_query("c02", program="Бизнес", store=st)["total"] == 0
