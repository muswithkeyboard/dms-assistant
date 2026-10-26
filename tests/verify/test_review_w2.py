"""Исправления по ревизии волны 2 (дефекты 1–6): проверки только с доказательствами, цитата без «потерянного не»,
вывод без непроверенных чисел, absent по результату инструмента, ссылки карточки договора, альтернативы своей
программы. Синтетика tests/fixtures/synth_kit.py; на комплекте (data/derived/kit.sqlite) — SKIP без базы."""
from __future__ import annotations

from dms_core import citations as C
from dms_core.agent import ToolRecord, summarize
from dms_core.compose import compose
from dms_core.constants import Scenario
from dms_core.schemas import (
    AnswerContext,
    Checks,
    Clause,
    DraftAlternative,
    DraftCitation,
    DraftStatement,
)
from dms_core.store import KitStore
from dms_core.store.db import default_path
from dms_core.tools.registry import bind_tools
from fixtures.synth_kit import make_group_docs, make_parsed
from helpers import skip
from verify.vhelp import cite, clause, draft, row, stmt, store


def _why(v: C.Verified) -> dict[str, str]:
    return dict(v.dropped)


def _sup(text: str, *cits: DraftCitation, supports: list[str]) -> DraftStatement:
    return DraftStatement(text=text, citations=list(cits), supports=supports)


def _rec(st: KitStore, cid: str, tool_name: str, /, **args) -> ToolRecord:
    """Запись инструмента, как её делает агент: вызов настоящего инструмента договора."""
    from dms_core.tools.registry import collect_refs
    _, execute = bind_tools(cid, st)
    res = execute(tool_name, args)
    err = res.get("error")
    return ToolRecord(name=tool_name, args=args, refs=set() if err else collect_refs(res), error=err,
                      summary={} if err else summarize(tool_name, res),
                      result=dict(res) if tool_name in ("payments_next", "date_calc") and not err else None)


# ------------------------------------------------------------------ дефект 1: проверки без доказательств

def test_checks_without_supports_become_not_found():
    """Модель пишет yes/conditions без единого утверждения — метка из этого не строится."""
    st = store()
    d = draft("c01", checks=Checks(exclusion="yes", care_in_program="yes", clinic_in_program="yes",
                                   conditions=["franchise", "referral"]))
    v = C.verify(d, set(), st, "c01")
    ch = v.draft.checks
    assert (ch.exclusion, ch.care_in_program, ch.clinic_in_program) == ("not_found",) * 3
    assert ch.conditions == []
    assert _why(v)["checks.exclusion"] == C.R_NO_SUPPORT and _why(v)["checks.conditions.franchise"] == C.R_NO_SUPPORT


def test_checks_with_live_supports_stay():
    st = store()
    dental = clause(st, "c01", "3.1", "Стандарт")
    fr = clause(st, "c01", "4.1", "Стандарт")
    r = row(st, "c01", "Стомат")
    d = draft("c01",
              _sup("снятие налёта AIR FLOW входит", cite(dental), supports=["care_in_program"]),
              _sup("клиника Улыбка в перечне", DraftCitation(ref=r.id, quote="Улыбка"),
                   supports=["checks.clinic_in_program"]),
              _sup("франшиза по программе 10 процентов", cite(fr), supports=["conditions.franchise"]),
              checks=Checks(care_in_program="yes", clinic_in_program="yes", conditions=["franchise", "zone"]))
    v = C.verify(d, {dental.id, fr.id, r.id}, st, "c01")
    ch = v.draft.checks
    assert (ch.care_in_program, ch.clinic_in_program) == ("yes", "yes")
    assert ch.conditions == ["franchise"]                     # zone ничем не подтверждено


def test_supports_statement_dropped_then_check_dropped():
    """Подтверждающее утверждение с выдуманной цитатой выбрасывается — вместе с ним и проверка."""
    st = store()
    c = clause(st, "c01", "3.1", "Стандарт")
    d = draft("c01", _sup("лечение кариеса входит", cite(c, "лечение кариеса и удаление зубов входит в программу"),
                          supports=["exclusion"]), checks=Checks(exclusion="yes"))
    v = C.verify(d, {c.id}, st, "c01")
    assert v.draft.checks.exclusion == "not_found"


# ------------------------------------------------------------------ дефект 2: цитата и смысл

def test_removed_negation_rejected_synthetic():
    st = store()
    c = clause(st, "c02", "3.1", "Стандарт")          # «… Профессиональная гигиена не входит.»
    flipped = c.text.replace(" не ", " ", 1)
    d = draft("c02", stmt("профессиональная гигиена входит", cite(c, flipped)))
    v = C.verify(d, {c.id}, st, "c02")
    assert not v.draft.statements and _why(v)[c.id] == C.R_QUOTE_NOT_FOUND


def test_changed_number_rejected():
    st = store()
    c = clause(st, "c01", "4.1", "Стандарт")          # «Франшиза … составляет 10.0 процентов …»
    q = c.text.replace("10.0", "19.0")
    assert not C.quote_found(q, c.text)
    assert C.quote_found(c.text.replace("стоимости", "стоимсти"), c.text)   # опечатка — можно


def test_unrelated_statement_dropped():
    """Общая фраза пункта не подтверждает чужое утверждение (нет общих значимых слов)."""
    st = store()
    c = clause(st, "c01", "1.1")
    d = draft("c01", stmt("Стоматология без ограничений", cite(c, "обязуется организовать и оплатить медицинские")))
    v = C.verify(d, {c.id}, st, "c01")
    assert not v.draft.statements and _why(v)[c.id] == C.R_UNRELATED


def test_shown_quote_is_document_text_with_offsets():
    st = store()
    c = clause(st, "c01", "3.1", "Стандарт")
    q = "СНЯТИЕ  зубного «налета» методом AIR-FLOW"
    v = C.verify(draft("c01", stmt("налёт снимают", cite(c, q))), {c.id}, st, "c01")
    cit = v.citations[c.id]
    assert cit.offsets is not None and c.text[cit.offsets[0]:cit.offsets[1]] == cit.quote
    assert cit.quote == "снятие зубного налёта методом AIR FLOW"


def _kit() -> KitStore:
    if not default_path().is_file():
        skip("нет data/derived/kit.sqlite")
    return KitStore(default_path())


def test_kit_negation_flips_never_accepted():
    """Все пункты c01 и c09 с «не»: фрагмент с удалённым «не» не принимается ни разу (ревизия w2: было 90/90)."""
    st = _kit()
    tried = accepted = 0
    for cid in ("c01", "c09"):
        for c in st.clauses(cid):
            i = c.text.find(" не ")
            if i < 0:
                continue
            seg = c.text[max(0, i - 50): i + 60]
            flipped = seg.replace(" не ", " ", 1)
            if len(flipped) < C.MIN_QUOTE_CHARS:
                continue
            tried += 1
            accepted += C.quote_found(flipped, c.text)
    assert tried > 50 and accepted == 0, (tried, accepted)


# ------------------------------------------------------------------ дефект 3: summary

def test_summary_with_unverified_number_replaced():
    st = store()
    c = clause(st, "c01", "3.1", "Стандарт")
    d = draft("c01", stmt("снятие налёта AIR FLOW один раз в год", cite(c)),
              summary="Гигиена оплачивается 3 раза в год без франшизы 999 руб.")
    v = C.verify(d, {c.id}, st, "c01")
    a = compose(Scenario.sc02_info, AnswerContext(contract_id="c01"), {"c01": v}, store=st)
    assert a.summary == "снятие налёта AIR FLOW один раз в год"
    ok = compose(Scenario.sc02_info, AnswerContext(contract_id="c01"),
                 {"c01": C.verify(draft("c01", stmt("снятие налёта AIR FLOW один раз в год", cite(c)),
                                        summary="Снятие налёта — один раз в год (вопрос про 2026)"), {c.id}, st,
                                  "c01")}, store=st, question="Сколько раз в 2026 году?")
    assert ok.summary.startswith("Снятие налёта")


def test_summary_with_foreign_clinic_replaced_and_neutral_without_statements():
    st = store()
    c = clause(st, "c01", "3.1", "Стандарт")
    d = draft("c01", stmt("снятие налёта AIR FLOW один раз в год", cite(c)),
              summary="Можно обратиться в «Медси на Белорусской».")
    a = compose(Scenario.sc02_info, AnswerContext(contract_id="c01"), {"c01": C.verify(d, {c.id}, st, "c01")},
                store=st)
    assert "Медси" not in a.summary
    empty = compose(Scenario.sc02_info, AnswerContext(contract_id="c01"),
                    {"c01": C.verify(draft("c01", summary="Оплачивается 100 %"), set(), st, "c01")}, store=st)
    assert empty.summary and "100" not in empty.summary


# ------------------------------------------------------------------ дефект 4: absent по результату инструмента

def test_absent_care_needs_matrix_row_present_false_of_this_program():
    st = store()
    looked = ["матрица видов помощи программы «Стандарт»"]
    d = draft("c02", _sup("inpatient не входит в программу Стандарт", DraftCitation(ref="program#p5"),
                          supports=["care_in_program"]), checks=Checks(care_in_program="absent"), looked_at=looked)
    rec = _rec(st, "c02", "care_matrix", program="Стандарт")
    assert "program#p5" in rec.refs
    ok = C.verify(d, rec.refs, st, "c02", tools=[rec], program="Стандарт")
    assert ok.draft.checks.care_in_program == "absent", ok.dropped
    # без записей инструмента / другой программы / пустой матрицы — not_found
    assert C.verify(d, rec.refs, st, "c02").draft.checks.care_in_program == "not_found"
    assert C.verify(d, rec.refs, st, "c02", tools=[rec], program="Премиум").draft.checks.care_in_program == "not_found"
    empty = _rec(st, "c02", "care_matrix", program="несуществующая программа")
    assert not empty.refs
    v = C.verify(d, {"program#p5"}, st, "c02", tools=[empty])
    assert v.draft.checks.care_in_program == "not_found"
    # у c01 строка present=True: absent по ней невозможен
    d1 = d.model_copy(update={"contract_id": "c01"})
    rec1 = _rec(st, "c01", "care_matrix", program="Стандарт")
    assert C.verify(d1, rec1.refs, st, "c01", tools=[rec1], program="Стандарт").draft.checks.care_in_program \
        == "not_found"


def test_absent_clinic_needs_listing_and_missing_name():
    st = store()
    r = row(st, "c01", "Стомат")
    by_name = _rec(st, "c01", "clinics_query", program="Стандарт", name="Медси")
    listing = _rec(st, "c01", "clinics_query", program="Стандарт", care_type="Стоматологическая помощь")
    assert not by_name.refs and r.id in listing.refs
    d = draft("c01", _sup("стоматологию по программе Стандарт оказывает клиника Улыбка",
                          DraftCitation(ref=r.id, quote="Улыбка"), supports=["clinic_in_program"]),
              checks=Checks(clinic_in_program="absent"), looked_at=["перечень клиник программы «Стандарт»"])
    refs = by_name.refs | listing.refs
    assert C.verify(d, refs, st, "c01", tools=[by_name, listing]).draft.checks.clinic_in_program == "absent"
    # перечень не запрошен (только поиск по имени) — not_found
    assert C.verify(d, refs, st, "c01", tools=[by_name]).draft.checks.clinic_in_program == "not_found"
    # клиника на самом деле есть в перечне программы — not_found
    present = _rec(st, "c01", "clinics_query", program="Стандарт", name="Улыбка")
    v = C.verify(d, refs | present.refs, st, "c01", tools=[present, listing])
    assert v.draft.checks.clinic_in_program == "not_found" and _why(v)["checks.clinic_in_program"] == C.R_ABSENT_TOOL


def test_absent_exclusion_needs_search_with_hits():
    st = store()
    c = clause(st, "c01", "3.1", "Стандарт")
    d = draft("c01", _sup("снятие налёта AIR FLOW входит, в исключениях нет", cite(c), supports=["exclusion"]),
              checks=Checks(exclusion="absent"), looked_at=["поиск: исключения программы Стандарт"])
    hit = _rec(st, "c01", "search_clauses", query="снятие налёта")
    assert hit.summary["hits"] >= 1
    assert C.verify(d, {c.id}, st, "c01", tools=[hit]).draft.checks.exclusion == "absent"
    assert C.verify(d, {c.id}, st, "c01", tools=[]).draft.checks.exclusion == "not_found"


# ------------------------------------------------------------------ дефект 5: ссылки карточки договора

def _store_with_schedule() -> KitStore:
    """c01 с таблицей графика взносов в документе «договор» (строки — как у разборщика: …#tNrM)."""
    st = KitStore(":memory:")
    p = make_parsed("c01")
    for i, (due, amount) in enumerate([("15.01.2026", "250 000,00"), ("15.07.2026", "250 000,00")], 1):
        p.clauses.append(Clause(id=f"c01.contract#t6r{i}", doc_id="c01.contract", contract_id="c01",
                                text=f"{i} | {due} | {amount} | рубль", path="договор › график взносов"))
    st.save(p)
    st.save(make_parsed("c02"))
    g, docs, cls = make_group_docs()
    st.save_group(g, docs, cls)
    return st


def test_contract_doc_ref_with_quote_is_live():
    st = _store_with_schedule()
    card = _rec(st, "c01", "contract_card")
    assert "c01.contract" in card.refs
    good = draft("c01", stmt("срок действия договора один год",
                             DraftCitation(ref="c01.contract", quote="Срок действия договора составляет один год")))
    v = C.verify(good, card.refs, st, "c01", tools=[card])
    assert v.draft.statements and v.citations["c01.contract"].clause_number == "1.2"
    bad = draft("c01", stmt("Выдумка", DraftCitation(ref="c01.contract", quote="абсолютно произвольная цитата из головы")))
    assert not C.verify(bad, card.refs, st, "c01", tools=[card]).draft.statements


def test_payments_next_statement_confirmed_by_tool_result():
    st = _store_with_schedule()
    pay = _rec(st, "c01", "payments_next", date="2026-03-01")
    assert pay.result and pay.result["next"]["due"] == "2026-07-15"
    d = draft("c01", stmt("Ближайший взнос 15.07.2026 — 250 000 руб.", DraftCitation(ref="c01.contract")),
              stmt("Ближайший взнос 20.08.2026", DraftCitation(ref="c01.contract")))
    v = C.verify(d, pay.refs, st, "c01", tools=[pay])
    assert len(v.draft.statements) == 1
    cit = v.citation(v.draft.statements[0].citations[0])
    assert cit is not None and cit.table_ref == "c01.contract#t6r2" and "15.07.2026" in cit.quote
    assert C.R_CALC in _why(v).values()


def test_agent_run_refs_carry_tool_records():
    """AgentRun.refs → набор с записями: verify(run.draft, run.refs, …) видит результат payments_next."""
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parents[1] / "agent"))
    from fake_llm import FakeLLM, final, tool, tool_results

    from dms_core.agent import run_agent
    from dms_core.schemas import ChatContext

    st = _store_with_schedule()

    def answer(messages, _t, _s):
        r = tool_results(messages)[0]
        return final({"contract_id": "c01", "summary": "Ближайший взнос", "statements": [
            {"text": f"Ближайший взнос {r['next']['due']}", "citations": [{"ref": r["ref"], "quote": ""}]}]})

    run = run_agent(FakeLLM([tool("payments_next", date="2026-01-01"), answer]), "c01", "Когда взнос?",
                    scenario=Scenario.sc02_info, slots={}, context=ChatContext(contract_id="c01"), store=st)
    v = C.verify(run.draft, run.refs, st, "c01")
    assert len(v.draft.statements) == 1, v.dropped


# ------------------------------------------------------------------ дефект 6: альтернатива чужой программы

def test_alternative_without_program_needs_clinics_query_program():
    st = store()
    r = row(st, "c01", "Амбулатор")
    d = draft("c01", alternatives=[DraftAlternative(row_id=r.id, reason="r")])
    assert not C.verify(d, {r.id}, st, "c01").draft.alternatives          # программы нет нигде
    q_other = _rec(st, "c01", "clinics_query", program="Премиум")
    assert not C.verify(d, {r.id}, st, "c01", tools=[q_other]).draft.alternatives
    q_own = _rec(st, "c01", "clinics_query", program="Стандарт")
    assert C.verify(d, {r.id}, st, "c01", tools=[q_own]).draft.alternatives
    assert not C.verify(d, {r.id}, st, "c01", tools=[q_own], program="Премиум").draft.alternatives
