"""Выбор строки матрицы видов помощи под услугу вопроса (этап w12-care, config/service_classes.yaml).

1. Способ оказания: очный вопрос не получает «входит» по строке телемедицины, вопрос о телемедицине — по строке
   очной поликлиники; «на дом» ↔ поликлиника, «стационар» ↔ амбулаторно; «без вызова врача на дом» — не помощь
   на дому.
2. Узкая строка «в объёме лабораторных исследований» не подтверждает МРТ и приём врача; перечень «в объёме:
   приёмы специалистов (терапевт, …), инструментальные исследования (флюорография, ЭКГ)» не подтверждает МРТ.
3. Из нескольких строк выбирается подходящая; подходящей нет — not_found и справка «найден вид помощи …, но он
   не про …» без «входит».
Названия строк матрицы — выдуманные (по образцу устройства программ, без текстов комплекта).
"""
from __future__ import annotations

from dms_core.guided import (
    _Run,
    bound_executor,
    care_misfit,
    check_care,
    class_phrases,
    mode_fits,
    narrow_scope,
    pick_care_rows,
    question_modes,
    service_classes,
    text,
    text_modes,
)
from dms_core.scenarios.route import rule_slots
from dms_core.schemas import CareMatrix, ChatContext
from dms_core.store import KitStore
from fixtures.synth_kit import make_parsed
from helpers import kit_db_or_skip, kit_or_skip

TELE = "Программа Телемедицина: Консультативная онлайн-помощь через сеть Интернет дежурного терапевта/педиатра"
LAB = "Поликлиническое обслуживание, в объеме лабораторных исследований по направлению врача Пульта"
POLY = "Программа «Стандарт». Поликлиническое обслуживание без стоматологии, без вызова врача на дом"
HOME = "Программа «Стандарт». Вызов врача на дом"
HOSP = "Программа «Стандарт». Стационарное обслуживание"
CHECKUP = ("Программа «Профилактика»: Чек-ап в объеме: 5 приемов специалистов (терапевт, офтальмолог), "
           "2 инструментальных исследований (флюорография, ЭКГ)")
CTX = ChatContext(contract_id="c01", program="Стандарт")


def _kit(names: list[str]) -> KitStore:
    p = make_parsed("c01")
    p.care = [CareMatrix(contract_id="c01", program="Стандарт", care_type=n, present=True, source_refs=[f"m{i}"])
              for i, n in enumerate(names)]
    st = KitStore(":memory:")
    st.save(p)
    return st


def _care(st: KitStore, question: str) -> tuple[object, _Run]:
    slots = rule_slots(question, CTX, st)
    run = _Run(cid="c01", st=st, execute=bound_executor("c01", st))
    service = slots.get("service") or slots.get("care_type")
    care = slots.get("care_type") or slots.get("service")
    return check_care(run, "Стандарт", care, service, question=question), run


# ---------------------------------------------------------------- 1. способ оказания

def test_modes_of_rows_and_questions():
    assert text_modes(TELE) == {"telemed"}
    assert text_modes(POLY) == {"outpatient"}             # «без вызова врача на дом» — не помощь на дому
    assert text_modes(HOME) == {"home"}
    assert text_modes(HOSP) == {"inpatient"}
    assert "telemed" in text_modes(LAB)                    # «по направлению врача Пульта»
    assert question_modes("Клиника отказала в приёме терапевта", "Амбулаторно-поликлиническая помощь") == \
        {"outpatient"}
    assert question_modes("Можно ли получить онлайн-приём терапевта?", None) == {"telemed"}
    assert question_modes("Можно ли вызвать терапевта на дом?", None) == {"home"}
    assert question_modes("Сделать МРТ", "Диагностика") == frozenset()    # очно, без уточнения


def test_mode_fits():
    assert not mode_fits(frozenset({"telemed"}), frozenset())               # очный вопрос — не телемедицина
    assert not mode_fits(frozenset({"telemed"}), frozenset({"outpatient"}))
    assert mode_fits(frozenset({"telemed"}), frozenset({"telemed"}))
    assert not mode_fits(frozenset({"outpatient"}), frozenset({"telemed"}))
    assert not mode_fits(frozenset({"outpatient"}), frozenset({"home"}))
    assert not mode_fits(frozenset({"inpatient"}), frozenset({"outpatient"}))
    assert mode_fits(frozenset({"outpatient", "inpatient"}), frozenset({"inpatient"}))
    assert mode_fits(frozenset(), frozenset({"telemed"}))                    # строка без способа — любому


# ---------------------------------------------------------------- 2. класс услуги и узкая строка

def test_service_classes_and_narrow_scope():
    assert service_classes("МРТ") == {"instrumental"}
    assert service_classes("анализы крови") == {"lab"}
    assert service_classes("терапевта") == {"doctor"}
    assert service_classes("массаж") == {"procedures"}
    assert set(narrow_scope(LAB) or {}) == {"lab"}          # «по направлению врача» — не объём
    assert narrow_scope(POLY) is None
    assert narrow_scope("Стационарное обслуживание (только экстренная госпитализация)") is None


def test_care_misfit_rules():
    out = frozenset({"outpatient"})
    assert care_misfit(LAB, frozenset(), class_phrases("МРТ"))[0] == "care_misfit_class"
    assert care_misfit(LAB, out, class_phrases("терапевта"))[0] == "care_misfit_class"
    assert care_misfit(LAB, frozenset(), class_phrases("анализы")) is None
    assert care_misfit(TELE, out, class_phrases("терапевта"))[0] == "care_misfit_mode"
    assert care_misfit(CHECKUP, frozenset(), class_phrases("МРТ"))[0] == "care_misfit_class"   # перечень без МРТ
    assert care_misfit(CHECKUP, frozenset(), class_phrases("ЭКГ")) is None
    assert care_misfit(CHECKUP, out, class_phrases("терапевта")) is None
    assert care_misfit(POLY, frozenset(), class_phrases("МРТ")) is None


def test_pick_order_general_before_narrow():
    rows = [{"care_type": n} for n in (TELE, LAB, CHECKUP, POLY)]
    fit, misfit = pick_care_rows(rows, "Можно ли на приём к терапевту?", "Амбулаторно-поликлиническая помощь",
                                 "терапевта")
    assert [r["care_type"] for r in fit] == [POLY, CHECKUP]
    assert {r["care_type"] for r, _k, _w in misfit} == {TELE, LAB}


# ---------------------------------------------------------------- 3. check_care на выдуманной матрице

def test_check_care_picks_fitting_row():
    st = _kit([TELE, LAB, POLY, HOME, HOSP])
    for q, want in (("Клиника отказала сотруднику в приёме терапевта, это правомерно?", POLY),
                    ("Сотрудник хочет сделать МРТ, входит ли это в программу?", POLY),
                    ("Можно ли получить онлайн-консультацию терапевта?", TELE),
                    ("Можно ли вызвать врача на дом?", HOME),
                    ("Положена ли плановая госпитализация?", HOSP)):
        cc, _run = _care(st, q)
        assert cc.value == "yes" and cc.name == want, (q, cc)


def test_only_narrow_lab_row_gives_not_found_with_honest_phrase():
    st = _kit([LAB])
    cc, run = _care(st, "Сотрудник хочет сделать МРТ, входит ли это в программу?")
    assert cc.value == "not_found"
    texts = [s.text for s in run.statements]
    assert not any("— входит в программу" in t for t in texts)
    assert any("но он не про" in t and "инструментальн" in t for t in texts)
    assert all("care_in_program" not in s.supports for s in run.statements)
    cc, _run = _care(st, "Можно ли сдать анализы по программе?")
    assert cc.value == "yes"


def test_only_telemed_row_for_in_person_question():
    st = _kit([TELE])
    cc, run = _care(st, "Можно ли сотруднику на приём к терапевту?")
    assert cc.value == "not_found"
    assert any("но он про" in s.text for s in run.statements)
    assert "{" not in text("care_misfit_mode", care="x", program="y", row_mode="a", question_mode="b")


# ---------------------------------------------------------------- 4. комплект (SKIP без data/raw)

def test_kit_c01_zetta_rostov_rows():
    kit_or_skip()
    st = kit_db_or_skip()
    prog = next((p for p in [m.program for m in st.care("c01")] if "Ростов" in p and "Ковид" in p), None)
    if prog is None:
        return
    ctx = ChatContext(contract_id="c01", program=prog)
    for q in ("Клиника «Медси» отказала сотруднику в приёме терапевта, это правомерно?",
              "Сотрудник хочет сделать МРТ в клинике «Медси», входит ли это в программу?"):
        slots = rule_slots(q, ctx, st)
        run = _Run(cid="c01", st=st, execute=bound_executor("c01", st))
        cc = check_care(run, prog, slots.get("care_type"), slots.get("service"), question=q)
        assert cc.name and "Телемедицин" not in cc.name and "лабораторн" not in cc.name, cc.name


def test_street_all_words_and_street_types():
    """Улица филиала — все слова названия, кроме типа улицы: «Красной Пресне» ≠ «Красногорск» (общее начало),
    «Ленинском проспекте» = «Ленинский пр-т» (демо-прогон К13, 26.10.2026)."""
    from dms_core.guided import on_street

    assert not on_street("Красной Пресне", "Московская область, Красногорск, Успенская, д.5")
    assert not on_street("Красной Пресне", "Москва, улица Краснобогатырская, д.90")
    assert on_street("Красной Пресне", "Москва, ул. Красная Пресня, д.16")
    assert on_street("Ленинском проспекте", "Москва, Ленинский пр-т, 5")
    assert on_street("Ильинском бульваре", "Красногорск, Ильинский бульвар, д.7")
    assert on_street("Плющева", "111398, Москва г, Плющева ул, дом № 15А")      # «пл» — сокращение, не начало слова
