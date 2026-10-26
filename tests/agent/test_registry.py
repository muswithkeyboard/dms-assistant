"""Реестр инструментов: договор привязан кодом, у модели нет contract_id, результаты компактны и с ref (ADR 0005)."""
from __future__ import annotations

import json

from agent.agent_kit import agent_store
from dms_core.tools.compare import compare_programs
from dms_core.tools.registry import TOOL_NAMES, TRUNC, bind_tools, collect_refs

EXPECTED = {"contract_card", "list_programs", "search_clauses", "get_clause", "care_matrix", "clinics_query",
            "compare_programs", "payments_next", "date_calc"}

CALLS = [
    ("contract_card", {}), ("list_programs", {}),
    ("search_clauses", {"query": "стоматология гигиена франшиза", "k": 15}),
    ("get_clause", {"number": "3.1"}), ("care_matrix", {"program": "Стандарт"}),
    ("clinics_query", {"program": "Стандарт"}), ("clinics_query", {"name": "Медси"}),
    ("compare_programs", {"a": "Стандарт", "b": "Премиум"}),
    ("payments_next", {"date": "2026-03-01"}), ("date_calc", {"op": "in_period", "date": "2026-05-05"}),
]


def test_specs_have_no_contract_id():
    specs, _ = bind_tools("c01", agent_store())
    assert {s.name for s in specs} == EXPECTED == set(TOOL_NAMES)
    for s in specs:
        assert s.parameters["type"] == "object"
        assert s.parameters["additionalProperties"] is False
        assert "contract_id" not in s.parameters["properties"]
        assert "contract_id" not in json.dumps(s.parameters)


def test_contract_id_argument_is_refused():
    _, run = bind_tools("c01", agent_store())
    for name, args in CALLS:
        r = run(name, {**args, "contract_id": "c02"})
        assert set(r) == {"error"} and "contract_id" in r["error"], name


def test_results_only_from_bound_contract():
    st = agent_store()
    for cid, other_marks in (("c01", ["Медси", "Грузинский", "лечение кариеса"]),
                             ("c02", ["Улыбка", "Жемчуг", "AIR FLOW", "игнорируй"])):
        _, run = bind_tools(cid, st)
        for name, args in CALLS:
            r = run(name, args)
            dump = json.dumps(r, ensure_ascii=False)
            assert "error" not in r, (cid, name, r)
            for mark in other_marks:
                assert mark not in dump, (cid, name, mark)
            # общие правила СК группы — не условия договора (`docs/rules.md` п. 0а)
            assert "оплачиваются всегда" not in dump
            for ref in collect_refs(r):
                if "." in ref:
                    assert ref.split(".")[0] == cid, (cid, name, ref)


def test_every_item_has_ref():
    _, run = bind_tools("c01", agent_store())
    hits = run("search_clauses", {"query": "стоматология", "k": 10})["hits"]
    assert hits and all(h["ref"].startswith("c01.") for h in hits)
    rows = run("clinics_query", {"program": "Стандарт"})["rows"]
    assert rows and all(r["ref"].startswith("c01.clinics#") for r in rows)
    care = run("care_matrix", {"program": "Стандарт"})["rows"]
    assert care and all(c["ref"] and c["refs"] for c in care)
    assert run("contract_card", {})["ref"] == "c01.contract"
    assert run("payments_next", {"date": "2026-03-01"})["ref"] == "c01.contract"


def test_long_text_is_cut_with_mark():
    _, run = bind_tools("c01", agent_store())
    r = run("get_clause", {"number": "8.1"})
    text = r["clauses"][0]["text"]
    assert text.endswith(TRUNC) and len(text) < 800


def test_errors_are_results_not_exceptions():
    _, run = bind_tools("c01", agent_store())
    assert "нет инструмента" in run("drop_table", {})["error"]
    assert "неизвестные параметры" in run("search_clauses", {"query": "x", "limit": 3})["error"]
    assert "обязательных" in run("search_clauses", {})["error"]
    assert "error" in run("date_calc", {"op": "teleport", "date": "2026-01-01"})
    assert "error" in run("payments_next", {"date": "вчера"})
    assert "error" in run("get_clause", {})


def test_payments_arithmetic_is_code():
    _, run = bind_tools("c01", agent_store())
    r = run("payments_next", {"date": "2026-03-01"})
    assert r["next"]["due"] == "2026-07-15" and r["days_to_next"] == 136 and r["remaining_sum"] == 250000.0


def test_compare_programs_diff():
    r = compare_programs("c01", "Стандарт", "Премиум", store=agent_store())
    only_b = [x["clinic_name"] for x in r["clinics_only_in_b"]]
    assert only_b == ["Клиника «Жемчуг»"]
    assert r["clinics_only_in_a_total"] == 3
    diff = {d["care_type"]: d for d in r["care_diff"]}
    assert diff["inpatient"]["Стандарт"] == "есть" and diff["inpatient"]["Премиум"] == "нет"
    assert "dental" not in diff


def test_unknown_contract_fails_before_model():
    try:
        bind_tools("c99", agent_store())
    except KeyError:
        return
    raise AssertionError("ожидалась ошибка для неизвестного договора")
