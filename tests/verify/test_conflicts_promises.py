"""Старшинство документов и противоречия (A3, config/precedence.yaml); фильтр обещаний (forbidden_promises.yaml)."""
from __future__ import annotations

from dms_core import citations as C
from dms_core import precedence
from dms_core.config import load_yaml
from dms_core.conflicts import analyze
from dms_core.constants import DocType
from dms_core.promises import find_promises
from dms_core.schemas import Checks, DraftContradiction
from verify.vhelp import cite, clause, draft, store


def test_precedence_levels_from_config():
    assert precedence.level(DocType.amendment) < precedence.level(DocType.contract) < precedence.level("program")
    assert precedence.level("нечто") == load_yaml("precedence.yaml")["levels"]["other"]
    assert not precedence.is_contract_terms(DocType.catalog) and precedence.is_contract_terms("program")
    assert precedence.senior(2, 3) == 0 and precedence.senior(3, 2) == 1 and precedence.senior(3, 3) is None


def _verified(a, b):
    st = store()
    d = draft("c01", checks=Checks(care_in_program="yes"), contradictions=[DraftContradiction(a=cite(a), b=cite(b))])
    return C.verify(d, {a.id, b.id}, st, "c01")


def test_contradiction_of_different_levels_takes_senior():
    st = store()
    prog, contract = clause(st, "c01", "4.1"), clause(st, "c01", "1.2")   # программа (3) против договора (2)
    rep = analyze(_verified(prog, contract))
    assert rep.contradictory and rep.resolved_by_precedence   # позиция по старшему, статус — противоречие
    w = rep.warnings()[0]
    assert w.citations[0].doc_id == "c01.contract" and w.citations[1].doc_id == "c01.program"   # обе цитаты
    assert "старшему" in w.text


def test_contradiction_of_equal_levels_is_contradictory():
    st = store()
    a, b = clause(st, "c01", "3.1", "Стандарт"), clause(st, "c01", "4.1")    # оба — программа
    rep = analyze(_verified(a, b))
    assert rep.contradictory and len(rep.warnings()[0].citations) == 2


def test_promises_found_case_insensitive():
    text = "Страховая оплатит лечение. Мы ГАРАНТИРУЕМ результат. Ответ в течение 3 дней. Клиника уже добавлена."
    hits = find_promises(text)
    assert hits == ["Страховая оплатит", "ГАРАНТИРУЕМ", "в течение 3 дней", "Клиника уже добавлена"]
    assert find_promises("Просим сообщить, возможно ли добавление.") == []


def test_promise_allowed_only_in_listed_template():
    text = "Клиника добавлена в программу."
    assert find_promises(text) == ["Клиника добавлена"]
    assert find_promises(text, "hr_clinic_added") == []
    assert find_promises(text, "hr_no_clinic") == ["Клиника добавлена"]


def test_draft_templates_have_no_promises():
    templates = load_yaml("texts_ru/drafts.yaml")["templates"]
    for tid, t in templates.items():
        assert find_promises(f"{t.get('subject', '')}\n{t.get('body', '')}", tid) == [], tid
