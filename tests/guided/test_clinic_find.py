"""Этап w7-core (ревизия 6): клиника находится — слот не теряет номер и второе слово, строчные названия ищутся по
перечню договора, улица с цифрами, страховщик в названии клиники, выбор строки из поиска, ссылка «нет в перечне».
Синтетика, без сети."""
from __future__ import annotations

from dms_core import engine
from dms_core.citations import verify
from dms_core.constants import CLINIC_CHOICE_MAX, CLINIC_PICK_NONE, Label, Scenario
from dms_core.guided import on_street, run_guided
from dms_core.scenarios.route import extend_by_list, listed_clinic, listed_full_match, rule_slots
from dms_core.schemas import CareMatrix, ChatContext, Clause, ClinicRow
from dms_core.store import KitStore
from dms_core.tools.clinics import search_clinics
from fixtures.synth_kit import make_parsed
from guided.test_recall2 import EXCLUSIONS, INCLUDED, _cl
from helpers import label_as_engine, skip_unless_rule

CTX = ChatContext(group_id="gk9", contract_id="c01", program="Стандарт")
POLY = "Амбулаторно-поликлиническая помощь"
# (название, адрес, город) — программа «Стандарт», поликлиника
ROWS = [
    ("12 КДЦ Минобороны России ФГБУ", "Москва г, Академика Скрябина ул, д.3", "Москва"),        # s0
    ("52 КДЦ Минобороны России", "Москва г, Пироговская ул, д.5", "Москва"),                   # s1
    ("Доктор рядом (ООО «АКСИОМА-МЕД»)", "Москва, ул. Кожевническая, д.10", "Москва"),         # s2
    ("«Новая медицина-2000» ООО", "Орехово-Зуево г, Стадионная ул, д.2", "Орехово-Зуево"),     # s3
    ("ООО «Он Клиник Рязань»", "Рязань г, Чапаева ул, д.57", "Рязань"),                        # s4
    ("ООО «Медэкспресс»", "Рязань г, Семашко ул, д.3", "Рязань"),                               # s5
    ("ООО «ТОНУС ПРЕМИУМ»", "Нижний Новгород г, Воровского ул, д.3", "Нижний Новгород"),       # s6
    ("ООО «Ренессанс Здоровье»", "Санкт-Петербург, Кузнечный пер, д.2", "Санкт-Петербург"),    # s7
    ("АВСМЕДИЦИНА", "Москва г, 1905 года ул, д.7", "Москва"),                                  # s8
    ("АВСМЕДИЦИНА", "Химки г, 8 Марта ул, д.12", "Химки"),                                     # s9
    ("АВСМЕДИЦИНА", "Москва г, 3-я Парковая ул, д.41", "Москва"),                              # s10
    ("АВСМЕДИЦИНА", "Москва г, 5-я Парковая ул, д.1", "Москва"),                               # s11
    ("МЕДСИ", "Москва г, Тверская ул, д.1", "Москва"),                                         # s12
    ("Medsi Clinic Красная Пресня", "Москва г, Красная Пресня ул, д.16", "Москва"),            # s13
]
for _i in range(CLINIC_CHOICE_MAX + 3):                    # сеть больше CLINIC_CHOICE_MAX адресов
    ROWS.append(("АВСМЕДИЦИНА", f"Москва г, Ленина ул, д.{_i + 100}", "Москва"))


def _kit(*, listing_title: bool = False) -> KitStore:
    p = make_parsed("c01")
    for i, (number, path, text) in enumerate(INCLUDED + EXCLUSIONS):
        p.clauses.append(_cl(i, number, path, text))
    if listing_title:
        text = "Приложение 4. Список медицинских организаций."
        p.clauses.append(Clause(id="c01.contract#p900", doc_id="c01.contract", contract_id="c01", number=None,
                                path="Договор › 10.9 (абзац)", program=None, text=text, text_hash="h900"))
    p.clinics = [ClinicRow(id=f"c01.clinics#s{i}", contract_id="c01", doc_id="c01.clinics", program="Стандарт",
                           care_type=POLY, clinic_name=n, address=a, city=c, source_ref=f"s{i}")
                 for i, (n, a, c) in enumerate(ROWS)]
    p.care = [CareMatrix(contract_id="c01", program="Стандарт", care_type="outpatient", present=True,
                         source_refs=["m1"])]
    st = KitStore(":memory:")
    st.save(p)
    st.save(make_parsed("c02"))
    return st


ST = _kit()


def _slots(q: str) -> dict[str, str]:
    return rule_slots(q, CTX, ST)


# ---------------------------------------------------------------- слот: номер и второе слово


def test_slot_keeps_number_and_second_word():
    s = _slots("Можно ли к хирургу в 12 КДЦ на Скрябина?")
    assert s["clinic"] == "12 КДЦ" and s["street"] == "Скрябина"
    assert _slots("Положен ли приём невролога в 52 КДЦ Минобороны?")["clinic"] == "52 КДЦ Минобороны"
    assert _slots("Сотрудник хочет попасть к неврологу в Доктор рядом — это входит в ДМС?")["clinic"] == \
        "Доктор рядом"
    s = _slots("Положен ли приём невролога в Новая медицина-2000 на Стадионная?")
    assert s["clinic"] == "Новая медицина-2000" and s["street"] == "Стадионная"
    s = _slots("Положен ли приём эндокринолога в Он Клиник Рязань?")
    assert s["clinic"] == "Он Клиник Рязань" and "city" not in s


def test_extension_does_not_swallow_city_after_confident_name():
    # «МЕДСИ Москва»: уверенное название — город отдельно, даже если город есть в строке перечня
    toks = [("в", 0), ("Медэкспресс", 2), ("Рязань", 14)]
    from dms_core.scenarios.route import _list_index

    assert extend_by_list(toks, 1, 2, _list_index(CTX, ST), place=lambda k: k == 2) == (1, 2)
    # без уверенного слова («Он Клиник») город из той же строки перечня — часть названия
    toks = [("в", 0), ("Он", 2), ("Клиник", 5), ("Рязань", 12)]
    assert extend_by_list(toks, 1, 3, _list_index(CTX, ST), place=lambda k: k == 3) == (1, 4)


# ---------------------------------------------------------------- строчные названия по перечню


def test_lowercase_names_found_by_listing():
    assert _slots("Можно ли сотруднику на приём к урологу в медэкспресс?")["clinic"] == "медэкспресс"
    s = _slots("Можно ли сотруднику на приём к урологу в клинике тонус премиум в г. Нижний Новгород?")
    assert s["clinic"] == "тонус премиум" and "медэкспресс" not in s.get("service", "")
    assert listed_clinic("к урологу в тонус премиум", CTX, ST) == "тонус премиум"


def test_lowercase_finder_ignores_common_words_and_insurer():
    for q in ("Положен ли приём уролога в течение года после операции?",
              "Можно ли в поликлинике по месту жительства сделать приём невролога?",
              "Входит ли приём невролога в программу в Москве?",
              "В Ренессансе сказали, что приём невролога не входит — правы ли они?",
              "тонус премиум — это клиника?"):                    # без предлога «в / у / из / от» — не название
        assert listed_clinic(q, CTX, ST) is None, q
    assert "clinic" not in _slots("Положен ли приём уролога в течение года после операции?")


def test_document_titles_and_generic_quotes_are_not_clinics():
    # слот клиники ведёт к «выберите клинику» (clinic_unresolved) — название документа и «не по ДМС» им не становятся
    s = _slots("Совпадают ли условия помощи на дому в Программе и в Списке медицинских организаций?")
    assert "clinic" not in s
    assert "clinic" not in _slots("Сотруднику отказали в приёме невролога, сказали «не по ДМС». Обоснованно?")
    assert _slots("Положен ли приём невролога в клинике «Медэкспресс»?")["clinic"] == "Медэкспресс"
    # «он» — не служебное слово названия: «Он Клиник» без города остаётся клиникой
    assert _slots("Можно ли к неврологу в Он Клиник?")["clinic"] == "Он Клиник"


def test_insurer_word_in_clinic_name_kept_when_listed():
    assert listed_full_match("Ренессанс Здоровье", CTX, ST)
    assert not listed_full_match("Ренессанс", CTX, ST)
    q = "Покрывает ли полис консультацию невролога в клинике «Ренессанс Здоровье»?"
    a = engine.answer(q, CTX, store=ST)
    assert a.clinic_choices and a.clinic_choices[0].candidates[0].id == "c01.clinics#s7"
    assert listed_clinic("приём невролога в ренессанс здоровье", CTX, ST) == "ренессанс здоровье"


# ---------------------------------------------------------------- улица с цифрами


def test_street_with_numbers_slot_and_match():
    assert _slots("Положен ли приём невролога в АВСМЕДИЦИНА на 1905 года?")["street"] == "1905 года"
    assert _slots("Можно ли к неврологу в АВСМЕДИЦИНА на 8 Марта в г. Химки?")["street"] == "8 Марта"
    assert _slots("Положен ли приём невролога в АВСМЕДИЦИНА на 3-я Парковая?")["street"] == "3-я Парковая"
    assert on_street("1905 года", "Москва г, 1905 года ул, д.7")
    assert on_street("8 Марта", "Химки г, 8 Марта ул, д.12")
    assert on_street("3-я Парковая", "Москва г, 3-я Парковая ул, д.41")
    assert not on_street("3-я Парковая", "Москва г, 5-я Парковая ул, д.1")
    assert not on_street("1905 года", "Москва г, Ленина ул, д.100")


def test_candidates_on_named_street_first_in_big_network():
    a = engine.answer("Положен ли приём невролога в АВСМЕДИЦИНА на 3-я Парковая?", CTX, store=ST)
    cands = a.clinic_choices[0].candidates
    assert len(cands) == CLINIC_CHOICE_MAX and cands[0].id == "c01.clinics#s10"


# ---------------------------------------------------------------- выбор строки, найденной поиском


def test_pick_row_from_search_goes_first_in_candidates():
    q = "Положен ли приём невролога в АВСМЕДИЦИНА?"
    a0 = engine.answer(q, CTX, store=ST)
    ids = [r.id for r in a0.clinic_choices[0].candidates]
    far = next(r for r in search_clinics(ST.clinics("c01", program="Стандарт"), "ленина 108"))
    assert far.id not in ids                                   # строки нет среди кандидатов
    a = engine.answer(q, CTX, store=ST, clinic_picks={"c01": far.id})
    ch = a.clinic_choices[0]
    assert ch.selected == far.id and ch.candidates[0].id == far.id and len(ch.candidates) <= CLINIC_CHOICE_MAX
    stmts = [s for s in a.statements if any(c.table_ref == "s" + far.id.split("#s")[1] for c in s.citations)]
    assert stmts                                               # утверждение «клиника в перечне» по этой строке


# ---------------------------------------------------------------- поиск по перечню


def test_search_by_name_address_digits_latin_prefix():
    rows = ST.clinics("c01", program="Стандарт")
    assert search_clinics(rows, "") == rows and search_clinics(rows, None) == rows
    ids = [r.id for r in search_clinics(rows, "авсмедицина 1905")]
    assert ids == ["c01.clinics#s8"]
    assert [r.id for r in search_clinics(rows, "8 марта")] == ["c01.clinics#s9"]
    assert [r.id for r in search_clinics(rows, "парковая 3")] == ["c01.clinics#s10"]
    assert [r.id for r in search_clinics(rows, "химки")] == ["c01.clinics#s9"]          # город
    assert {r.id for r in search_clinics(rows, "medsi")} == {"c01.clinics#s12", "c01.clinics#s13"}   # латиница
    assert [r.id for r in search_clinics(rows, "медэксп")] == ["c01.clinics#s5"]        # начало слова
    assert [r.id for r in search_clinics(rows, "аксиома")] == ["c01.clinics#s2"]        # юрлицо в скобках
    assert search_clinics(rows, "несуществующая") == []
    # слово названия раньше слова адреса: «пресня» — в названии s13, в адресе тоже — одна строка
    assert [r.id for r in search_clinics(rows, "пресня")] == ["c01.clinics#s13"]


# ---------------------------------------------------------------- ссылка «нет в перечне»


def _absent_backer(st: KitStore, q: str):
    slots = {**rule_slots(q, CTX, st), "program": "Стандарт", "clinic_pick": CLINIC_PICK_NONE}
    run = run_guided("c01", q, scenario=Scenario.sc01, slots=slots, context=CTX, store=st)
    v = verify(run.draft, run.refs, st, "c01", program="Стандарт", tools=run.tools)
    backer = [s for s in v.draft.statements if "clinic_in_program" in s.supports]
    return v, backer, label_as_engine(v, CLINIC_PICK_NONE)[0]


def test_absent_citation_is_listing_title_not_foreign_clinic():
    skip_unless_rule("clinic_absent")
    q = "Прием невролога в клинике «Несуществующая Альфа», по ДМС пойдет?"
    v, backer, label = _absent_backer(_kit(listing_title=True), q)
    assert label is Label.denied and v.draft.checks.clinic_in_program == "absent"
    assert backer and backer[0].citations[0].ref == "c01.contract#p900"
    assert "Список медицинских организаций" in backer[0].citations[0].quote
    # пункта-названия перечня нет — ссылка на строку перечня программы (как раньше), verify её пропускает
    v, backer, label = _absent_backer(ST, q)
    assert label is Label.denied and backer and backer[0].citations[0].ref.startswith("c01.clinics#")
