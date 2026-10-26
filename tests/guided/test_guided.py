"""Режим guided (ADR 0009) без сети на синтетике: метки SC 01, absent ≠ not_found, альтернативы своей программы,
взнос по дате, ГК без смешивания договоров, инъекция в тексте пункта, модель только на слоты/вывод."""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

from agent.agent_kit import agent_store
from agent.fake_llm import FakeLLM, final
from dms_core.adapters.base import text_hash
from dms_core.citations import verify
from dms_core.constants import Label, Scenario
from dms_core.guided import (
    engine_mode,
    fill_slots,
    fragment,
    generated,
    key_stems,
    phrase_hit,
    programs_in,
    run_guided,
)
from dms_core.schemas import ChatContext, Clause
from dms_core.store import KitStore
from fixtures.synth_kit import make_group_docs, make_parsed
from helpers import label_as_engine, skip_unless_rule, with_monkeypatch

ROOT = Path(__file__).resolve().parents[2]

DENTAL = {"care_type": "Стоматологическая помощь", "service": "стоматология"}


def _sc01(cid: str, slots: dict, store=None, program: str = "Стандарт", question: str = "Положена ли услуга?",
          pick: str | None = None):
    """guided → verify → метка как в движке; pick — выбор клиники менеджером (ADR 0010, slots["clinic_pick"])."""
    st = store or agent_store()
    ctx = ChatContext(group_id="gk9", contract_id=cid, program=program)
    slots = {**slots, **({"clinic_pick": pick} if pick else {})}
    run = run_guided(cid, question, scenario=Scenario.sc01, slots=dict(slots), context=ctx, store=st)
    v = verify(run.draft, run.refs, st, cid, program=program)
    label, note, rule = label_as_engine(v, pick)
    return run, v, label, note, rule


# ---------------------------------------------------------------- SC 01: все четыре метки

def test_allowed_needs_exclusions_seen():
    # c02: стоматология в матрице есть, клиника «Медси на Белорусской» в перечне, франшиза «—» (не применяется),
    # но исключений программы в договоре нет — «можно» без просмотра исключений не ставится (w5-recall2,
    # guided.allowed_blocker); «можно» при просмотренных исключениях — tests/guided/test_recall2.py
    run, v, label, _, _rule = _sc01("c02", {**DENTAL, "clinic": "Медси на Белорусской"})
    assert v.draft.checks.clinic_in_program == "yes" and v.draft.checks.conditions == []
    assert label is Label.insufficient and v.draft.checks.care_in_program == "not_found"
    assert any("исключения программы" in m for m in v.draft.missing)
    assert run.steps == 0 and run.error is None and not v.dropped


def test_conditional_when_clinic_row_has_franchise():
    _run, v, label, _, rule = _sc01("c01", {**DENTAL, "clinic": "Улыбка"})
    assert v.draft.checks.conditions == ["franchise"] and label is Label.conditional and rule == "conditions"
    fr = [s for s in v.draft.statements if "conditions.franchise" in s.supports]
    assert fr and "10 %" in fr[0].text and fr[0].citations[0].ref.startswith("c01.clinics#")


def test_denied_care_absent_only_from_matrix_row_present_false():
    # c02: стационар в матрице программы — present=False → absent → «нельзя»
    _run, v, label, _, rule = _sc01("c02", {"care_type": "Стационарная помощь", "service": "госпитализация"})
    assert v.draft.checks.care_in_program == "absent" and label is Label.denied and rule == "care_absent"
    backer = [s for s in v.draft.statements if "care_in_program" in s.supports]
    assert backer and backer[0].citations[0].ref == "program#p5"
    # c01: тот же вид помощи present=True → yes, не «нельзя»
    _, v1, label1, _, _ = _sc01("c01", {"care_type": "Стационарная помощь", "service": "госпитализация"})
    assert v1.draft.checks.care_in_program == "yes" and label1 is not Label.denied


def test_denied_clinic_absent_with_alternative_of_same_program():
    skip_unless_rule("clinic_absent")
    # «нет в перечне»: отличительные слова названия не похожи ни на одну строку перечня программы; по тексту —
    # absent, но «нельзя» только после выбора менеджера «нет в перечне программы» (ADR 0010)
    slots = {**DENTAL, "clinic": "Несуществующая Альфа", "clinic_exact": "1"}
    _r0, v0, label0, _n0, _ = _sc01("c01", slots)
    assert v0.draft.checks.clinic_in_program == "absent" and label0 is not Label.denied
    assert v0.draft.clinic_choice is not None and v0.draft.clinic_choice.selected is None
    run, v, label, note, rule = _sc01("c01", slots, pick="none")
    assert v.draft.checks.clinic_in_program == "absent" and label is Label.denied and rule == "clinic_absent"
    assert note == "есть альтернатива"
    # альтернатива — только строка ЭТОЙ программы («Жемчуг» — программа «Премиум» того же договора)
    alts = {a.row_id for a in v.draft.alternatives}
    assert alts == {"c01.clinics#t0r1"}
    assert all(not r.startswith("c02.") for t in run.tools for r in t.refs)


def test_clinic_from_free_text_compared_by_lemmas():
    skip_unless_rule("clinic_absent")
    # w5-recall2: название из текста в косвенном падеже сравнивается по леммам — «в Улыбке» = «Клиника «Улыбка»»;
    # уверенно извлечённого названия нет ни в одной строке перечня — «нет в перечне» и без кавычек; частичное
    # сходство отличительного слова («Улыбочка») — сомнение: «не нашли», а не «нельзя»
    _, v, label, _, _ = _sc01("c01", {**DENTAL, "clinic": "Улыбке"})
    assert v.draft.checks.clinic_in_program == "yes"
    _, v, label, _, _ = _sc01("c01", {**DENTAL, "clinic": "Несуществующей Альфе"})
    assert v.draft.checks.clinic_in_program == "absent" and label is not Label.denied   # без выбора — не «нельзя»
    _, v, label, _, _ = _sc01("c01", {**DENTAL, "clinic": "Несуществующей Альфе"}, pick="none")
    assert v.draft.checks.clinic_in_program == "absent" and label is Label.denied
    _, v, label, _, _ = _sc01("c01", {**DENTAL, "clinic": "Улыбочке"})
    assert v.draft.checks.clinic_in_program == "not_found" and label is not Label.denied


def test_not_found_is_not_denied():
    # телемедицины нет в матрице «Стандарт» ни со значением true, ни false → not_found → «недостаточно данных»
    _, v, label, _, _ = _sc01("c01", {"care_type": "Телемедицина", "service": "онлайн-консультация"})
    assert v.draft.checks.care_in_program == "not_found" and label is Label.insufficient
    # перечня клиник у договора нет вовсе — «клиники нет в перечне» сказать нельзя: not_found, а не «нельзя»
    p = make_parsed("c02")
    p.clinics = []
    st = KitStore(":memory:")
    st.save(p)
    _, v2, label2, _, _ = _sc01("c02", {**DENTAL, "clinic": "Несуществующая Альфа"}, store=st)
    assert v2.draft.checks.clinic_in_program == "not_found" and label2 is not Label.denied


def test_program_unknown_with_several_programs_asks():
    st = agent_store()
    c = st.contract("c01")
    st_multi = c.model_copy(update={"programs": [*c.programs, c.programs[0].model_copy(update={"name": "Премиум"})]})
    p = make_parsed("c01")
    p.contract = st_multi
    st2 = KitStore(":memory:")
    st2.save(p)
    run = run_guided("c01", "Положена ли стоматология?", scenario=Scenario.sc01, slots=dict(DENTAL),
                     context=ChatContext(contract_id="c01"), store=st2)
    assert run.draft.questions and run.draft.checks.care_in_program is None and not run.draft.statements


def test_no_service_no_verdict():
    run, v, label, _, _ = _sc01("c01", {"clinic": "Улыбка"})
    assert v.draft.checks.care_in_program is None and label is not Label.allowed
    assert any("услуга" in m for m in run.draft.missing)


# ---------------------------------------------------------------- SC 02

def _store_with_schedule() -> KitStore:
    p = make_parsed("c01")
    row = "2 | 15.07.2026 | 01.07.2026–31.12.2026 | 250 000,00"
    p.clauses.append(Clause(id="c01.contract#t6r2", doc_id="c01.contract", contract_id="c01", number=None,
                            path="Договор › 2.6", text=row, text_hash=text_hash(row)))
    st = KitStore(":memory:")
    st.save(p)
    st.save(make_parsed("c02"))
    return st


def test_payment_by_context_date():
    st = _store_with_schedule()
    ctx = ChatContext(contract_id="c01", date=dt.date(2026, 3, 1))
    run = run_guided("c01", "Когда следующий взнос и сколько платить?", scenario=Scenario.sc02_info, slots={},
                     context=ctx, store=st)
    rec = next(t for t in run.tools if t.name == "payments_next")
    assert rec.args == {"date": "2026-03-01"} and rec.result["next"]["n"] == 2
    v = verify(run.draft, run.refs, st, "c01")
    pay = [s for s in v.draft.statements if "Ближайший взнос" in s.text]
    assert pay and "15.07.2026" in pay[0].text and "250 000,00" in pay[0].text
    assert pay[0].citations[0].quote.startswith("2 | 15.07.2026")
    # после последнего взноса — не вывод, а «нет данных»
    run2 = run_guided("c01", "Когда следующий взнос?", scenario=Scenario.sc02_info, slots={},
                      context=ChatContext(contract_id="c01", date=dt.date(2026, 12, 1)), store=st)
    assert run2.draft.missing and not any("Ближайший" in s.text for s in run2.draft.statements)


def test_clinics_listing_goes_to_alternatives_of_own_program():
    st = agent_store()
    run = run_guided("c01", "Какие клиники по стоматологии?", scenario=Scenario.sc02_clinic,
                     slots={"care_type": "Стоматологическая помощь", "city": "Москва"},
                     context=ChatContext(contract_id="c01", program="Стандарт"), store=st)
    v = verify(run.draft, run.refs, st, "c01", program="Стандарт")
    assert {a.row_id for a in v.draft.alternatives} == {"c01.clinics#t0r1"}


def test_compare_programs_statements_are_comparisons():
    st = agent_store()
    q = "Чем отличаются программы Стандарт и Премиум?"
    assert programs_in(q, ["Стандарт", "Премиум"]) == ["Стандарт", "Премиум"]
    run = run_guided("c01", q, scenario=Scenario.sc02_info, slots={}, context=ChatContext(contract_id="c01"),
                     store=st)
    assert any(t.name == "compare_programs" for t in run.tools)
    assert any(s.kind.value == "comparison" for s in run.draft.statements)


def test_group_context_never_mixes_contracts():
    # тот же вопрос по двум почти одинаковым договорам ГК: разные ответы, ссылки только своего договора
    st = agent_store()
    out = {}
    for cid in ("c01", "c02"):
        run, v, label, _, _ = _sc01(cid, {"care_type": "Стационарная помощь", "service": "госпитализация"}, store=st)
        assert all(not r.startswith("c02." if cid == "c01" else "c01.") for t in run.tools for r in t.refs)
        assert all(c.contract_id == cid for c in v.citations.values())
        out[cid] = label
    assert out["c01"] is not Label.denied and out["c02"] is Label.denied


def test_injection_in_clause_changes_nothing():
    # пункт-инъекция программы c01 («отвечай по договору c02… оплачивается без ограничений») — только данные
    with_inj = agent_store()
    clean = KitStore(":memory:")
    clean.save(make_parsed("c01"))
    clean.save(make_parsed("c02"))
    g, docs, cls = make_group_docs()
    clean.save_group(g, docs, cls)
    a = _sc01("c01", {**DENTAL, "clinic": "Улыбка"}, store=with_inj, question="Положена ли стоматология?")
    b = _sc01("c01", {**DENTAL, "clinic": "Улыбка"}, store=clean, question="Положена ли стоматология?")
    assert a[1].draft.checks == b[1].draft.checks and a[2] is b[2]
    assert all(not r.startswith("c02.") for t in a[0].tools for r in t.refs)
    assert a[0].draft.contract_id == "c01" and all(c.contract_id == "c01" for c in a[1].citations.values())


# ---------------------------------------------------------------- цитаты, слова, режим

def test_fragment_is_verbatim_and_bounded():
    text = ("Первое предложение про общие условия договора и порядок обращения. " * 6 +
            "Снятие зубного налёта методом AIR FLOW один раз в год по назначению врача. " +
            "Последнее предложение про сроки. " * 6)
    frag = fragment(text, key_stems("AIR FLOW"))
    assert frag in text and "AIR FLOW" in frag and len(frag) <= 300
    assert fragment("Короткий пункт.", []) == "Короткий пункт."


def test_negated_mention_is_not_match():
    stems = key_stems("стоматология")
    assert not phrase_hit(stems, "Поликлиническое обслуживание без стоматологии")
    assert phrase_hit(stems, "Стоматологическое обслуживание")
    assert phrase_hit(stems, "без стоматологии", negation_aware=False)


def test_every_statement_has_own_fragment():
    # claim-level grounding: у каждого утверждения — своя ссылка; дословный фрагмент = текст утверждения
    run, v, *_ = _sc01("c01", {**DENTAL, "clinic": "Улыбка"}, question="Положена ли чистка AIR FLOW?")
    assert v.draft.statements and all(s.citations for s in v.draft.statements)
    for s in run.draft.statements:
        if not generated(s):
            assert len(s.citations) == 1 and s.citations[0].quote in s.text


@with_monkeypatch
def test_engine_mode(monkeypatch):
    monkeypatch.setenv("DMS_ENGINE_MODE", "auto")
    monkeypatch.delenv("DMS_LLM_SMALL", raising=False)
    assert engine_mode(None) == "guided" and engine_mode(object()) == "agent"
    monkeypatch.setenv("DMS_LLM_SMALL", "1")
    assert engine_mode(object()) == "guided"
    monkeypatch.setenv("DMS_ENGINE_MODE", "agent")
    assert engine_mode(None) == "extractive"
    monkeypatch.setenv("DMS_ENGINE_MODE", "guided")
    assert engine_mode(object()) == "guided"


# ---------------------------------------------------------------- модель: только флагом (ADR 0009, замер)

@with_monkeypatch
def test_model_not_called_without_flags(monkeypatch):
    # по умолчанию guided модель не вызывает ни на слоты, ни на вывод — даже если она передана
    monkeypatch.delenv("DMS_GUIDED_LLM_SLOTS", raising=False)
    monkeypatch.delenv("DMS_GUIDED_LLM_SUMMARY", raising=False)
    monkeypatch.setattr("dms_core.guided._flag", lambda name: False)
    llm = FakeLLM([])
    run = run_guided("c01", "Положена ли стоматология?", scenario=Scenario.sc01, slots={"clinic": "Улыбка"},
                     context=ChatContext(contract_id="c01", program="Стандарт"), store=agent_store(), llm=llm)
    assert not llm.calls and run.steps == 0
    assert fill_slots("Отказали в МРТ", Scenario.sc01, {}, ChatContext(contract_id="c01"), llm) == {}


def test_classifier_extension_point():
    from dms_core.guided import choose_scenario

    st = agent_store()
    ctx = ChatContext(contract_id="c01", program="Стандарт")
    r = choose_scenario("Какие клиники по стоматологии в Москве?", ctx, store=st,
                        classifier=lambda q: Scenario.sc02_info)
    assert r.scenario is Scenario.sc02_info and r.source == "classifier"
    # явный отказ (правила → sc01) классификатор не перебивает; сбой классификатора — остаются правила
    r2 = choose_scenario("Клиника отказала в приёме стоматолога", ctx, store=st,
                         classifier=lambda q: Scenario.sc02_info)
    assert r2.scenario is Scenario.sc01

    def boom(q):
        raise RuntimeError("нет модели")

    assert choose_scenario("Какие клиники по стоматологии?", ctx, store=st, classifier=boom).source == "rules"


@with_monkeypatch
def test_fill_slots_one_call_grounded_and_structured(monkeypatch):
    from dms_core.llm import _common

    monkeypatch.setenv("DMS_GUIDED_LLM_SLOTS", "1")
    seen = {}

    def step(messages, tools, schema):
        seen["mode"] = _common.structured_mode()
        seen["tools"] = tools
        seen["schema"] = schema
        seen["user"] = messages[-1].content
        return final({"service": "МРТ колена", "care_type": "диагностика", "clinic": "Клиника Ромашка",
                      "city": None, "program": "Выдуманная"})

    llm = FakeLLM([step])
    q = "Сотруднику отказали в МРТ колена. Это правильно?"
    out = fill_slots(q, Scenario.sc01, {"refusal": "отказали"}, ChatContext(contract_id="c01"), llm,
                     store=agent_store())
    assert len(llm.calls) == 1 and seen["tools"] is None and seen["mode"] == "on"
    assert all(v["type"] == ["string", "null"] for v in seen["schema"]["properties"].values())
    assert out["service"] == "МРТ колена" and out["care_type"] == "Диагностика"
    assert "clinic" not in out and "program" not in out       # нет опоры в тексте вопроса → отброшены
    assert "<question>" in seen["user"] and "Стандарт" in seen["user"]
    assert _common.structured_mode() != "on" or _common.env("DMS_LLM_STRUCTURED") == "on"
    # нужных слотов нет — модель не вызывается
    llm2 = FakeLLM([])
    assert fill_slots(q, Scenario.sc01, {"service": "МРТ", "clinic": "X"}, ChatContext(), llm2) == \
        {"service": "МРТ", "clinic": "X"} and not llm2.calls


@with_monkeypatch
def test_summary_call_gets_no_document_text(monkeypatch):
    monkeypatch.setenv("DMS_GUIDED_LLM_SUMMARY", "1")
    monkeypatch.delenv("DMS_GUIDED_LLM_SLOTS", raising=False)
    st = agent_store()
    llm = FakeLLM([final({"summary": "Стоматология входит в программу «Стандарт»."})])
    run = run_guided("c01", "Положена ли стоматология в «Улыбка»?", scenario=Scenario.sc01,
                     slots={**DENTAL, "clinic": "Улыбка"}, context=ChatContext(contract_id="c01",
                                                                                program="Стандарт"),
                     store=st, llm=llm)
    assert len(llm.calls) == 1 and run.steps == 1 and run.usage.output_tokens == 20
    sent = llm.calls[0].messages[-1].content
    assert "AIR FLOW" not in sent and "Франшиза по программе" not in sent     # фрагменты пунктов не уходят
    assert run.draft.summary.startswith("Стоматология")


@with_monkeypatch
def test_model_failure_falls_back_to_template(monkeypatch):
    monkeypatch.setenv("DMS_GUIDED_LLM_SUMMARY", "1")
    run = run_guided("c01", "Положена ли стоматология?", scenario=Scenario.sc01, slots=dict(DENTAL),
                     context=ChatContext(contract_id="c01", program="Стандарт"), store=agent_store(),
                     llm=FakeLLM([]))
    assert run.error is None and run.draft.checks.care_in_program == "yes" and run.draft.summary


def test_guided_engine_script_end_to_end():
    sys.path.insert(0, str(ROOT))
    from scripts.guided_engine import answer

    st = agent_store()
    trace: dict = {}
    a = answer("Положена ли стоматология в клинике «Улыбка»?", ChatContext(group_id="gk9", contract_id="c01",
                                                                          program="Стандарт"),
               store=st, trace=trace)
    assert a.label is Label.conditional and a.llm.model == "guided" and trace["engine_mode"] == "guided"
    assert trace["peak_rss_mb"] > 0 and json.dumps(trace["llm_tokens"])
    # справочный вопрос в контексте ГК — проход по каждому договору («Входит ли …?» — уже SC 01, w5-recall)
    g = answer("Какие условия госпитализации в программе?", ChatContext(group_id="gk9", program="Стандарт"),
               store=st)
    assert {p.contract_id for p in g.per_contract} == {"c01", "c02"}
    for p in g.per_contract:
        assert all(c.contract_id == p.contract_id for s in p.statements for c in s.citations)


def test_bogus_clinic_slot_never_gives_denied():
    # «клиника» = служебные слова / юрлицо / город — не название: проверка клиники не выполняется, «нельзя» нет
    from dms_core.guided import plausible_name

    for bogus in ("не по ДМС", "Компания 1", "Москва", "Сотрудник"):
        _, v, label, _, _ = _sc01("c01", {**DENTAL, "clinic": bogus, "city": "Москва"})
        assert v.draft.checks.clinic_in_program in (None, "not_found") and label is not Label.denied
    assert plausible_name("Гиппократ 21 век", others=["Москва"]) and not plausible_name("ДМС", others=[])
