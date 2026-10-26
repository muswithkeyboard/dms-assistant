"""Исправления по ревизии w5-coverage (ложные «можно» / «нельзя»): синтетика на каждое правило, без сети.

1. Раздел пункта: предок-исключение сильнее вводной самого пункта; общие слова видов помощи — только заголовки-предки.
2. Уточнение услуги («анализы на ВИЧ») не теряется: «входит» по строке вида помощи матрицы не ставится, исключение
   без слов процедуры — оговорка, а не «нельзя».
4. Оговорки исключений «чаще, чем», «при наличии», инвалидность застрахованного — условие, не «нельзя».
5. Оговорки в «входит» («первичная…», «в объеме N…») — условие; синонимы услуги дают противоречие.
"""
from __future__ import annotations

from dms_core.constants import Label
from dms_core.guided import (
    _Run,
    _Sections,
    bound_executor,
    check_care,
    exclusion_hit,
    heading_section,
    service_terms,
)
from dms_core.schemas import CareMatrix
from guided.test_coverage import INCLUDED, PROG, _cl, _sc01, _store, _supported

# ГК 3: «IV. Исключения» → «4.2 … не оплачивается Страховщиком:» → «4.2.2 Приемы, консультации … по: …»
SOGAZ_EXCL = [
    ("IV", f"{PROG} › Раздел IV", "IV. ИСКЛЮЧЕНИЯ ИЗ ПРОГРАММЫ:"),
    ("4.1", f"{PROG} › Раздел IV › 4.1",
     "4.1. Перечень заболеваний, обращение по поводу которых не является страховым случаем:"),
    ("4.1.1", f"{PROG} › Раздел IV › 4.1.1", "4.1.1. ВИЧ – инфекция, СПИД, и их осложнения."),
    ("4.1.15", f"{PROG} › Раздел IV › 4.1.15",
     ("4.1.15. Заболевания, послужившие основанием для установления Застрахованному лицу инвалидности 1 или 2 "
      "группы – при наличии у Застрахованного лица инвалидности на дату вступления в силу договора.")),
    ("4.2", f"{PROG} › Раздел IV › 4.2",
     "4.2. Медицинские услуги, обращение за которыми не является страховым случаем и не оплачивается Страховщиком:"),
    ("4.2.2", f"{PROG} › Раздел IV › 4.2.2",
     ("4.2.2. Приемы, консультации и манипуляции врачей-специалистов и среднего медицинского персонала по: "
      "гериатрии, диабетологии, косметологии, сексологии.")),
    ("4.2.31", f"{PROG} › Раздел IV › 4.2.31",
     ("4.2.31. Услуги, оказываемые в профилактических и косметических целях: герметизация фиссур, химическое "
      "отбеливание зубов, реставрация зубных рядов.")),
    ("4.4", f"{PROG} › Раздел IV › 4.4",
     "4.4. С введением иммуноглобулинов против клещевого энцефалита чаще, чем один раз в месяц."),
]


# ---------------------------------------------------------------- 1. раздел пункта

def test_exclusion_ancestor_beats_own_intro_with_generic_words():
    st = _store(SOGAZ_EXCL)
    clauses = st.clauses("c01")
    s = _Sections(clauses)
    by_num = {c.number: c for c in clauses if c.number}
    # вводная «Приемы, консультации и манипуляции … по:» раньше давала «входит» по общим словам
    assert s.section(by_num["4.2.2"])[0] == "exclusion"
    assert s.section(by_num["4.2.31"])[0] == "exclusion"
    assert heading_section("Приемы, консультации и манипуляции врачей по:", ancestor=False) is None
    assert heading_section("Поликлиническое обслуживание") == "included"            # заголовок-предок
    assert heading_section("Поликлиническое обслуживание", ancestor=False) is None
    assert heading_section("3.6. Страховщик отказывает в выплате страхового возмещения в случаях если:") == \
        "exclusion"


def test_specialist_in_exclusion_list_is_denied_not_included():
    # «гериатр» в перечне 4.2.2 (далеко от начала пункта) — исключение по части перечня после «по:»
    st = _store(SOGAZ_EXCL + INCLUDED)
    _run, v, label, rule = _sc01(st, {"service": "гериатра"})
    assert v.draft.checks.care_in_program != "yes"
    assert v.draft.checks.exclusion == "yes" and label is Label.denied and rule == "excluded"
    assert "гериатрии" in _supported(v, "exclusion")[0].citations[0].quote
    _run, v2, label2, _ = _sc01(st, {"service": "реставрация зубных рядов"})
    assert v2.draft.checks.exclusion == "yes" and label2 is Label.denied


def test_flat_path_lookback_exclusion_same_path_only():
    cl = [
        _cl("c01", 0, None, "П › Страховщик не оплачивает", "Страховщик не оплачивает:"),
        _cl("c01", 1, None, "П › Страховщик не оплачивает", "Гирудотерапию и озонотерапию"),
        _cl("c01", 2, None, "П › Прочее", "Сведения о порядке"),
        _cl("c01", 3, None, "П › Прочее", "Гемодиализ"),
    ]
    s = _Sections(cl)
    assert s.section(cl[1])[0] == "exclusion"            # тот же путь, что у заголовка исключений
    assert s.section(cl[3])[0] is None                   # другой путь: поиск остановлен, раздел не угадывается


# ---------------------------------------------------------------- 2. уточнение услуги

def _diag_store(extra):
    care = [CareMatrix(contract_id="c01", program="Стандарт", care_type="diagnostics", present=True,
                       source_refs=["t0:r1"])]
    return _store(extra, care=care)


def test_matrix_care_type_does_not_cover_qualified_service():
    st = _diag_store(SOGAZ_EXCL + INCLUDED)
    run = _Run(cid="c01", st=st, execute=bound_executor("c01", st))
    assert check_care(run, "Стандарт", "Диагностика", "УЗИ").value == "yes"
    assert check_care(run, "Стандарт", "Диагностика", "анализы на ВИЧ").value == "not_found"
    # место исследования (anatomy_words) — не уточнение вида услуги
    assert check_care(run, "Стандарт", "Диагностика", "УЗИ щитовидной железы").value == "yes"


def test_disease_in_exclusion_without_procedure_word_is_caution_not_denied():
    # «анализы на ВИЧ»: в исключении 4.1.1 названа болезнь, а не анализ → условие, а не «нельзя»; и не «можно»
    st = _diag_store(SOGAZ_EXCL + INCLUDED)
    _run, v, label, _rule = _sc01(st, {"care_type": "Диагностика", "service": "анализы на ВИЧ"})
    ch = v.draft.checks
    assert ch.exclusion not in ("yes", "absent") and ch.care_in_program != "yes"
    assert label not in (Label.denied, Label.allowed)
    assert any("ВИЧ" in c.quote for s in _supported(v, "conditions.other") for c in s.citations)
    t = service_terms("анализы на ВИЧ")[0]
    assert t.reduced() is not None and t.reduced().required == ("вич",)


def test_anatomy_is_not_disease_for_exclusion():
    st = _diag_store([*SOGAZ_EXCL, *INCLUDED,
                      ("4.1.10", f"{PROG} › Раздел IV › 4.1.10",
                       "4.1.10. Новообразования головного мозга и их осложнения.")])
    _run, v, label, _rule = _sc01(st, {"care_type": "Диагностика", "service": "МРТ головного мозга"})
    assert v.draft.checks.exclusion != "yes" and label is not Label.denied


# ---------------------------------------------------------------- 4. оговорки исключений

def test_exclusion_cautions_frequency_and_insured_status():
    st = _store(SOGAZ_EXCL + INCLUDED)
    _run, v, label, _ = _sc01(st, {"service": "введение иммуноглобулина клещевого энцефалита"})
    assert v.draft.checks.exclusion != "yes" and label is not Label.denied
    _run, v2, label2, _ = _sc01(st, {"service": "лечение заболевания инвалидность"})
    assert v2.draft.checks.exclusion != "yes" and label2 is not Label.denied
    assert _supported(v2, "conditions.other")


# ---------------------------------------------------------------- 5. оговорки «входит», синонимы

def test_included_caution_is_condition():
    inc = [*INCLUDED, ("1.5", f"{PROG} › Программа включает › 1.5",
                       "1.5. Первичная консультация врача-психиатра без проведения лечения.")]
    st = _store(SOGAZ_EXCL + inc)
    _run, v, label, _ = _sc01(st, {"service": "консультация психиатра"})
    assert v.draft.checks.care_in_program == "yes" and "other" in v.draft.checks.conditions
    assert label is Label.conditional


def test_synonym_service_gives_contradiction():
    extra = [*INCLUDED,
             ("1.6", f"{PROG} › Программа включает › 1.6", "1.6. Лечение заболеваний, передающихся половым путем (ЗППП)."),
             (None, f"{PROG} › Исключения из программы", "Исключения из программы:"),
             ("2", f"{PROG} › Исключения из программы › 2", "2. Венерических заболеваний, сифилиса.")]
    st = _store(extra)
    _run, v, label, _ = _sc01(st, {"service": "лечение ЗППП"})
    assert v.draft.checks.documents == "contradictory" and label is Label.conditional
    assert exclusion_hit(service_terms("лечение ЗППП"), "Венерических заболеваний, сифилиса")
