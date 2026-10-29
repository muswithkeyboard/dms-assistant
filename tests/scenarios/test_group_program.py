"""Чат ГК с выбранной программой (замечание пользователя 29.10.2026): ответ — только по договорам группы, где эта
программа есть; программы нет ни в одном договоре — по всем договорам, как раньше."""
from __future__ import annotations

from dms_core.engine import contracts_of
from dms_core.schemas import ChatContext, Program
from dms_core.store import KitStore
from fixtures.synth_kit import make_parsed


def _store() -> KitStore:
    st = KitStore(":memory:")
    a, b = make_parsed("c01"), make_parsed("c02")
    b.contract.programs = [Program(name="Москва Премиум", premium_per_person=1.0, persons=1)]
    st.save(a)
    st.save(b)
    return st


def test_group_program_narrows_contracts() -> None:
    st = _store()
    assert contracts_of(ChatContext(group_id="gk9", program="Стандарт"), st) == ["c01"]
    assert contracts_of(ChatContext(group_id="gk9", program="  стандарт "), st) == ["c01"]       # регистр, пробелы
    assert contracts_of(ChatContext(group_id="gk9", program="Москва Премиум"), st) == ["c02"]
    assert contracts_of(ChatContext(group_id="gk9", program="Нет такой"), st) == ["c01", "c02"]  # не найдена — все
    assert contracts_of(ChatContext(group_id="gk9"), st) == ["c01", "c02"]
    assert contracts_of(ChatContext(contract_id="c02", program="Стандарт"), st) == ["c02"]       # договор — как был


def test_group_program_single_contract_does_not_ask_contract() -> None:
    from dms_core.constants import Scenario
    from dms_core.scenarios.clarify import missing_facts

    st = _store()
    ask = "Какое юрлицо / договор у застрахованного?"
    assert ask in missing_facts(Scenario.sc01, {}, ChatContext(group_id="gk9"), store=st)
    assert ask not in missing_facts(Scenario.sc01, {}, ChatContext(group_id="gk9", program="Москва Премиум"), store=st)
    assert ask in missing_facts(Scenario.sc01, {}, ChatContext(group_id="gk9", program="Нет такой"), store=st)


def test_program_not_in_contract_is_named_in_question() -> None:
    from dms_core.engine import answer

    st = KitStore(":memory:")
    b = make_parsed("c02")
    b.contract.programs = [Program(name="Москва", premium_per_person=1.0, persons=1),
                           Program(name="Регион", premium_per_person=1.0, persons=1)]
    st.save(b)
    a = answer("Входит ли стоматология?", ChatContext(contract_id="c02", program="ВИП"), llm=None, store=st)
    assert any("«ВИП»" in q and "нет" in q for q in a.questions), a.questions
