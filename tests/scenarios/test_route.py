"""Маршрутизация и уточнения: правила по словам без модели, один вызов модели, откат на правила при сбое."""
from __future__ import annotations

import json

from agent.agent_kit import agent_store
from agent.fake_llm import FakeLLM, final
from dms_core.constants import Scenario
from dms_core.llm.base import LlmTurn, Usage
from dms_core.scenarios.clarify import missing_facts
from dms_core.scenarios.route import route, rule_scenario, rule_slots
from dms_core.schemas import ChatContext

ONE = ChatContext(group_id="gk9", contract_id="c01")       # у c01 в синтетике одна программа «Стандарт»
GROUP = ChatContext(group_id="gk9")


def test_rule_scenarios():
    cases = {
        "Клиника отказала сотруднику в приёме стоматолога": Scenario.sc01,
        "Сотрудника не приняли в поликлинике, говорят нет в списке": Scenario.sc01,
        "Положено ли МРТ по ДМС?": Scenario.sc01,
        "Какие клиники доступны в Самаре по стоматологии?": Scenario.sc02_clinic,
        "Подберите клинику рядом с офисом": Scenario.sc02_clinic,
        "Хотим добавить клинику «Здоровье» в программу": Scenario.sc02_add_clinic,
        "Какой следующий страховой взнос по договору?": Scenario.sc02_info,
        "Входит ли ведение беременности в программу?": Scenario.sc01,      # вопрос о покрытии услуги (w5-recall)
        "Что входит в программу «Стандарт»?": Scenario.sc02_info,
        "Добрый день": Scenario.general,
    }
    for q, sc in cases.items():
        assert rule_scenario(q) == sc, q


def test_rule_slots_extract_only_text_facts():
    st = agent_store()
    s = rule_slots("Клиника «Медси» отказала жене сотрудника в МРТ 12.03.2026 в Москве по программе «Стандарт»",
                   ONE, st)
    assert s["clinic"] == "Медси" and s["program"] == "Стандарт" and s["date"] == "2026-03-12"
    assert s["relative"] == "супруг(а)" and s["city"] == "Москва" and s["refusal"].startswith("отказ")
    assert s["care_type"] == "Диагностика" and s["service"] == "МРТ"
    s = rule_slots("Положена ли чистка AirFlow?", ONE, st)
    assert s["care_type"] == "Стоматологическая помощь"
    assert "clinic" not in s and "program" not in s and "date" not in s
    # программа, которой нет в договоре, слотом program не становится
    assert "program" not in rule_slots("по программе «Золотая»", ONE, st)


def test_missing_facts_by_config():
    st = agent_store()
    # SC 01 без услуги и клиники, отказ не упомянут: вопрос только про услугу (клиника — при отказе,
    # config/required_facts.yaml when: refusal_mentioned); программа у договора одна — не спрашиваем
    qs = missing_facts(Scenario.sc01, {}, ONE, store=st)
    assert len(qs) == 1 and "услуга" in qs[0]
    qs = missing_facts(Scenario.sc01, {"refusal": "да"}, ONE, store=st)
    assert len(qs) == 2 and "клинике" in qs[1]
    # контекст ГК: ещё договор; программа неизвестна, у договоров ГК по одной программе — не спрашиваем
    qs = missing_facts(Scenario.sc01, {"service": "МРТ", "clinic": "Медси"}, GROUP, store=st)
    assert qs == ["Какое юрлицо / договор у застрахованного?"]
    # вид помощи закрывает услугу (и наоборот)
    assert missing_facts(Scenario.sc01, {"care_type": "Стоматологическая помощь", "clinic": "X"}, ONE, store=st) == []
    assert missing_facts(Scenario.sc02_info, {}, ONE, store=st) == []


def test_program_question_when_store_unknown():
    """Хранилище недоступно → считаем, что программа влияет (лучше спросить, чем перенести условия)."""
    qs = missing_facts(Scenario.sc02_clinic, {"care_type": "dental", "city": "Москва"},
                       ChatContext(contract_id="c77"), store=agent_store())
    assert qs == ["Какая программа у застрахованного?"]


def test_route_with_llm_one_call():
    llm = FakeLLM([final({"scenario": "sc01", "slots": {"service": "МРТ", "clinic": "Медси",
                                                         "program": "стандарт", "unknown": "x"}})])
    r = route("В Медси не сделали МРТ по программе стандарт", ONE, llm, store=agent_store())
    assert len(llm.calls) == 1 and llm.calls[0].tools is None and llm.calls[0].json_schema
    assert r.source == "llm" and r.scenario == Scenario.sc01
    assert r.slots["program"] == "Стандарт" and "unknown" not in r.slots and r.questions == []
    assert llm.calls[0].messages[0].content.startswith("<!-- version:")


def test_route_falls_back_to_rules():
    bad = FakeLLM([LlmTurn(text="не знаю", tool_calls=[], usage=Usage(), model="fake")])
    r = route("Клиника отказала в МРТ", ONE, bad, store=agent_store())
    # без модели решают правила или kNN по эмбеддингам (если энкодер есть в окружении) — но не модель
    assert r.source in ("rules", "knn") and r.scenario == Scenario.sc01 and r.warnings
    wrong = FakeLLM([final(json.dumps({"scenario": "sc99", "slots": {}}))])
    assert route("Клиника отказала в МРТ", ONE, wrong, store=agent_store()).source in ("rules", "knn")
    down = FakeLLM([])      # провайдер недоступен → LlmError
    r = route("Клиника отказала в МРТ", ONE, down, store=agent_store())
    assert r.source in ("rules", "knn") and "клинике" in r.questions[0]


def test_route_without_llm():
    r = route("Какие клиники есть в Москве по стоматологии?", ONE, store=agent_store())
    assert r.scenario == Scenario.sc02_clinic and r.slots["city"] == "Москва" and r.questions == []


def test_llm_slots_must_be_grounded_in_question():
    """Слоты, которых нет в тексте (выдуманный отказ, родственник, клиника), код отбрасывает."""
    llm = FakeLLM([final({"scenario": "sc02_info", "slots": {
        "service": "стоматология", "refusal": "отказ клиники", "relative": "супруг", "clinic": "Улыбка",
        "date": "2026-01-01"}})])
    r = route("Положена ли стоматология по ДМС?", ONE, llm, store=agent_store())
    assert set(r.slots) >= {"service", "care_type"}
    assert not {"refusal", "relative", "clinic", "date"} & set(r.slots)
    assert sum("отброшен" in w for w in r.warnings) == 4
    # маркер «положено ли» — sc01 важнее ответа модели
    assert r.scenario == Scenario.sc01 and any("заменён на sc01" in w for w in r.warnings)


def test_nearest_payment_is_not_clinic_pick():
    # «ближайший взнос» — вопрос об условиях договора, а «ближайшая клиника» — подбор клиники (правила без модели)
    from dms_core.scenarios.route import rule_scenario

    assert rule_scenario("Какой ближайший взнос по договору и до какой даты его оплатить?") == Scenario.sc02_info
    assert rule_scenario("Где ближайшая клиника для МРТ?") == Scenario.sc02_clinic


def test_service_phrase_without_dictionary():
    # Услуга без словаря видов помощи — содержательные слова вопроса без общих слов ДМС (свои формулировки)
    from dms_core.scenarios.route import service_phrase

    s = service_phrase("Сотруднику отказали в склеротерапии вен, это обоснованно?", exclude=[])
    assert s and "склеротерапии" in s and "Сотруднику" not in s and "обоснованно" not in s
    assert service_phrase("Клиника отказала, правы ли они?", exclude=[]) is None


def test_clinic_question_does_not_block_answer():
    # «В какой клинике отказали?» — не блокирует вывод (config/required_facts.yaml, blocking: false)
    from dms_core.scenarios.clarify import split_missing

    st = agent_store()
    blocking, advisory = split_missing(Scenario.sc01, {"service": "МРТ", "refusal": "да"}, ONE, store=st)
    assert blocking == [] and any("клинике" in q for q in advisory)
    blocking, _ = split_missing(Scenario.sc01, {"refusal": "да"}, ONE, store=st)
    assert blocking and "услуга" in blocking[0]


# ---------------------------------------------------------------- исправления по ревизии w5-coverage

def test_refusal_or_payment_demand_is_one_class():
    # отказ и требование оплаты — маркер refusal и SC 01 (свои формулировки, общий класс, не перечень вопросов)
    from dms_core.scenarios.route import _REFUSAL

    refusals = ["СК не согласовала КТ", "Пациенту отклонили запись", "Не дали направление на анализы",
                "В клинике не стали делать рентген", "Попросили доплатить за приём", "Сказали, что это платно",
                "Медцентр сделает только за деньги", "Выставили счёт за вызов", "Клиника требует оплатить анализы",
                "Стоматолог взял деньги за пломбу", "Регистратура говорит, что это не по полису",
                "Отказ в оплате лечения", "Сказали, что приём нужно оплатить самому"]
    for q in refusals:
        assert _REFUSAL.search(q), q
        assert rule_scenario(q) == Scenario.sc01, q
        assert rule_slots(q, ONE, agent_store()).get("refusal"), q
    for q in ["Нужно ли оплатить франшизу?", "Когда срок оплаты взноса?", "Положено ли бесплатное УЗИ?",
              "Сколько нужно заплатить за прием?"]:
        assert not _REFUSAL.search(q), q


def test_service_slot_keeps_qualifier_and_care_type_separately():
    st = agent_store()
    s = rule_slots("Положены ли анализы на ВИЧ?", ONE, st)
    assert s["care_type"] == "Диагностика" and "ВИЧ" in s["service"] and "анализы" in s["service"]
    s = rule_slots("Положено ли УЗИ при беременности?", ONE, st)
    assert s["service"] == "УЗИ при беременности"
    assert rule_slots("Положены ли генетические анализы?", ONE, st)["service"] == "генетические анализы"
    # обстоятельства («в 20 км за МКАД», дата, клиника) уточнением услуги не становятся
    s = rule_slots("Отказали в вызове врача на дом — живёт в 20 км за МКАД", ONE, st)
    assert s["service"] == "вызове врача на дом"
    # синоним вида помощи — по границе слова: «КТ» не находится в «катаракты»
    assert "care_type" not in rule_slots("Положено ли лечение катаракты?", ONE, st)


def test_knn_does_not_override_refusal():
    import dms_core.scenarios.route as R

    orig = R.embedding_scenario
    R.embedding_scenario = lambda q: Scenario.sc02_info
    try:
        assert route("Клиника не стала делать УЗИ, говорит не по программе", ONE, store=agent_store()).scenario \
            == Scenario.sc01
        r = route("Можно ли перевести сотрудника на другую программу?", ONE, store=agent_store())
        assert r.scenario == Scenario.sc02_info and r.source == "knn"      # без отказа kNN решает, как раньше
    finally:
        R.embedding_scenario = orig


def test_clinic_name_with_conjunction_and_verb_service():
    """Ревизия w18: «Мать и дитя» — одна клиника, не «Мать» + родственник; «Медси и Будь Здоров» — не одна;
    «вырвать зуб» → услуга «удаление зуба»."""
    st = agent_store()
    s = rule_slots("Можно ли полечиться в клинике Мать и дитя?", ONE, st)
    assert s.get("clinic") == "Мать и дитя" and "relative" not in s
    assert rule_slots("В Медси и в Будь Здоров отказали в МРТ", ONE, st).get("clinic") == "Медси"
    from dms_core.scenarios.route import procedure_service
    assert procedure_service("можно ли вырвать зуб", "зуб", exclude=[]) == "удаление зуба"
    assert procedure_service("Удалить папиллому на шее — по ДМС?", "папиллому шее", exclude=[]) == "папиллому шее"
