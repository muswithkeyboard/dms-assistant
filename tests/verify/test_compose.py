"""compose: форма ТЗ п. 8, статус ТЗ п. 8.1 (порядок CONTRACTS §7), контекст ГК — блоки по договорам без смешивания."""
from __future__ import annotations

from dms_core import citations as C
from dms_core.compose import GROUP_SUMMARY, compose, status_of, worst
from dms_core.constants import AnswerMode, Confidence, Label, Scenario
from dms_core.scenarios.clinics_pick import pick
from dms_core.schemas import AnswerContext, Checks, DraftAlternative, DraftContradiction
from verify.vhelp import cite, clause, draft, row, stmt, store


def _one(cid="c01", **kw):
    st = store()
    c = clause(st, cid, "1.1")
    d = draft(cid, stmt("Страховщик организует и оплачивает услуги", cite(c)), **kw)
    return st, C.verify(d, {c.id}, st, cid)


def test_status_order_function():
    assert status_of(contradictory=True, questions=True, has_evidence=False) is Confidence.contradictory
    assert status_of(contradictory=False, questions=True, has_evidence=False) is Confidence.need_clarification
    assert status_of(contradictory=False, questions=False, has_evidence=False) is Confidence.insufficient
    assert status_of(contradictory=False, questions=False, has_evidence=True) is Confidence.confirmed
    assert worst([Confidence.confirmed, Confidence.insufficient]) is Confidence.insufficient
    assert worst([]) is Confidence.insufficient


def test_status_confirmed_single_contract_form():
    _, v = _one()
    a = compose(Scenario.sc02_info, AnswerContext(contract_id="c01"), {"c01": v})
    assert a.confidence is Confidence.confirmed and a.summary == "вывод c01" and not a.per_contract
    cit = a.statements[0].citations[0]
    assert cit.doc_title == "contract c01" and cit.clause_number == "1.1" and cit.quote and cit.verified


def test_status_need_clarification():
    _, v = _one(questions=["Какая программа у сотрудника?"])
    a = compose(Scenario.sc01, AnswerContext(contract_id="c01"), {"c01": v}, label=Label.allowed)
    assert a.confidence is Confidence.need_clarification and a.questions == ["Какая программа у сотрудника?"]
    _, v2 = _one()
    b = compose(Scenario.sc01, AnswerContext(contract_id="c01"), {"c01": v2}, label=Label.allowed,
                questions=["Уточните клинику"])
    assert b.confidence is Confidence.need_clarification


def test_status_insufficient_without_live_refs():
    st = store()
    c = clause(st, "c01", "1.1")
    v = C.verify(draft("c01", stmt("выдумка", cite(c, "этого текста в пункте договора нет вообще"))), {c.id},
                 st, "c01")
    a = compose(Scenario.sc02_info, AnswerContext(contract_id="c01"), {"c01": v})
    assert a.confidence is Confidence.insufficient and a.statements == []


def test_status_insufficient_for_extractive_and_insufficient_label():
    _, v = _one()
    assert compose(Scenario.sc02_info, AnswerContext(contract_id="c01"), {"c01": v},
                   mode=AnswerMode.extractive).confidence is Confidence.insufficient
    assert compose(Scenario.sc01, AnswerContext(contract_id="c01"), {"c01": v},
                   label=Label.insufficient).confidence is Confidence.insufficient


def test_status_contradictory_beats_questions_and_shows_both_quotes():
    st = store()
    a, b = clause(st, "c01", "3.1", "Стандарт"), clause(st, "c01", "4.1")    # один уровень (программа)
    d = draft("c01", stmt("x", cite(a)), contradictions=[DraftContradiction(a=cite(a), b=cite(b))],
              checks=Checks(), questions=["вопрос"])
    v = C.verify(d, {a.id, b.id}, st, "c01")
    ans = compose(Scenario.sc01, AnswerContext(contract_id="c01"), {"c01": v}, label=Label.conditional)
    assert ans.confidence is Confidence.contradictory
    warn = [s for s in ans.statements if s.kind.value == "limitation"]
    assert len(warn) == 1 and {c.doc_id for c in warn[0].citations} == {"c01.program"}
    assert len(warn[0].citations) == 2


def test_different_levels_contradictory_with_both_quotes():
    # Решение автора: любое показанное противоречие → статус contradictory (как эталон К17)
    st = store()
    a, b = clause(st, "c01", "4.1"), clause(st, "c01", "1.2")
    d = draft("c01", stmt("x", cite(a)), contradictions=[DraftContradiction(a=cite(a), b=cite(b))], checks=Checks())
    v = C.verify(d, {a.id, b.id}, st, "c01")
    ans = compose(Scenario.sc01, AnswerContext(contract_id="c01"), {"c01": v}, label=Label.conditional)
    assert ans.confidence is Confidence.contradictory and ans.limitations
    assert any(len(s.citations) == 2 for s in ans.statements)


def test_group_two_contracts_with_identical_text_not_mixed():
    """ГК: у c01 и c02 пункт 1.1 одинаков. Ответ — блок на каждый договор, ссылки только своего договора;
    ссылка c02 в черновике c01 выброшена verify и не попадает ни в один блок."""
    st = store()
    c1, c2 = clause(st, "c01", "1.1"), clause(st, "c02", "1.1")
    d1 = draft("c01", stmt("страховщик обязуется оплатить услуги", cite(c1), cite(c2)), questions=["программа?"])
    d2 = draft("c02", stmt("страховщик обязуется оплатить услуги", cite(c2)))
    run = {c1.id, c2.id}
    v1, v2 = C.verify(d1, run, st, "c01"), C.verify(d2, run, st, "c02")
    assert C.foreign_refs(v1) == [c2.id]
    ans = compose(Scenario.sc02_info, AnswerContext(group_id="gk9"), {"c01": v1, "c02": v2},
                  labels={"c01": Label.allowed, "c02": None})
    assert ans.summary == GROUP_SUMMARY and not ans.statements
    assert [p.contract_id for p in ans.per_contract] == ["c01", "c02"]
    assert ans.per_contract[0].label is Label.allowed and ans.per_contract[1].label is None
    for block in ans.per_contract:
        cits = [c for s in block.statements for c in s.citations]
        assert cits and {c.contract_id for c in cits} == {block.contract_id}
        assert all(c.doc_id.startswith(block.contract_id + ".") for c in cits)
    assert ans.questions == ["c01: программа?"]
    assert ans.confidence is Confidence.need_clarification     # худший статус по договорам


def test_alternatives_from_pick_and_draft():
    st = store()
    r = row(st, "c01", "Амбулатор")
    alts = pick("c01", "Стандарт", "outpatient", store=st)
    v = C.verify(draft("c01", alternatives=[DraftAlternative(row_id=r.id, reason="модель")]), {r.id}, st, "c01",
                 program="Стандарт")
    ans = compose(Scenario.sc01, AnswerContext(contract_id="c01"), {"c01": v}, label=Label.denied,
                  label_note="есть альтернатива", alternatives=alts, store=st)
    assert [a.clinic.id for a in ans.alternatives] == [r.id]        # без дубля
    assert ans.label is Label.denied and ans.label_note == "есть альтернатива"
    assert ans.confidence is Confidence.insufficient                # утверждений со ссылками нет


def test_comparisons_separate():
    st = store()
    a = clause(st, "c01", "3.1", "Стандарт")
    v = C.verify(draft("c01", stmt("в «Стандарт» гигиена есть, в «Премиум» — нет", cite(a), kind="comparison")),
                 {a.id}, st, "c01")
    ans = compose(Scenario.sc02_info, AnswerContext(contract_id="c01"), {"c01": v})
    assert len(ans.comparisons) == 1 and not ans.statements


def test_empty_verified():
    ans = compose(Scenario.general, AnswerContext(), {})
    assert ans.confidence is Confidence.insufficient and ans.summary == ""
