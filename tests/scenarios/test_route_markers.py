"""Маршрут по маркерам config/route_markers.yaml (этап w5-recall): вопросы о покрытии услуги — SC 01, косвенные
отказы, сокращения и названия клиник в слотах. Свои формулировки (общая лексика), без сети и без модели."""
from __future__ import annotations

from agent.agent_kit import agent_store
from dms_core.config import load_yaml
from dms_core.constants import Scenario
from dms_core.scenarios.route import (
    _REFUSAL,
    _care_types,
    firm_sc01,
    is_abbreviation,
    rule_scenario,
    rule_slots,
    service_phrase,
    service_with_care,
)
from dms_core.schemas import ChatContext

ONE = ChatContext(group_id="gk9", contract_id="c01")


def test_markers_come_from_config():
    cfg = load_yaml("route_markers.yaml")
    assert cfg["version"] >= 1
    for key in ("add_clinic", "refusal", "compare", "info_first", "pick_first", "coverage", "coverage_soft",
                "pick", "info"):
        assert cfg[key], key


def test_coverage_phrasings_are_sc01_and_firm():
    for q in ["Гирудотерапия входит?", "Жемчужные ванны входят?", "Лечение кариеса по полису?",
              "Оплатят ли роды?", "Покроет ли страховка брекеты?", "Можно ли по ДМС поставить пломбу?",
              "Бесплатно ли УЗИ щитовидки?", "Удаление папиллом по программе?", "Справку оформят по ДМС?",
              "Будет ли оплачено лечение пневмонии?", "Покупку слухового аппарата компенсируют?",
              "Индивидуальный пост в стационаре оплачивается?", "Сколько раз можно к психотерапевту?"]:
        assert rule_scenario(q) == Scenario.sc01, q
        assert firm_sc01(q), q                       # kNN такой сценарий не перебивает
    # «можно ли» без «по ДМС» — SC 01 по правилам, но kNN может перебить (справка о договоре)
    assert rule_scenario("Можно ли сделать МРТ?") == Scenario.sc01 and not firm_sc01("Можно ли сделать МРТ?")


def test_info_and_pick_are_not_coverage():
    cases = {
        "Что входит в программу «Стандарт»?": Scenario.sc02_info,
        "Какие услуги входят в стоматологию?": Scenario.sc02_info,
        "Какая страховая сумма по программе Москва?": Scenario.sc02_info,
        "Можно ли застраховать родственника по ДМС?": Scenario.sc02_info,
        "Можно ли перевести сотрудника со Стандарта на Бизнес?": Scenario.sc02_info,
        "Сколько стоит программа по ДМС для сотрудника?": Scenario.sc02_info,
        "Какие клиники есть по полису в Самаре?": Scenario.sc02_clinic,
        "Входят ли клиники X и Y в программу? Чем программы отличаются?": Scenario.sc02_info,   # сравнение
    }
    for q, sc in cases.items():
        assert rule_scenario(q) == sc, q
    # первый вопрос обращения решает: «Входит ли клиника …? Если нет — какие клиники доступны?»
    assert rule_scenario("Входит ли Медцентр Альфа в программу? Если нет — какие клиники доступны?") == \
        Scenario.sc01


def test_indirect_refusals():
    for q in ["Сотрудника завернули в регистратуре", "Клиника требует деньги за приём", "В клинике требуют 15% за приём",
              "Бесплатно не оформили справку", "На КПП клиники не пропустили по полису", "Пациента развернули",
              "СК не подтвердила гарантийное письмо", "Медцентр взял 2000 рублей за анализы"]:
        assert _REFUSAL.search(q), q
        assert rule_scenario(q) == Scenario.sc01, q
    for q in ["Бесплатно ли УЗИ?", "Сколько стоит программа?", "Когда срок оплаты взноса?"]:
        assert not _REFUSAL.search(q), q


def test_abbreviations_kept_in_service_slot():
    assert is_abbreviation("ПСА") and is_abbreviation("ЭхоКГ") and is_abbreviation("ПЭТ-КТ")
    assert not is_abbreviation("Медси") and not is_abbreviation("Мед")
    assert service_phrase("Можно ли сделать ЭКГ по ДМС?", exclude=[]) == "ЭКГ"
    assert "ВПЧ" in rule_slots("Анализ на ВПЧ входит?", ONE, agent_store())["service"]
    assert rule_slots("ПЭТ-КТ по ДМС сделают?", ONE, agent_store())["service"] == "ПЭТ-КТ"


def test_proper_name_is_not_service():
    # название клиники / прибора с заглавной внутри фразы — не услуга
    s = service_phrase("Промывание миндалин на Тонзиллоре входит?", exclude=[])
    assert s and "Тонзиллоре" not in s and "миндалин" in s
    s = rule_slots("Можно ли записаться к терапевту в Медси на Белорусской?", ONE, agent_store())
    assert "Медси" not in s.get("service", "")


def test_clinic_name_from_kit_list():
    # c02 синтетики: «Медси на Белорусской» в перечне — название без кавычек и без слова «клиника» — слот clinic
    two = ChatContext(group_id="gk9", contract_id="c02")
    s = rule_slots("Сотруднику в МЕДСИ отказали в приёме терапевта", two, agent_store())
    assert s.get("clinic") == "МЕДСИ"
    # сокращение услуги клиникой не становится, даже если оно есть в названии клиники
    assert "clinic" not in rule_slots("Можно ли сделать МРТ?", two, agent_store())


def test_service_keeps_head_noun_with_care_synonym():
    q = "Нужна имплантация зубов после удаления — входит в программу?"
    care, phrase = _care_types(q)
    assert care == "Стоматологическая помощь"
    assert service_with_care(q, phrase or "", exclude=[]) == "имплантация зубов после удаления"
    q = "Индивидуальный медицинский пост в стационаре оплачивается?"
    _care, phrase = _care_types(q)
    assert service_with_care(q, phrase or "", exclude=[]) == "пост в стационаре"
    # обстоятельства («отказали в …», «в 20 км за МКАД») в услугу не входят
    q = "Отказали в вызове врача на дом — живёт в 20 км за МКАД"
    _care, phrase = _care_types(q)
    assert service_with_care(q, phrase or "", exclude=[]) == "вызове врача на дом"


def test_care_synonym_glued_only_at_word_boundary():
    # слитное написание («AirFlow» = «AIR FLOW») — да; синоним внутри другого слова — нет
    assert _care_types("Положена ли чистка AirFlow?")[0] == "Стоматологическая помощь"
    assert _care_types("Положен ли самомассаж?")[0] is None
