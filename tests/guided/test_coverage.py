"""Покрытие услуги в guided (этап w5-coverage): раздел найденного пункта («входит» / «исключение» / «условие»)
по заголовкам, совпадение услуги по основам слов, матрица видов помощи с составными строками.

Синтетика — договор c01 из tests/fixtures/synth_kit.py с добавленными пунктами программы трёх устройств:
путь с заголовком раздела (ГК 2), пункт-родитель по номеру («4» → «4.1», ГК 2), «Раздел IV» + таблица услуг
(ГК 3), плоский путь с ближайшим предшествующим заголовком (ГК 2). Документов организаторов нет; тесты на копии
базы — SKIP без data/derived/kit.sqlite.
"""
from __future__ import annotations

from dms_core.adapters.base import text_hash
from dms_core.citations import verify
from dms_core.constants import Label, Scenario
from dms_core.guided import (
    ServiceTerms,
    _Run,
    _Sections,
    bound_executor,
    care_row_matches,
    check_care,
    check_coverage,
    heading_section,
    run_guided,
    service_terms,
    terms_hit,
)
from dms_core.scenarios.sc01_refusal import decide_label
from dms_core.schemas import CareMatrix, ChatContext, Clause
from dms_core.store import KitStore
from dms_core.store.db import default_path
from fixtures.synth_kit import make_parsed
from helpers import skip

PROG = "Программа «Стандарт»"


def _cl(cid: str, n: int, number: str | None, path: str, text: str) -> Clause:
    return Clause(id=f"{cid}.program#x{n}", doc_id=f"{cid}.program", contract_id=cid, number=number,
                  path=path, program="Стандарт", text=text, text_hash=text_hash(text))


def _store(extra: list[tuple[str | None, str, str]], care: list[CareMatrix] | None = None) -> KitStore:
    """c01 + пункты программы (номер, путь, текст) в порядке документа."""
    p = make_parsed("c01")
    for i, (number, path, text) in enumerate(extra):
        p.clauses.append(_cl("c01", i, number, path, text))
    if care is not None:
        p.care = care
    st = KitStore(":memory:")
    st.save(p)
    st.save(make_parsed("c02"))
    return st


EXCLUSIONS = [
    (None, f"{PROG} › Исключения из программы", "Исключения из программы:"),
    ("3", f"{PROG} › Исключения из программы › 3",
     "3. Профилактические и косметические мероприятия; полировка зубов; герметизация фиссур; отбеливание зубов."),
    ("4", f"{PROG} › Исключения из программы › 4",
     ("4. Проведение магнитно-резонансной томографии (МРТ), за исключением случаев, когда исследование "
     "назначено по направлению лечащего врача при травме.")),
]
INCLUDED = [
    (None, f"{PROG} › Программа включает", "Программа включает:"),
    ("1.1", f"{PROG} › Программа включает › 1.1",
     "1.1. Ультразвуковое исследование органов брюшной полости по направлению лечащего врача."),
    ("1.2", f"{PROG} › Программа включает › 1.2", "1.2. Лазерная коррекция зрения не входит в программу."),
    ("1.3", f"{PROG} › Программа включает › 1.3", "1.3. Приём врача-терапевта в поликлинике по месту жительства."),
]


def _sc01(st: KitStore, slots: dict, question: str = "Положена ли услуга?"):
    ctx = ChatContext(group_id="gk9", contract_id="c01", program="Стандарт")
    run = run_guided("c01", question, scenario=Scenario.sc01, slots=dict(slots), context=ctx, store=st)
    v = verify(run.draft, run.refs, st, "c01", program="Стандарт", tools=run.tools)
    label, _note, rule = decide_label(v.draft.checks, has_alternative=bool(v.draft.alternatives))
    if not v.citations:
        label, rule = Label.insufficient, "no_evidence"
    return run, v, label, rule


def _supported(v, name: str) -> list:
    return [s for s in v.draft.statements if name in s.supports]


# ---------------------------------------------------------------- ветки решения

def test_exclusion_yes_from_exclusion_section_with_quote():
    # «стоматолог предлагает герметизацию фиссур»: «стоматолог» — слово контекста, услуга — «герметизация фиссур»
    st = _store(EXCLUSIONS + INCLUDED)
    _run, v, label, rule = _sc01(st, {"service": "стоматолог герметизацию фиссур"})
    assert v.draft.checks.exclusion == "yes" and label is Label.denied and rule == "excluded"
    backer = _supported(v, "exclusion")
    assert backer and "герметизация фиссур" in backer[0].citations[0].quote
    assert backer[0].citations[0].ref == "c01.program#x1"          # пункт 3 раздела исключений


def test_care_yes_from_included_section_and_exclusion_absent():
    # матрица не знает УЗИ (только dental/inpatient) → «входит» из пункта раздела «Программа включает»
    st = _store(EXCLUSIONS + INCLUDED)
    _run, v, label, _rule = _sc01(st, {"service": "УЗИ брюшной полости"})
    ch = v.draft.checks
    assert ch.care_in_program == "yes" and _supported(v, "care_in_program")
    assert "c01.program#x4" in {c.ref for s in _supported(v, "care_in_program") for c in s.citations}   # п. 1.1
    # исключения программы найдены, УЗИ в них нет → absent (а не «нельзя»)
    assert ch.exclusion == "absent" and label is not Label.denied
    # «по направлению лечащего врача» в том же пункте → условие referral → «можно при условиях»
    assert "referral" in ch.conditions and label is Label.conditional


def test_not_included_inside_included_section_gives_no_yes():
    # «Лазерная коррекция зрения не входит в программу» стоит в разделе «Программа включает»: раздел — «входит»,
    # но отрицание в тексте фрагмента → не care=yes; и это не пункт исключений → не exclusion=yes
    st = _store(EXCLUSIONS + INCLUDED)
    _run, v, label, _rule = _sc01(st, {"service": "лазерная коррекция зрения"})
    assert v.draft.checks.care_in_program != "yes"
    assert v.draft.checks.exclusion != "yes" and label is not Label.denied
    shown = [s for s in v.draft.statements if "не входит" in s.text]
    assert shown and not shown[0].supports            # показан как ограничение, проверку не подтверждает


def test_generic_word_alone_is_no_match():
    # «приём» — общее слово ДМС: основ у услуги нет → ни «входит», ни «исключение»
    assert service_terms("приём") == []
    st = _store(EXCLUSIONS + INCLUDED)
    _run, v, label, _rule = _sc01(st, {"service": "приём"})
    assert v.draft.checks.care_in_program == "not_found" and v.draft.checks.exclusion == "not_found"
    assert label is Label.insufficient
    # «приём терапевта» совпадает по «терапевт», а не по «приём»
    t = service_terms("приём терапевта")
    assert t and t[0].required == ("терапе",)
    assert terms_hit(t[0], "Приём врача-терапевта в поликлинике")
    assert not terms_hit(t[0], "Приём врача-хирурга в поликлинике")


def test_except_clause_in_exclusions_is_condition_not_exclusion():
    # «МРТ …, за исключением случаев, когда … по направлению врача»: в части случаев МРТ оплачивается, поэтому
    # из пункта следует не «нельзя», а условие (вид — по тексту оговорки: направление); exclusion ≠ yes/absent
    st = _store(EXCLUSIONS + INCLUDED)
    _run, v, label, _rule = _sc01(st, {"service": "МРТ"})
    ch = v.draft.checks
    assert ch.exclusion == "not_found" and label is not Label.denied
    conds = [s for s in v.draft.statements if any(x.startswith("conditions.") for x in s.supports)]
    assert any("за исключением" in s.citations[0].quote for s in conds)


def test_included_and_excluded_is_contradiction_not_denied():
    both = [*EXCLUSIONS, *INCLUDED,
            ("1.4", f"{PROG} › Программа включает › 1.4", "1.4. Герметизация фиссур постоянных зубов у взрослых.")]
    st = _store(both)
    _run, v, label, rule = _sc01(st, {"service": "герметизация фиссур"})
    assert v.draft.checks.exclusion != "yes" and v.draft.checks.documents == "contradictory"
    assert v.draft.contradictions and label is Label.conditional and rule == "contradiction"


def test_care_level_word_mention_in_exclusion_is_not_exclusion():
    # «стационар»: вид помощи в матрице есть (inpatient present у c01); пункт исключений об «организации
    # стационара на дому» — о другой услуге, вид помощи целиком исключённым не становится
    extra = [(None, f"{PROG} › Страховщик не оплачивает", "Страховщик не оплачивает:"),
             ("2", f"{PROG} › Страховщик не оплачивает › 2", "2. Организацию стационара на дому и индивидуального поста.")]
    st = _store(extra)
    _run, v, label, _rule = _sc01(st, {"care_type": "Стационарная помощь", "service": "стационар"})
    assert v.draft.checks.care_in_program == "yes" and v.draft.checks.exclusion != "yes"
    assert label is not Label.denied


def test_absent_exclusion_needs_included_found():
    # исключения есть, но услуга не найдена ни в матрице, ни в «входит» → exclusion не absent (сомнение)
    st = _store(EXCLUSIONS)
    _run, v, _label, _rule = _sc01(st, {"service": "УЗИ брюшной полости"})
    assert v.draft.checks.care_in_program == "not_found" and v.draft.checks.exclusion == "not_found"


def test_mention_deep_in_exclusion_blocks_absent():
    # услуга названа в пункте исключений, но не в начале (пункт о другом) — не «исключение», но и не absent
    extra = [*INCLUDED, (None, f"{PROG} › Страховщик не оплачивает", "Страховщик не оплачивает:"),
             ("5", f"{PROG} › Страховщик не оплачивает › 5",
              ("5. Медицинские услуги, связанные с подготовкой к оперативному лечению, включая ультразвуковое "
              "исследование брюшной полости."))]
    st = _store(extra)
    _run, v, label, _rule = _sc01(st, {"service": "УЗИ брюшной полости"})
    assert v.draft.checks.care_in_program == "yes"
    assert v.draft.checks.exclusion == "not_found" and label is not Label.denied


# ---------------------------------------------------------------- разделы по заголовкам

def test_heading_negation_beats_inclusion_word():
    assert heading_section("Медицинские услуги, не входящие в программу") == "exclusion"
    assert heading_section("Услуги, которые не входят в программу:") == "exclusion"
    assert heading_section("Программа включает:") == "included"
    assert heading_section("IV. ИСКЛЮЧЕНИЯ ИЗ ПРОГРАММЫ:") == "exclusion"
    assert heading_section("VI. ПОРЯДОК ПРЕДОСТАВЛЕНИЯ МЕДИЦИНСКИХ УСЛУГ ПО ПРОГРАММЕ:") == "condition"


def test_section_by_numeric_parent_and_table_cell_and_lookback():
    cl = [
        # ГК 2: «4.1» лежит в пути под «страховыми случаями», но его родитель по номеру — «4. Исключения…»
        _cl("c01", 0, None, "П › Страховыми случаями являются", "По настоящей Программе страховыми случаями являются:"),
        _cl("c01", 1, "4", "П › Страховыми случаями являются › 4",
            "4. Исключения из страхования. Расходы, не возмещаемые Страховщиком:"),
        _cl("c01", 2, "4.1", "П › Страховыми случаями являются › 4.1", "4.1. Укусы клещей вне территории."),
        # ГК 3: таблица услуг под разделом «франшиза» — раздел строки задаёт ячейка «да»
        _cl("c01", 3, "VII", "П › Раздел VII", "VII. ИСПОЛНЕНИЕ ОБЯЗАТЕЛЬСТВА ПО ОПЛАТЕ ФРАНШИЗЫ"),
        _cl("c01", 4, None, "П › Раздел VII › 7.1 › табл. 8, стр. 3",
            "диагностика | магнитно-резонансная томография (МРТ) | да"),
        # ГК 2: плоский путь — ближайший предшествующий заголовок раздела видов помощи
        _cl("c01", 5, None, "П › Стоматологическое обслуживание", "Стоматологическое обслуживание"),
        _cl("c01", 6, None, "П › Снятие зубных отложений", "Снятие зубных отложений (без ограничения)"),
        _cl("c01", 7, None, "П › Снятие зубных отложений", "Герметизация фиссур."),
        # заголовок исключений при поиске назад останавливает поиск: раздел не угадывается
        _cl("c01", 8, None, "П › Страховщик не оплачивает", "Страховщик не оплачивает:"),
        _cl("c01", 9, None, "П › Прочее", "Прочие сведения"),
        _cl("c01", 10, None, "П › Прочее", "Отбеливание зубов."),
    ]
    s = _Sections(cl)
    assert s.section(cl[2])[0] == "exclusion"
    assert s.section(cl[4])[0] == "included"
    assert s.section(cl[7])[0] == "included"
    assert s.section(cl[10])[0] is None


def test_composite_matrix_row_and_service_row_with_limit():
    # «Поликлиника | Врач на дом» — составная строка: каждая часть сравнивается отдельно
    assert care_row_matches("Поликлиника | Врач на дом", [["дом"]])
    assert not care_row_matches("Поликлиническое обслуживание без вызова врача на дом", [["вызов", "дом"]])
    # ГК 1: строка матрицы названа услугой; «не более 5 раз в год» → условие limit со ссылкой на строку
    row = ('Расширение программы "Стандарт": Консультации и лечение у психотерапевта '
           '(амбулаторно-поликлиническое обслуживание) не более 5 раз в год')
    st = _store([], care=[CareMatrix(contract_id="c01", program="Стандарт", care_type=row, present=True,
                                     source_refs=["t0:r1"])])
    run = _Run(cid="c01", st=st, execute=bound_executor("c01", st))
    cc = check_care(run, "Стандарт", None, "психотерапевта шестой")
    assert cc.value == "yes" and cc.conditions == ["limit"]


def test_service_terms_noise_and_abbreviation():
    t = service_terms("чек-апе срок")
    assert t and t[0].required == ("чека",)              # «чек-ап» слитно, «срок» — слово ситуации
    assert terms_hit(t[0], "Медицинский осмотр (Чек-ап, см п 3.6.1.)")
    mri = service_terms("МРТ")
    assert any(terms_hit(x, "Проведение магнитно-резонансной томографии") for x in mri)
    assert service_terms("Томске плановом терапевта", skip=[])[0].required == ("планов", "терапе")
    assert terms_hit(ServiceTerms(("гермет", "фиссур")), "полировка зубов; герметизация фиссур")
    assert not terms_hit(ServiceTerms(("гермет", "фиссур")), "без герметизации фиссур")


def test_check_coverage_direct_without_service_is_noop():
    st = _store(EXCLUSIONS)
    run = _Run(cid="c01", st=st, execute=bound_executor("c01", st))
    cov = check_coverage(run, "Стандарт", None, "not_found")
    assert cov.care == "not_found" and cov.exclusion == "not_found" and not run.records


# ---------------------------------------------------------------- копия базы (без неё — SKIP)

def _kit() -> KitStore:
    if not default_path().is_file():
        skip("нет data/derived/kit.sqlite")
    return KitStore(default_path())


def _kit_sc01(st: KitStore, cid: str, program: str, slots: dict):
    ctx = ChatContext(contract_id=cid, program=program)
    run = run_guided(cid, "Положена ли услуга?", scenario=Scenario.sc01, slots=slots, context=ctx, store=st)
    v = verify(run.draft, run.refs, st, cid, program=program, tools=run.tools)
    label, _n, _r = decide_label(v.draft.checks, has_alternative=bool(v.draft.alternatives))
    return v, label


def test_kit_renaissance_sealing_excluded_in_moscow_dental_program():
    st = _kit()
    if "c05" not in {c.id for c in st.contracts()}:
        skip("нет договора c05")
    v, label = _kit_sc01(st, "c05", "Москва", {"service": "стоматолог герметизацию фиссур"})
    assert v.draft.checks.exclusion == "yes" and label is Label.denied
    assert any("фиссур" in c.quote for s in _supported(v, "exclusion") for c in s.citations)


def test_kit_sogaz_mri_conditional_and_home_visit_zone():
    st = _kit()
    if "c13" not in {c.id for c in st.contracts()}:
        skip("нет договора c13")
    v, label = _kit_sc01(st, "c13", "Бизнес", {"care_type": "Диагностика", "service": "МРТ"})
    assert v.draft.checks.care_in_program == "yes" and v.draft.checks.exclusion != "yes"
    assert label is Label.conditional
    v2, label2 = _kit_sc01(st, "c13", "Бизнес", {"care_type": "Помощь на дому", "service": "вызов врача на дом"})
    assert "zone" in v2.draft.checks.conditions and label2 is Label.conditional
