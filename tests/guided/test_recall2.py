"""Полнота SC 01 без ложных «можно» / «нельзя», второй круг (этап w5-recall2): синтетика на каждое правило, без сети.

1. Клиника по леммам: извлечение названия из косвенного падежа без города, сравнение по леммам и отличительным
   словам, без подстановки чужой клиники; «нет в перечне» — только при уверенном названии и без частичного сходства.
2. Лимит: число сравнивается при совпадении услуги, единицы и периода; иначе — условие.
3. Специалист и диагностика → амбулаторно-поликлиническая помощь (config/care_types.yaml route_words, part_of);
   «можно» — при клинике в перечне, услуге в тексте «входит» и просмотренных исключениях.
4. Помощь за рубежом: страны в признаках вопроса; подпункты раздела «вне территории РФ» — только к загранице.
5. Противоречие — только при совпадении услуги целиком; «кроме X» во «входит» и исключение «X» согласованы.
6. Маркеры отказа — естественные формулировки (config/route_markers.yaml).
7. Исключение раздела другого вида помощи («АПТЕКА») — не основание «нельзя» для вопроса о лечении.
Плюс регрессия на синтетике по 14 прежним ложным «нельзя» ревизии 3 (названия и тексты — выдуманные).
"""
from __future__ import annotations

from dms_core.adapters.base import text_hash
from dms_core.citations import verify
from dms_core.constants import Label, Scenario
from dms_core.guided import (
    _Run,
    _Sections,
    asked_count,
    bound_executor,
    care_compatible,
    check_clinic,
    check_coverage,
    coverage_hits,
    limits_in,
    question_abroad,
    run_guided,
    section_care,
    service_terms,
    strict_hit,
)
from dms_core.scenarios.route import _care_types, clinic_phrase, rule_scenario, rule_slots
from dms_core.schemas import CareMatrix, ChatContext, Clause, ClinicRow
from dms_core.store import KitStore
from dms_core.tools.clinics import _care_phrase_sets, distinct_overlap, name_score
from fixtures.synth_kit import make_parsed
from helpers import label_as_engine, skip_unless_limit_deny, skip_unless_rule

PROG = "Программа «Стандарт»"
CTX = ChatContext(group_id="gk9", contract_id="c01", program="Стандарт")

# Выдуманный перечень клиник программы «Стандарт» (и одна клиника программы «Премиум»)
CLINICS = [
    ("Амбулаторно-поликлиническая помощь", "АО «Ромашка здоровья»", "Томск"),
    ("Амбулаторно-поликлиническая помощь", "«Семейный доктор» АО", "Москва"),
    ("Амбулаторно-поликлиническая помощь", "«Верба» ООО", "Москва"),
    ("Амбулаторно-поликлиническая помощь", "ООО «Австрийский медицинский центр»", "Воронеж"),
    ("Амбулаторно-поликлиническая помощь", "МЦ «Пилюлькин»", "Казань"),
    ("Амбулаторно-поликлиническая помощь", "ЧУЗ «КБ «ТрансМедицина» г. Самара»", "Самара"),
    ("Стоматологическая помощь", "ОГАУЗ «Северская ОКБ»", "Северск"),
    ("Стоматологическая помощь", "ООО «ДЕНТАЛСТАР» (СК «ДОБРЫЙ ЛЕКАРЬ»)", "Томск"),
    ("Стационарная помощь", "ОГАУЗ «Больница скорой медицинской помощи»", "Томск"),
    ("Амбулаторно-поликлиническая помощь", "«Медико-оздоровительный центр Северный» ООО", "Москва"),
    ("Амбулаторно-поликлиническая помощь", "«Мой доктор» ООО", "Москва"),
    ("Амбулаторно-поликлиническая помощь", "АО «ГК «Медикс»", "Москва"),
]
OTHER_PROGRAM = [("Амбулаторно-поликлиническая помощь", "«Никита и Ко» ООО (ЧУДЕСНЫЙ ДОКТОР)", "Москва")]

INCLUDED = [
    (None, f"{PROG} › Программа включает", "Программа включает:"),
    ("1.1", f"{PROG} › Программа включает › 1.1",
     "1.1. Приёмы врачей-специалистов: невролога, терапевта, эндокринолога, кардиолога в поликлинике."),
    ("1.2", f"{PROG} › Программа включает › 1.2",
     "1.2. Ультразвуковые исследования органов брюшной полости и щитовидной железы, электрокардиография."),
]
EXCLUSIONS = [
    (None, f"{PROG} › Исключения из программы", "Исключения из программы:"),
    ("4.1", f"{PROG} › Исключения из программы › 4.1",
     "4.1. Пластические операции с эстетической целью, косметология, гомеопатия."),
]


def _cl(n: int, number: str | None, path: str, text: str, doc: str = "program") -> Clause:
    return Clause(id=f"c01.{doc}#r{n}", doc_id=f"c01.{doc}", contract_id="c01", number=number, path=path,
                  program="Стандарт", text=text, text_hash=text_hash(text))


def _kit(rows: list[tuple[str | None, str, str]] | None = None, *, clinics=CLINICS, care=None) -> KitStore:
    p = make_parsed("c01")
    for i, (number, path, text) in enumerate(rows or []):
        p.clauses.append(_cl(i, number, path, text))
    p.clinics = [
        ClinicRow(id=f"c01.clinics#s{i}", contract_id="c01", doc_id="c01.clinics", program=prog, care_type=ct,
                  clinic_name=name, city=city, franchise_pct=None, source_ref=f"s{i}")
        for i, (prog, (ct, name, city)) in enumerate([*(("Стандарт", c) for c in clinics),
                                                     *(("Премиум", c) for c in OTHER_PROGRAM)])
    ]
    p.care = care if care is not None else [
        CareMatrix(contract_id="c01", program="Стандарт", care_type="outpatient", present=True, source_refs=["m1"]),
        CareMatrix(contract_id="c01", program="Стандарт", care_type="dental", present=True, source_refs=["m2"]),
    ]
    st = KitStore(":memory:")
    st.save(p)
    st.save(make_parsed("c02"))
    return st


def _sc01(st: KitStore, question: str, slots: dict | None = None, *, pick: str | None = None):
    """Вопрос → слоты правилами (как движок без модели) → guided → verify → метка как в движке; pick — выбор
    клиники менеджером (ADR 0010)."""
    s = slots if slots is not None else rule_slots(question, CTX, st)
    s = {**s, "program": "Стандарт"}
    run = run_guided("c01", question, scenario=Scenario.sc01, slots={**s, **({"clinic_pick": pick} if pick else {})},
                     context=CTX, store=st)
    v = verify(run.draft, run.refs, st, "c01", program="Стандарт", tools=run.tools)
    label, _note, _rule = label_as_engine(v, pick, refusal=bool(s.get("refusal")))
    return s, v, label


def _row(name: str) -> ClinicRow:
    return ClinicRow(id="x", contract_id="c01", doc_id="d", program="Стандарт", care_type="c", clinic_name=name,
                     source_ref="x")


def _run(st: KitStore) -> _Run:
    return _Run(cid="c01", st=st, execute=bound_executor("c01", st))


# ---------------------------------------------------------------- 1. клиника по леммам


def test_name_score_by_lemmas_and_distinct_words():
    assert name_score("Ромашке здоровья", _row("АО «Ромашка здоровья»")) >= 88          # косвенный падеж
    assert name_score("Семейном докторе", _row("«Семейный доктор» АО")) >= 88           # одни общие слова
    assert name_score("Северской областной клинической больнице", _row("ОГАУЗ «Северская ОКБ»")) >= 88
    assert name_score("БСМП", _row("ОГАУЗ «Больница скорой медицинской помощи»")) >= 88   # сокращение
    assert name_score("КДЦ Северный", _row("«Медико-оздоровительный центр Северный» ООО")) >= 88
    # подстановки чужой клиники нет: все отличительные слова запроса должны найтись
    assert name_score("Чудесный Доктор", _row("«Мой доктор» ООО")) == 0
    assert name_score("Мед.Ком", _row("АО «ГК «Медикс»")) == 0
    # частичное сходство — сомнение (для «нет в перечне»), короткое слово — только точно
    assert distinct_overlap("Медиксе", _row("АО «ГК «Медикс»"))
    assert not distinct_overlap("Медси", _row("ООО «Клиника МЕД+»"))


def test_clinic_phrase_without_city_and_service():
    st = _kit()
    assert clinic_phrase("Прием невролога в Ромашке здоровья, по ДМС пойдет?", CTX, st)[0] == "Ромашке здоровья"
    name, city = clinic_phrase("в ТрансМедицине Самары взяли деньги за УЗИ", CTX, st)
    assert name == "ТрансМедицине" and city == "Самара"                               # город — отдельно
    assert clinic_phrase("Прием терапевта в Вербе на Профсоюзной — по полису?", CTX, st)[0] == "Вербе"
    assert clinic_phrase("Лечение кариеса в Москве — по полису?", CTX, st)[0] is None   # город перечня
    assert clinic_phrase("Застрахованный в Турции, острая зубная боль", CTX, st)[0] is None   # страна
    assert clinic_phrase("Профессиональный хоккеист получил травму — оплатят?", CTX, st)[0] is None
    assert clinic_phrase("Чудесный Доктор на Школьной — терапевт по полису?", CTX, st)[0] == "Чудесный Доктор"
    slots = rule_slots("Прием невролога в Семейном докторе на Дежнева — входит?", CTX, st)
    assert slots["clinic"] == "Семейном докторе" and slots["care_type"] == "Амбулаторно-поликлиническая помощь"
    assert slots["service"] == "невролога"


def test_absent_only_for_confident_name_without_overlap():
    st = _kit()
    run = _run(st)
    # уверенное название, ни одна строка программы не похожа → «нет в перечне» + другая программа договора
    value, _ = check_clinic(run, "Стандарт", "Чудесный Доктор", None, "терапевт")
    assert value == "absent"
    assert any("Премиум" in s.text for s in run.statements)
    # название из одних общих слов — не absent; частичное сходство отличительного слова — not_found
    assert check_clinic(_run(st), "Стандарт", "Детская поликлиника", None, None)[0] == "not_found"
    assert check_clinic(_run(st), "Стандарт", "Медиксе Плюс", None, None)[0] == "not_found"
    # одно общее слово («Клиник» из «Он Клиник») — название не распознано
    assert check_clinic(_run(st), "Стандарт", "Клиник", None, None)[0] == "not_found"
    # название — город перечня («Томск») — не клиника
    assert check_clinic(_run(st), "Стандарт", "Томск", None, None, city="Томск")[0] == "not_found"


# ---------------------------------------------------------------- 2. лимит


def test_limit_parsing_units_and_periods():
    assert [(x.n, x.unit, x.period) for x in limits_in("Ударно-волновая терапия не более 5 сеансов в год")] == \
        [(5, "procedure", "year")]
    assert [(x.n, x.period) for x in limits_in("Не более 3 процедур на курс, в год совокупно не более 10")] == \
        [(3, "course")]
    assert [(x.n, x.unit) for x in limits_in("онкомаркеры: 2 раза")] == [(2, "any")]
    assert [x.n for x in limits_in("консультация сомнолога однократно")] == [1]
    assert [(x.n, x.unit) for x in limits_in("Удаление папиллом: не более 3 (трех) образований")] == [(3, "objects")]
    assert [x.period for x in limits_in("1 раз в 3 года")] == ["other"]
    # счёт в вопросе: звонки — не обращения; разные числа — неоднозначно
    assert asked_count("Уже 6 раз звонили в клинику, не записывают к психотерапевту") is None
    assert asked_count("Уже 10 процедур грязелечения было, теперь ванны, первая процедура") is None
    assert asked_count("6 раз уже был у сомнолога, еще раз можно?") == 7
    assert asked_count("Удалить 4-ю папиллому за год", ("папиллом",)) == 4


LIMITS = [
    (None, f"{PROG} › Программа включает", "Программа включает:"),
    ("2.1", f"{PROG} › Программа включает › 2.1",
     "2.1. Бальнеотерапия: жемчужные ванны, грязелечение — не более 10 процедур каждого вида в год."),
    ("2.2", f"{PROG} › Программа включает › 2.2",
     "2.2. Мануальная терапия — не более 3 процедур на курс по страховому случаю."),
    ("2.3", f"{PROG} › Программа включает › 2.3",
     "2.3. Консультации психотерапевта не более 5 раз в год."),
    ("2.4", f"{PROG} › Программа включает › 2.4",
     "2.4. Лечебная физкультура (не более 10 сеансов); массаж (не более 10 процедур); иглорефлексотерапия."),
    ("2.5", f"{PROG} › Программа включает › 2.5",
     "2.5. Удаление новообразований кожи (папиллом): не более 3 (трех) образований."),
]


def test_limit_exceeded_only_with_same_service_unit_period():
    skip_unless_limit_deny()
    st = _kit(LIMITS + EXCLUSIONS)
    # курс: вопрос о том же курсе — «нельзя»; второй курс — сброс, условие
    _s, v, label = _sc01(st, "Мануальная терапия: четвертая процедура в том же курсе — оплатят?",
                         {"service": "мануальная терапия"})
    assert label is Label.denied and v.draft.checks.exclusion == "yes"
    # услуга не та: лимит в соседнем пункте перечня через «;» («ЛФК (не более 10…); …; иглорефлексотерапия»)
    _s, v, label = _sc01(st, "11-я процедура иглорефлексотерапии — оплатят?", {"service": "иглорефлексотерапии"})
    assert label is not Label.denied
    # «N образований» — тоже лимит; единица вопроса — слово услуги
    _s, v, label = _sc01(st, "Удалить 4-ю папиллому за год — по ДМС?", {"service": "папиллому"})
    assert label is Label.denied


# ---------------------------------------------------------------- 3. специалист → поликлиника, «можно»


def test_specialist_and_diagnostics_route_to_outpatient():
    assert _care_types("Прием невролога в клинике")[0] == "Амбулаторно-поликлиническая помощь"
    assert _care_types("Терапевт на дом — можно?")[0] == "Помощь на дому"            # синоним раньше специалиста
    assert _care_types("Мануальный терапевт: четвертая процедура")[0] == "Восстановительное лечение"
    # диагностика — в рамках поликлиники (part_of): клиника «Поликлиника» подходит для УЗИ
    phrases = _care_phrase_sets("Диагностика")
    assert any("поликлиника" in p for p in phrases)


def test_allowed_with_clinic_text_included_and_exclusions_seen():
    skip_unless_rule("covered")
    st = _kit(INCLUDED + EXCLUSIONS)
    q = "Прием невролога в Ромашке здоровья, по ДМС пойдет?"
    _slots, v, label = _sc01(st, q)
    ch = v.draft.checks
    assert ch.clinic_in_program == "yes" and ch.care_in_program == "yes" and ch.exclusion == "absent"
    # без выбора менеджера «можно» не ставится (ADR 0010); первый кандидат — строгое совпадение «Ромашка здоровья»
    assert label is not Label.allowed
    row = v.draft.clinic_choice.candidates[0]
    assert row.clinic_name == "АО «Ромашка здоровья»"
    _slots, v, label = _sc01(st, q, pick=row.id)
    assert v.draft.clinic_choice.selected == row.id and label is Label.allowed
    # без просмотренных исключений — не «можно»
    _s, _v2, label2 = _sc01(_kit(INCLUDED), q, pick=row.id)
    assert label2 is not Label.allowed
    # клиника сообщила «специалист по ДМС не принимает» — не «можно» (правило договора о приёме не по ДМС)
    _s, _v3, label3 = _sc01(st, "В Ромашке здоровья сказали, что невролог по ДМС не принимает, только за свой счет",
                            pick=row.id)
    assert label3 is not Label.allowed


# ---------------------------------------------------------------- 4. заграница


ABROAD = [
    ("3.5", f"{PROG} › Раздел III › 3.5",
     "3.5. Медицинские услуги, предоставляемые вне территории Российской Федерации при острых состояниях:"),
    ("3.5.2", f"{PROG} › Раздел III › 3.5.2", "3.5.2. Стоматологическая помощь при острой боли:"),
    ("3.5.2.1", f"{PROG} › Раздел III › 3.5.2.1", "3.5.2.1. Лечение кариеса зубов при острой боли."),
    (None, f"{PROG} › Исключения из программы", "Исключения из программы:"),
    ("4.1", f"{PROG} › Исключения из программы › 4.1", "4.1. Лечение кариеса зубов, пломбирование."),
]


def test_abroad_by_country_and_subitems_only_for_abroad_question():
    assert question_abroad("Застрахованный в Турции, острая зубная боль — стоматолога оплатят?")
    assert question_abroad("Плановая операция в Израиле — по полису?")
    assert not question_abroad("Плановая операция в Москве — по полису?")
    st = _kit(ABROAD)
    s = _Sections(st.clauses("c01"))
    sub = next(c for c in st.clauses("c01") if c.number == "3.5.2.1")
    assert s.abroad(sub)
    # вопрос о России: 3.5.2.1 не участвует ни во «входит», ни в противоречии — только исключение 4.1
    hits = coverage_hits(_run(st), "Стандарт", service_terms("лечение кариеса"))
    assert {h.clause.number for h in hits} == {"4.1"}
    hits_abroad = coverage_hits(_run(st), "Стандарт", service_terms("лечение кариеса"), abroad=True)
    assert {h.clause.number for h in hits_abroad} == {"3.5.2.1"}


# ---------------------------------------------------------------- 5. противоречия


def test_contradiction_needs_whole_service_and_agreed_exception():
    v = service_terms("терапевта")
    assert strict_hit(v, "Приём врача-терапевта в поликлинике")
    assert not strict_hit(v, "терапевтическая санация полости рта")
    rows = [
        (None, f"{PROG} › Программа включает", "Программа включает:"),
        ("1.1", f"{PROG} › Программа включает › 1.1", "1.1. Приём врача-терапевта в поликлинике."),
        ("1.2", f"{PROG} › Программа включает › 1.2",
         "1.2. Удаление внутриматочной спирали по показаниям (исключая случаи, связанные с беременностью)."),
        (None, f"{PROG} › Исключения из программы", "Исключения из программы:"),
        ("4.1", f"{PROG} › Исключения из программы › 4.1", "4.1. Терапевтическая санация полости рта."),
        ("4.2", f"{PROG} › Исключения из программы › 4.2",
         "4.2. Удаление внутриматочной спирали в связи с беременностью."),
    ]
    st = _kit(rows)
    cov = check_coverage(_run(st), "Стандарт", "терапевта", "not_found")
    assert not cov.contradictory and cov.exclusion != "yes" and cov.care == "yes"
    cov2 = check_coverage(_run(st), "Стандарт", "удаление внутриматочной спирали", "not_found")
    assert not cov2.contradictory and cov2.exclusion != "yes" and "other" in cov2.conditions
    # услуга — одно слово вида помощи («диагностика»): исключение о болезни с ним не сравнивается
    rows3 = [*INCLUDED, (None, f"{PROG} › Исключения из программы", "Исключения из программы:"),
             ("4.3", f"{PROG} › Исключения из программы › 4.3",
              "4.3. Диагностика и лечение иммунодефицитных состояний.")]
    cov3 = check_coverage(_run(_kit(rows3)), "Стандарт", "диагностика", "yes", care="Диагностика")
    assert not cov3.contradictory and cov3.exclusion != "yes"


# ---------------------------------------------------------------- 6. маркеры отказа


def test_natural_refusal_phrases():
    for q in ("В клинике не берут по полису массаж", "Денег хотят за УЗИ", "Сказали плати сам",
              "Не стали записывать к неврологу", "Говорят, это не страховой случай", "Отправили на кассу",
              "Выписали чек за приём", "Предложили прайс", "Пришлось раскошелиться на МРТ",
              "Там только коммерческая запись", "Страховая не гарантирует оплату", "бесплатно не приняли",
              "по полису не принимают", "Касса потребовала оплату приёма"):
        assert rule_scenario(q) == Scenario.sc01, q
        assert rule_slots(q, CTX, None).get("refusal"), q
    assert not rule_slots("Можно ли пройти чек-ап?", CTX, None).get("refusal")


# ---------------------------------------------------------------- 7. раздел другого вида помощи


def test_pharmacy_section_exclusion_not_for_treatment_question():
    rows = [
        (None, f"{PROG} › Программа «АПТЕКА»", "Программа «АПТЕКА» (лекарственное обеспечение):"),
        ("30", f"{PROG} › Программа «АПТЕКА» › Программа не предусматривает оплату лекарственных средств › 30",
         "30. Препараты для лечения бесплодия."),
        (None, f"{PROG} › Исключения из программы", "Исключения из программы:"),
        ("4", f"{PROG} › Исключения из программы › 4", "4. Диагностика и лечение бесплодия."),
    ]
    st = _kit(rows)
    s = _Sections(st.clauses("c01"))
    ph = next(c for c in st.clauses("c01") if c.number == "30")
    assert section_care(s, ph) == "pharmacy"
    assert not care_compatible("pharmacy", None, "Лечение бесплодия по полису?")
    assert care_compatible("pharmacy", None, "Препараты от бесплодия по полису?")
    _s, v, label = _sc01(st, "Лечение бесплодия по полису?", {"service": "Лечение бесплодия"})
    assert label is Label.denied
    basis = [c.quote for x in v.draft.statements if "exclusion" in x.supports for c in x.citations]
    assert basis and all("Препараты" not in q for q in basis)


# ---------------------------------------------------------------- регрессия: 14 прежних ложных «нельзя» (r3)
# Каждый случай — синтетический аналог вопроса ревизии 3, где прежнее правило давало «нельзя»: клиника в косвенном
# падеже не находилась («нет в перечне»), лимит сравнивался без учёта вида, периода и того, что посчитано.


def _not_denied_clinic_yes(question: str) -> None:
    st = _kit(INCLUDED + EXCLUSIONS)
    slots, v, label = _sc01(st, question)
    assert label is not Label.denied, (question, slots)
    assert v.draft.checks.clinic_in_program == "yes", (question, slots)


def test_r3_024_indirect_case_two_words():
    _not_denied_clinic_yes("Прием невролога в Ромашке здоровья, по ДМС пойдет?")


def test_r3_118_generic_name_with_street():
    _not_denied_clinic_yes("Прием невролога в Семейном докторе на Дежнева — входит?")


def test_r3_224_generic_name_without_service():
    st = _kit(INCLUDED + EXCLUSIONS)
    _s, v, label = _sc01(st, "Можно ли обслуживаться в Семейном докторе?")
    assert label is not Label.denied and v.draft.checks.clinic_in_program == "yes"


def test_r3_155_clinic_named_like_foreign_city():
    _not_denied_clinic_yes("Прием терапевта в Вербе на Профсоюзной — по полису?")


def test_r3_041_adjective_name_with_generic_words():
    _not_denied_clinic_yes("Прием терапевта в Австрийском медицинском центре — оплатят по полису?")


def test_r3_106_city_after_name():
    _not_denied_clinic_yes("Прием педиатра... ой, терапевта в Пилюлькине Казань — входит?")


def test_r3_097_hyphen_name_city_in_genitive():
    _not_denied_clinic_yes("в ТрансМедицине Самары взяли деньги за УЗИ. Вернут?")


def test_r3_020_full_name_vs_acronym():
    _not_denied_clinic_yes("удалить зуб в Северской областной клинической больнице — по полису можно?")


def test_r3_022_name_in_parentheses_of_legal_name():
    _not_denied_clinic_yes("В Добром лекаре попросили оплатить 15 % от пломбы. Это законно?")


def test_r3_027_query_acronym_vs_full_name():
    # этап w5-strict: сокращение («БСМП» ↔ «Больница скорой медицинской помощи») — только кандидат, личность клиники
    # им не подтверждается: «не нашли» (not_found), но и не «нет в перечне» / «нельзя»
    st = _kit(INCLUDED + EXCLUSIONS)
    slots, v, label = _sc01(st, "Экстренная госпитализация в БСМП Томска — покроет?")
    assert label is not Label.denied, slots
    assert v.draft.checks.clinic_in_program == "not_found", slots


def test_r3_160_type_acronym_is_generic():
    _not_denied_clinic_yes("Прием эндокринолога в КДЦ Северный на Варшавке — можно?")


def test_r3_004_limit_of_other_kind():
    st = _kit(LIMITS + EXCLUSIONS)
    _s, v, label = _sc01(st, "Уже 10 процедур грязелечения было, теперь назначили жемчужные ванны, первая "
                             "процедура. Оплатят?", {"service": "жемчужные ванны"})
    assert label is not Label.denied and v.draft.checks.exclusion != "yes"


def test_r3_140_limit_per_course_new_course():
    st = _kit(LIMITS + EXCLUSIONS)
    _s, v, label = _sc01(st, "4-я процедура мануальной терапии — но это уже второй курс по новой травме. Оплатят?",
                         {"service": "мануальной терапии"})
    assert label is not Label.denied and v.draft.checks.exclusion != "yes"


def test_r3_220_calls_are_not_visits():
    st = _kit(LIMITS + EXCLUSIONS)
    _s, v, label = _sc01(st, "Уже 6 раз звонили в клинику, не записывают к психотерапевту. Положено?",
                         {"service": "психотерапевту"})
    assert label is not Label.denied and v.draft.checks.exclusion != "yes"
