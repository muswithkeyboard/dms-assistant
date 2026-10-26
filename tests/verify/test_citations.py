"""citations.verify: живы только ссылки своего договора, из прогона и с найденной цитатой (план §6.5, ADR 0005)."""
from __future__ import annotations

from dms_core import citations as C
from dms_core.schemas import Checks, DraftAlternative, DraftCitation, DraftContradiction, DraftStatement
from verify.vhelp import cite, clause, draft, row, stmt, store


def _reasons(v: C.Verified) -> dict[str, str]:
    return dict(v.dropped)


def test_live_clause_citation_passes():
    st = store()
    c = clause(st, "c01", "3.1", "Стандарт")
    v = C.verify(draft("c01", stmt("снятие налёта AIR FLOW входит", cite(c, "снятие зубного налёта методом AIR FLOW"))),
                 {c.id}, st, "c01")
    assert len(v.draft.statements) == 1 and not v.dropped
    cit = v.citations[c.id]
    assert cit.verified and cit.contract_id == "c01" and cit.clause_number == "3.1"
    assert cit.doc_title == "program c01" and cit.offsets is not None
    assert C.dropped_share(v) == 0.0


def test_quote_normalization_and_fuzzy():
    st = store()
    c = clause(st, "c01", "3.1", "Стандарт")
    # другой регистр, «ё» → «е», лишние пробелы — точное совпадение после нормализации
    q1 = "СНЯТИЕ   зубного налета методом AIR FLOW"
    # одна опечатка — проходит (≤ 2 правки, отрицания и числа те же)
    q2 = "снятие зубного налёта методом AIR FLOV один раз в год"
    v = C.verify(draft("c01", stmt("налёт снимают", cite(c, q1)), stmt("налёт раз в год", cite(c, q2))),
                 {c.id}, st, "c01")
    assert len(v.draft.statements) == 2, v.dropped
    # на экран — текст документа, а не модели: опечатка «FLOV» не показывается
    shown = [v.citation(dc).quote for s in v.draft.statements for dc in s.citations]
    assert all("FLOW" in q and "FLOV" not in q for q in shown), shown


def test_fake_quote_dropped():
    st = store()
    c = clause(st, "c01", "3.1", "Стандарт")
    v = C.verify(draft("c01", stmt("лечение кариеса входит", cite(c, "лечение кариеса и удаление зубов входит в программу"))),
                 {c.id}, st, "c01")
    assert v.draft.statements == []
    assert _reasons(v)[c.id] == C.R_QUOTE_NOT_FOUND
    assert _reasons(v)["statements[0]"] == C.R_STATEMENT
    assert C.dropped_share(v) == 1.0


def test_short_quote_dropped():
    st = store()
    c = clause(st, "c01", "3.1", "Стандарт")
    v = C.verify(draft("c01", stmt("x", cite(c, "AIR FLOW"))), {c.id}, st, "c01")
    assert _reasons(v)[c.id] == C.R_QUOTE_SHORT and not v.draft.statements


def test_ref_not_in_run_dropped():
    st = store()
    c = clause(st, "c01", "3.1", "Стандарт")
    v = C.verify(draft("c01", stmt("x", cite(c))), set(), st, "c01")
    assert _reasons(v)[c.id] == C.R_NOT_IN_RUN and not v.draft.statements


def test_foreign_contract_ref_dropped_even_with_identical_text():
    """Пункт 1.1 у c01 и c02 дословно одинаков: ссылка на c02 в ответе c01 — перенос, отброс."""
    st = store()
    own, other = clause(st, "c01", "1.1"), clause(st, "c02", "1.1")
    assert own.text == other.text
    v = C.verify(draft("c01", stmt("страховщик обязуется оплатить услуги", cite(other)),
                       stmt("страховщик обязуется оплатить услуги", cite(own))), {own.id, other.id}, st, "c01")
    assert _reasons(v)[other.id] == C.R_FOREIGN
    assert C.foreign_refs(v) == [other.id]
    assert [s.citations[0].ref for s in v.draft.statements] == [own.id]


def test_foreign_clinic_row_and_alternative_dropped():
    st = store()
    r2 = row(st, "c02", "Стомат")
    d = draft("c01", stmt("x", DraftCitation(ref=r2.id, quote=r2.clinic_name)),
              alternatives=[DraftAlternative(row_id=r2.id, reason="r")])
    v = C.verify(d, {r2.id}, st, "c01")
    assert C.foreign_refs(v) == [r2.id, r2.id] and not v.draft.alternatives


def test_whole_draft_of_other_contract_dropped():
    st = store()
    c = clause(st, "c02", "1.1")
    v = C.verify(draft("c02", stmt("x", cite(c)), checks=Checks(care_in_program="yes")), {c.id}, st, "c01")
    assert not v.draft.statements and v.draft.checks is None
    assert C.R_FOREIGN in _reasons(v).values()


def test_group_rules_are_not_contract_terms():
    st = store()
    d = draft("c01", stmt("x", DraftCitation(ref="gk9.rules#p0", quote="чистка зубов и снятие налёта AIR FLOW")))
    v = C.verify(d, {"gk9.rules#p0"}, st, "c01")
    assert _reasons(v)["gk9.rules#p0"] == C.R_NOT_TERMS


def test_unknown_ref_dropped():
    st = store()
    v = C.verify(draft("c01", stmt("x", DraftCitation(ref="c01.program#p999", quote="x" * 30))),
                 {"c01.program#p999"}, st, "c01")
    assert _reasons(v)["c01.program#p999"] == C.R_UNKNOWN


def test_clinic_row_by_id_and_name():
    st = store()
    r = row(st, "c01", "Стомат")
    ok = C.verify(draft("c01", stmt("клиника Улыбка", DraftCitation(ref=r.id, quote="Улыбка"))), {r.id}, st, "c01")
    assert ok.citations[r.id].table_ref == r.source_ref and ok.citations[r.id].quote == r.clinic_name
    empty = C.verify(draft("c01", stmt("клиника Улыбка", DraftCitation(ref=r.id))), {r.id}, st, "c01")
    assert empty.draft.statements
    bad = C.verify(draft("c01", stmt("клиника Улыбка", DraftCitation(ref=r.id, quote="Медси на Белорусской"))),
                   {r.id}, st, "c01")
    assert _reasons(bad)[r.id] == C.R_NAME_MISMATCH


def test_absent_without_looked_at_becomes_not_found():
    """Даже с подтверждающим утверждением absent без просмотра (looked_at) — not_found (`docs/rules.md` п. 3а)."""
    st = store()
    c = clause(st, "c01", "3.1", "Стандарт")
    backer = DraftStatement(text="снятие налёта AIR FLOW", citations=[cite(c)],
                            supports=["care_in_program", "clinic_in_program", "exclusion"])
    d = draft("c01", backer, checks=Checks(care_in_program="absent", clinic_in_program="absent", exclusion="absent"))
    v = C.verify(d, {c.id}, st, "c01")
    ch = v.draft.checks
    assert (ch.care_in_program, ch.clinic_in_program, ch.exclusion) == ("not_found",) * 3
    assert _reasons(v)["checks.care_in_program"] == C.R_ABSENT


def test_absent_without_supports_becomes_not_found():
    st = store()
    d = draft("c01", checks=Checks(clinic_in_program="absent", care_in_program="absent"),
              looked_at=["перечень клиник программы «Стандарт»", "матрица видов помощи"])
    v = C.verify(d, set(), st, "c01")
    assert v.draft.checks.clinic_in_program == "not_found" and v.draft.checks.care_in_program == "not_found"
    assert _reasons(v)["checks.care_in_program"] == C.R_NO_SUPPORT


def test_absent_clinic_without_clinics_document_becomes_not_found():
    """У договора нет перечня клиник: «клиники нет в программе» сказать нельзя (`docs/rules.md` п. 3а)."""
    st = store(c03_without_clinics=True)
    d = draft("c03", checks=Checks(clinic_in_program="absent"), looked_at=["перечень клиник"])
    v = C.verify(d, set(), st, "c03")
    assert v.draft.checks.clinic_in_program == "not_found"


def test_alternative_same_program_only():
    st = store()
    r = row(st, "c01", "Амбулатор")
    d = draft("c01", alternatives=[DraftAlternative(row_id=r.id, reason="r"),
                                   DraftAlternative(row_id="c01.clinics#nope", reason="r")])
    ok = C.verify(d, {r.id, "c01.clinics#nope"}, st, "c01", program="Стандарт")
    assert [a.row_id for a in ok.draft.alternatives] == [r.id]
    assert _reasons(ok)["c01.clinics#nope"] == C.R_ALT_ROW
    other = C.verify(d, {r.id}, st, "c01", program="Премиум")
    assert not other.draft.alternatives and _reasons(other)[r.id] == C.R_ALT_PROGRAM
    not_run = C.verify(d, set(), st, "c01", program="Стандарт")
    assert not not_run.draft.alternatives


def test_care_matrix_ref():
    st = store()
    v = C.verify(draft("c01", stmt("inpatient входит", DraftCitation(ref="program#p5"))), {"program#p5"}, st,
                 "c01")
    cit = v.citations["program#p5"]
    assert cit.table_ref == "program#p5" and "inpatient" in cit.quote


def test_contradiction_needs_two_live_quotes():
    st = store()
    a, b = clause(st, "c01", "1.2"), clause(st, "c01", "4.1")
    pair = DraftContradiction(a=cite(a), b=cite(b, "этого фрагмента в пункте нет совсем никак"))
    v = C.verify(draft("c01", checks=Checks(documents="contradictory"), contradictions=[pair]), {a.id, b.id},
                 st, "c01")
    assert not v.draft.contradictions and v.draft.checks.documents == "consistent"
    good = C.verify(draft("c01", checks=Checks(), contradictions=[DraftContradiction(a=cite(a), b=cite(b))]),
                    {a.id, b.id}, st, "c01")
    assert good.draft.checks.documents == "contradictory"


def test_normalize_helpers():
    assert C.normalize("«Ёлка» —  тест") == '"елка" тест'
    assert C.quote_long_enough("коротко", "коротко")      # короткий пункт целиком
    assert not C.quote_found("", "текст")
