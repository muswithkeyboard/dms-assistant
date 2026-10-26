"""engine.py: правки по ревизии волны 2 (метка без доказательств, слот договора и программы в ГК)."""
from __future__ import annotations

from agent_kit import agent_store
from fake_llm import FakeLLM, final

from dms_core import engine
from dms_core.constants import Label, Scenario
from dms_core.scenarios.route import Route
from dms_core.schemas import ChatContext


def _route(**slots):
    return Route(scenario=Scenario.sc01, slots=dict(slots), questions=[])


def test_label_without_live_citations_is_insufficient():
    # Модель вернула «входит, клиника есть», но ни одного утверждения со ссылкой → не «можно», а «недостаточно»
    st = agent_store()
    llm = FakeLLM([final({"contract_id": "c01", "summary": "Можно без ограничений.",
                          "checks": {"care_in_program": "yes", "clinic_in_program": "yes"}})])
    out = engine._one_contract(llm, "c01", "Положено ли МРТ?", _route(service="МРТ"), ChatContext(contract_id="c01"),
                               st)
    assert out["label"] is Label.insufficient and out["rule"] == "no_evidence"


def test_contract_slot_narrows_only_when_unambiguous():
    st = agent_store()
    gk = st.contracts()[0].group_id
    ctx = ChatContext(group_id=gk)
    c = st.contract("c01")
    assert engine.resolve_contract_slot(c.legal_entity, ctx, st) == "c01"
    assert engine.resolve_contract_slot("c01", ctx, st) == "c01"
    assert engine.resolve_contract_slot("компания", ctx, st) is None      # общая лемма — не договор
    assert engine.resolve_contract_slot(c.legal_entity, ChatContext(contract_id="c01"), st) is None


def test_program_slot_goes_only_to_contracts_that_have_it():
    st = agent_store()
    progs1 = {p.name for p in st.contract("c01").programs}
    progs2 = {p.name for p in st.contract("c02").programs}
    only1 = next(iter(progs1 - progs2), None)
    if only1 is not None:
        assert engine.program_for("c01", only1, st) == only1
        assert engine.program_for("c02", only1, st) is None
    assert engine.program_for("c01", "Несуществующая", st) is None


def test_suggest_automation_when_clinic_named():
    # Кнопка «Добавить клинику» (ревизия UI: блокер демо ТЗ 10.7) — в сценариях клиник и при названной клинике;
    # информационный вопрос без клиники автомат не предлагает (FR 09)
    assert engine.suggest_automation(Scenario.sc02_add_clinic, {}) == "add_clinic"
    assert engine.suggest_automation(Scenario.sc02_info, {"clinic": "Медси"}) == "add_clinic"
    assert engine.suggest_automation(Scenario.sc02_info, {}) is None
