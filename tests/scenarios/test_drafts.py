"""Черновики писем (ADR 0004, план §6.9): «[ЗАПОЛНИТЬ]», отсутствие обещаний во всех шаблонах с любыми полями,
подстановка без повторного раскрытия скобок, поля из карточки договора."""
from __future__ import annotations

import itertools
import random
import re

from dms_core.config import load_yaml
from dms_core.constants import AddClinicState as S
from dms_core.constants import Confidence, Scenario
from dms_core.promises import find_promises
from dms_core.scenarios import drafts
from dms_core.scenarios.clinics_pick import pick
from dms_core.schemas import Answer, AnswerContext, Request
from scenarios.test_add_clinic import kit

FIELDS = {
    "legal_entity": "Компания 1", "contract": "№ Д-1", "program": "«Стандарт»", "clinic": "Клиника «Жемчуг»",
    "care_type": "стоматология", "address": "г. Москва, ул. Арбат, 5", "alternatives": "— Клиника «Улыбка»",
    "insurer_terms": "«Доплата 1000 руб. на сотрудника»",
}
# Значения, которые могли бы протащить в письмо обещание, срок или «согласовано» (вставленный текст ответа и т. п.)
NASTY = ["", "   ", "{clinic}", "{insurer_terms}", "гарантируем оплату", "в течение 3 дней", "согласовано",
         "СК оплатит", "клиника уже добавлена", "точно будет добавлена", "бесплатно для вас", "Медси {x} }{"]


def stage_of(tid: str) -> S:
    by_tpl = {t: S(st) for st, ts in load_yaml("add_clinic.yaml")["drafts"].items() for t in ts}
    return by_tpl[tid]


def test_every_configured_draft_has_template():
    tpls = drafts.templates()
    for ids in load_yaml("add_clinic.yaml")["drafts"].values():
        for tid in ids:
            assert tid in tpls, tid


def test_templates_have_required_parts():
    # тема, обращение, юрлицо и договор, программа, клиника, суть (ADR 0004)
    for tid in drafts.templates():
        names = set(drafts.template_fields(tid))
        assert {"clinic", "program", "contract"} <= names, tid
        assert "legal_entity" in names and "care_type" in names, tid
        d = drafts.render(tid, FIELDS, stage=stage_of(tid))
        assert d.subject and d.body.startswith("Добрый день!"), tid


def test_unknown_fields_are_fill_markers():
    for tid in drafts.templates():
        d = drafts.render(tid, {}, stage=stage_of(tid))
        assert set(d.unknown_fields) == set(drafts.template_fields(tid)), tid
        assert drafts.FILL in d.body and d.fields == {}
        assert "{" not in d.subject + d.body, tid
        full = drafts.render(tid, FIELDS, stage=stage_of(tid))
        assert full.unknown_fields == [] and drafts.FILL not in full.subject + full.body, tid


def test_no_promises_in_any_template_with_any_fields():
    # MUST ANSWER 3: каждый шаблон × все варианты «плохих» значений каждого поля — итоговый текст без обещаний
    rnd = random.Random(7)
    for tid in drafts.templates():
        names = drafts.template_fields(tid)
        combos = [dict.fromkeys(names, v) for v in NASTY]
        combos += [{n: rnd.choice(NASTY + list(FIELDS.values())) for n in names} for _ in range(200)]
        combos += [dict(zip(names, vs, strict=False)) for vs in itertools.permutations(NASTY[:len(names)])][:50]
        for fields in [{}, FIELDS, *combos]:
            d = drafts.render(tid, fields, stage=stage_of(tid))
            assert find_promises(d.subject + "\n" + d.body, tid) == [], (tid, fields)
            assert d.auto_send is False


def test_promise_in_field_value_becomes_fill():
    d = drafts.render("hr_paid_terms", {**FIELDS, "insurer_terms": "«Гарантируем добавление в течение 3 дней»"},
                      stage=S.insurer_paid)
    assert "insurer_terms" in d.unknown_fields and "Гарантируем" not in d.body


def test_promise_fields_lists_phrases_and_templates_have_no_new_forbidden_words():
    flagged = drafts.promise_fields("hr_paid_terms", {**FIELDS, "insurer_terms": "добавим оперативно, до 01.11"})
    assert flagged == {"insurer_terms": ["оперативно", "до 01.11"]}
    assert drafts.promise_fields("hr_paid_terms", FIELDS) == {}
    for tid, tpl in drafts.templates().items():
        # сам текст шаблона (без подстановки) — без сроков, «оперативно», «будет добавлен…», гарантий
        assert find_promises(str(tpl.get("subject", "")) + "\n" + str(tpl.get("body", "")), tid) == [], tid


def test_added_phrase_only_in_allowed_template():
    # «… добавлена» — только в письме HR после подтверждения СК (hr_clinic_added, этапы insurer_free / hr_agreed);
    # ни одно другое письмо не говорит, что клиника добавлена
    for tid in drafts.templates():
        d = drafts.render(tid, FIELDS, stage=stage_of(tid))
        added = re.search(r"\bдобавлена\b", d.subject + d.body, re.IGNORECASE) is not None
        assert (tid == "hr_clinic_added") == added, tid
    assert find_promises("клиника уже добавлена", "hr_clinic_added") == []
    assert find_promises("клиника уже добавлена", "hr_no_clinic") != []


def test_braces_in_values_not_expanded():
    d = drafts.render("hr_no_clinic", {**FIELDS, "clinic": "Клиника {program}"}, stage=S.not_in_program)
    assert "Клиника {program}" in d.subject


def test_broken_template_raises():
    from dms_core.scenarios import drafts as mod

    orig = mod.templates
    mod.templates = lambda: {"bad": {"to": "hr", "subject": "Тема", "body": "Гарантируем оплату {clinic}"}}  # type: ignore[assignment]
    try:
        mod.render("bad", FIELDS, stage=S.not_in_program)
    except ValueError as e:
        assert "запрещённое обещание" in str(e)
    else:
        raise AssertionError("испорченный шаблон должен давать ValueError")
    finally:
        mod.templates = orig  # type: ignore[assignment]


def test_unknown_template():
    try:
        drafts.render("nope", {}, stage=S.not_in_program)
    except KeyError:
        pass
    else:
        raise AssertionError


def test_draft_fields_from_contract_card_and_slots():
    st = kit()
    ans = Answer(scenario=Scenario.sc02_add_clinic, context=AnswerContext(contract_id="c01", program="Стандарт"),
                 summary="", confidence=Confidence.confirmed,
                 alternatives=pick("c01", "Стандарт", "стоматология", store=st, exclude="Жемчуг"))
    r = Request(id="r", chat_id="c", title="t",
                slots={"clinic": "Жемчуг", "care_type": "стоматология", "relative": "супруг(а)", "date": "2026-03-12"})
    f = drafts.draft_fields(ans, r, st)
    assert f["legal_entity"] == "Компания 1" and f["contract"] == "№ Д-c01" and f["program"] == "«Стандарт»"
    assert f["clinic"] == "Жемчуг" and f["care_type"] == "стоматология"
    assert "Клиника «Улыбка»" in f["alternatives"] and "источник" not in f["alternatives"]
    # ПДн и даты обращения (родственник, дата) в поля письма не попадают
    assert "relative" not in f and "date" not in f and "супруг" not in " ".join(f.values())
    assert drafts.draft_fields(None, Request(id="r", chat_id="c", title="t"), st) == {}


def test_draft_id_is_idempotency_key():
    a = drafts.draft_id("r1", "жемчуг", S.hr_wants_add, "insurer_add_request")
    assert a == drafts.draft_id("r1", "жемчуг", "hr_wants_add", "insurer_add_request")
    assert a != drafts.draft_id("r1", "улыбка", S.hr_wants_add, "insurer_add_request")
    assert a != drafts.draft_id("r2", "жемчуг", S.hr_wants_add, "insurer_add_request")
