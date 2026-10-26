"""Этап w7-fix (ревизия 7): слово названия клиники не уходит в услугу, исключение — по лемме слова услуги,
«тип учреждения + номер» и названия из общих слов — слот по перечню, ложные слоты без клиники, улица филиала,
ссылка «нет в перечне» без строки чужой клиники, несклоняемое название клиники после «в». Синтетика, без сети;
названия и адреса — выдуманные или общего вида."""
from __future__ import annotations

from dms_core import engine
from dms_core.adapters.base import text_hash
from dms_core.citations import list_table_ref, verify
from dms_core.constants import CLINIC_PICK_NONE, Label, Scenario
from dms_core.guided import exclusion_hit, run_guided, same_word, service_terms
from dms_core.scenarios.route import rule_slots, street_after
from dms_core.schemas import CareMatrix, ChatContext, Clause, ClinicRow
from dms_core.store import KitStore
from fixtures.synth_kit import make_parsed
from guided.test_recall2 import INCLUDED, PROG
from helpers import label_as_engine, skip_unless_rule

CTX = ChatContext(group_id="gk9", contract_id="c01", program="Стандарт")
POLY = "Амбулаторно-поликлиническая помощь"
DENT = "Стоматологическая помощь"
# (название, адрес, город[, вид помощи])
ROWS = [
    ('"ЗДОРОВЬЕ" СЕМЕЙНЫЙ МЕДИЦИНСКИЙ ЦЕНТР" ООО', "Лобня г, Ленина ул, дом № 23", "Лобня"),
    ('"Поликлиника №1" ФГБУ', "Москва г, Сивцев Вражек пер, дом № 26", "Москва"),
    ('"СЕМЕЙНАЯ ПОЛИКЛИНИКА №1" ООО', "Сергиев Посад г, Новоугличское ш, дом № 3", "Сергиев Посад"),
    ('"Медицинские Центры - 2" ООО (ИММА)', "Москва г, Перерва ул, дом № 39", "Москва"),
    ('"МЕДИЦИНСКИЕ ЦЕНТРЫ-7" ООО (ИММА)', "Москва г, Липовый парк ул, дом № 4", "Москва"),
    ('"12 КДЦ"МИНОБОРОНЫ РОССИИ ФГБУ', "Москва г, 1-й Краснокурсантский проезд, дом № 1", "Москва"),
    ('"МСЧ № 152 ФМБА России" ФГБУЗ', "Москва г, Лесная ул, дом № 1", "Москва"),
    ('"Семейный доктор" АО', "Москва г, Дежнёва проезд, дом № 1", "Москва"),
    ('"Клиника Будь Здоров" ООО (Будь здоров)', "Москва г, Рочдельская ул, дом № 15", "Москва"),
    ('"Медицинский центр "Поликлиника.ру" ООО (Поликлиника.РУ)', "г. Москва, проезд Кожуховский 1-й, дом 9",
     "Москва"),
    ('Medikus (ООО "МЕДАВТО")', "Тюмень, ул. Пржевальского, д.35", "Тюмень"),
    ("КДЦ МЕДСИ на Белорусской", "Москва, Грузинский переулок, д.3А", "Москва"),
    ('"Доктор рядом" ООО', "Москва, ул. Кулакова, д.20", "Москва"),
    ('"Доктор рядом" ООО', "Москва, ул. Полянка, д.1", "Москва"),
    ("МЕДСИ", "Москва г, 1905 года ул, д.7", "Москва"),
    ("МЕДСИ", "Москва г, 1905 года ул, д.7", "Москва", DENT),          # та же клиника, тот же адрес
    ('"Медицинские услуги" АО', "Москва г, 2-я Фрунзенская ул, дом № 9", "Москва"),
    ("Клиника на Мытищинской", "Москва г, 3-я Мытищинская ул, д.5", "Москва"),
    ("СМ-Клиника", "Москва г, проспект Мира, д.10", "Москва"),
    ('"Микрохирургия глаза" ФГАУ', "Москва г, Бескудниковский б-р, д.59", "Москва"),
    ('"Московский государственный университет" (клиника)', "Москва г, Ломоносовский пр-т, д.27", "Москва"),
    ('СПб ГБУЗ "Женская консультация №22"', "Санкт-Петербург, Невский пр., д.1", "Санкт-Петербург"),
    ('"Клиника Ренессанс" ООО', "Воронеж г, Здоровья пер, д.90", "Воронеж"),
]
# исключения программы: диспансерное наблюдение и «расстройство здоровья» (общий вид пунктов ДМС)
EXCL = [
    (None, f"{PROG} › Исключения из программы", "Исключения из программы:"),
    ("4.1", f"{PROG} › Исключения из программы › 4.1",
     "4.1. Диспансерное наблюдение за состоянием здоровья лиц, страдающих хроническими заболеваниями."),
    ("4.2", f"{PROG} › Исключения из программы › 4.2",
     ("4.2. Лечение в связи с травматическим повреждением или иным расстройством здоровья, наступившим в результате "
     "умышленных противоправных действий Застрахованного.")),
]


def _cl(n: int, number: str | None, path: str, text: str, doc: str = "program") -> Clause:
    return Clause(id=f"c01.{doc}#r{n}", doc_id=f"c01.{doc}", contract_id="c01", number=number, path=path,
                  program="Стандарт", text=text, text_hash=text_hash(text))


def _kit(*, table: str = "program") -> KitStore:
    """table: program — перечень таблицей программы без строки-заголовка (вид ГК 2: «c01.program#t6r2…»);
    contract — таблицей договора со строкой-заголовком программы «Вариант № 1 Стандарт» (вид ГК 1)."""
    p = make_parsed("c01")
    for i, (number, path, text) in enumerate(INCLUDED + EXCL):
        p.clauses.append(_cl(i, number, path, text))
    doc = f"c01.{table}"
    if table == "contract":
        text = "Вариант № 1 Стандарт: перечень медицинских учреждений программы"
        p.clauses.append(Clause(id=f"{doc}#t6r1", doc_id=doc, contract_id="c01", number=None,
                                path="Договор › Таблица 6", program="Стандарт", text=text, text_hash=text_hash(text)))
    p.clinics = [ClinicRow(id=f"{doc}#t6r{i + 2}", contract_id="c01", doc_id=doc, program="Стандарт",
                           care_type=r[3] if len(r) > 3 else POLY, clinic_name=r[0], address=r[1], city=r[2],
                           source_ref=f"{doc}#t6r{i + 2}")
                 for i, r in enumerate(ROWS)]
    p.care = [CareMatrix(contract_id="c01", program="Стандарт", care_type="outpatient", present=True,
                         source_refs=["m1"])]
    st = KitStore(":memory:")
    st.save(p)
    st.save(make_parsed("c02"))
    return st


ST = _kit()


def _slots(q: str) -> dict[str, str]:
    return rule_slots(q, CTX, ST)


# ---------------------------------------------------------------- п. 1: слово названия — не услуга


def test_generic_name_after_preposition_is_clinic_not_service():
    s = _slots("можно ли к кардиологу в здоровье в лобне")
    assert s.get("clinic") == "здоровье" and "здоров" not in s.get("service", "")
    a = engine.answer("можно ли к кардиологу в здоровье в лобне", CTX, store=ST)
    assert a.label is not Label.denied                        # не исключение «расстройство здоровья»
    assert a.clinic_choices and a.clinic_choices[0].candidates[0].clinic_name.startswith('"ЗДОРОВЬЕ"')


# ---------------------------------------------------------------- п. 2: исключение — по лемме, не по основе


def test_exclusion_matches_lemma_not_shared_stem():
    assert not same_word("диспансеризация", "диспансерное")
    assert same_word("косметолог", "косметологические") and same_word("стоматология", "стоматологической")
    seg = "Диспансерное наблюдение за состоянием здоровья лиц, страдающих хроническими заболеваниями."
    assert not exclusion_hit(service_terms("диспансеризация"), seg)
    assert exclusion_hit(service_terms("диспансерное наблюдение"), seg)
    a = engine.answer("можно ли пройти диспансеризацию в поликлинике по месту жительства", CTX, store=ST)
    assert a.label is not Label.denied


# ---------------------------------------------------------------- п. 3: тип учреждения + номер, названия целиком


def test_numbered_and_generic_names_become_slot():
    cases = {
        "Можно ли к неврологу в Поликлинику №1 на Сивцевом Вражке?": "Поликлинику №1",
        "Можно ли к офтальмологу в поликлиника №1 по адресу улица Сивцев Вражек": "поликлиника №1",
        "Положен ли приём кардиолога в семейная поликлиника №1 на Новоугличском шоссе?": "семейная поликлиника №1",
        "Можно ли к травматологу в медицинские центры - 2 на улице Перерва": "медицинские центры - 2",
        "Можно ли к гастроэнтерологу в медицинские центры-7?": "медицинские центры-7",
        "положен ли приём невролога в 12 кдц на 1-м Краснокурсантском?": "12 кдц",
        "Можно ли по ДМС к кардиологу в мсч № 152 фмба россии на улице Лесная?": "мсч № 152 фмба россии",
        "можно ли к неврологу в семейный доктор на Дежнёва": "семейный доктор",
        "клиника будь здоров на рочдельской — к урологу можно?": "будь здоров",
        "Можно ли к терапевту в Поликлиника ру на Кожуховском проезде?": "Поликлиника ру",
        "Можно ли к неврологу в медикус?": "медикус",
        "кдц медси на белорусской — можно к кардиологу?": "кдц медси на белорусской",
        "Можно ли к офтальмологу в Медицинские услуги на 2-й Фрунзенской?": "Медицинские услуги",
    }
    for q, want in cases.items():
        s = _slots(q)
        assert s.get("clinic") == want, (q, s)
        assert not any(w in s.get("service", "").casefold() for w in ("поликлиник", "улиц", "услуг")), (q, s)
    # номер в слоте: «Медицинские центры-9» — такого номера в перечне нет, но это название (решит «нет в перечне»)
    assert _slots("Можно ли к неврологу в Медицинские центры-9?").get("clinic") == "Медицинские центры-9"
    # мера после числа — не номер учреждения
    assert "clinic" not in _slots("Можно ли в поликлинике 2 раза в год сдать анализы?")


def test_latin_row_found_by_cyrillic_name():
    a = engine.answer("Можно ли к неврологу в медикус?", CTX, store=ST)
    assert a.clinic_choices and a.clinic_choices[0].candidates[0].clinic_name.startswith("Medikus")


# ---------------------------------------------------------------- п. 4: ложные слоты без клиники


def test_no_clinic_questions_have_no_clinic_slot():
    for q in ("Можно ли к неврологу в московской клинике?", "Можно ли к неврологу в клинике рядом с работой?",
              "Можно ли сделать УЗИ в женской консультации?", "можно ли к офтальмологу в клинике здоровья глаз",
              "можно ли к терапевту в поликлинике у дома?"):
        assert "clinic" not in _slots(q), q
    # улица — не клиника: «на переулке Здоровья»
    assert _slots("можно ли к урологу в ренессанс на переулке Здоровья").get("clinic") == "ренессанс"


# ---------------------------------------------------------------- п. 6: улица филиала, улица — не город


def test_street_forms_and_street_is_not_city():
    def street(q: str, after: str) -> str | None:
        got = street_after(q, q.find(after) + len(after))
        return got[0] if got else None

    assert street("в Доктор рядом на улице Кулакова", "рядом") == "Кулакова"
    assert street("в Доктор рядом на улице кулакова", "рядом") == "кулакова"
    assert street("в 12 КДЦ в 1-м Краснокурсантском проезде", "КДЦ") == "1-м Краснокурсантском"
    assert street("в АВСМЕДИЦИНА на 8 марта в Химках", "АВСМЕДИЦИНА") == "8 марта"
    assert street("в поликлиника №1 по адресу улица Плющиха", "№1") == "Плющиха"
    assert street("в Медси в Лобне", "Медси") is None                 # «в» без типа улицы — город
    s = _slots("Можно ли к неврологу в Поликлинику №1 на Сивцевом Вражке?")
    assert s.get("street") == "Сивцевом Вражке" and "city" not in s
    s = _slots("Можно ли к неврологу в Поликлинику №1 на Плющихе?")
    assert s.get("city") != "Плющиха"


# ---------------------------------------------------------------- п. 7: ссылка «нет в перечне»


def _absent(st: KitStore, q: str):
    slots = {**rule_slots(q, CTX, st), "program": "Стандарт", "clinic_pick": CLINIC_PICK_NONE}
    run = run_guided("c01", q, scenario=Scenario.sc01, slots=slots, context=CTX, store=st)
    v = verify(run.draft, run.refs, st, "c01", program="Стандарт", tools=run.tools)
    backer = [s for s in v.draft.statements if "clinic_in_program" in s.supports]
    return v, backer, label_as_engine(v, CLINIC_PICK_NONE)[0]


def test_absent_citation_is_list_table_not_foreign_row():
    skip_unless_rule("clinic_absent")
    q = "Прием невролога в клинике «Несуществующая Альфа», по ДМС пойдет?"
    names = {r[0] for r in ROWS}
    # перечень — таблица программы без строки-заголовка: ссылка на таблицу, цитата — программа таблицы
    v, backer, label = _absent(ST, q)
    assert label is Label.denied and v.draft.checks.clinic_in_program == "absent"
    cit = backer[0].citations[0]
    assert cit.ref == "c01.program#t6" and cit.quote == "Стандарт" and cit.quote not in names
    assert v.citations["c01.program#t6"].table_ref == "c01.program#t6"
    # перечень — таблица договора со строкой-заголовком программы: ссылка на эту строку-пункт
    v, backer, label = _absent(_kit(table="contract"), q)
    assert label is Label.denied
    assert backer[0].citations[0].ref == "c01.contract#t6r1" and "Вариант № 1" in backer[0].citations[0].quote


def test_list_table_ref_verified_as_document_fragment():
    assert list_table_ref("c05.program#t6r2") == "c05.program#t6"
    assert list_table_ref("c01.contract#t11r202#v2") == "c01.contract#t11"
    assert list_table_ref("c09.clinics#Список медицинских организаций!10") == "c09.clinics#Список медицинских организаций"
    assert list_table_ref("c01.clinics#s3") is None
    from dms_core.schemas import DraftAnswer, DraftCitation, DraftStatement

    stmt = "Клиники «Альфа» нет в перечне программы «Стандарт»."

    def draft(quote: str) -> DraftAnswer:
        return DraftAnswer(contract_id="c01", summary="", statements=[DraftStatement(
            text=stmt, citations=[DraftCitation(ref="c01.program#t6", quote=quote)])])

    row = ST.clinics("c01", program="Стандарт")[0]
    ok = verify(draft("Стандарт"), {row.id}, ST, "c01", program="Стандарт")
    assert ok.draft.statements and ok.citations["c01.program#t6"].quote == "Стандарт"
    # цитата не заголовок таблицы — отброс; строк таблицы не было в прогоне — отброс
    assert not verify(draft(ROWS[3][0]), {row.id}, ST, "c01", program="Стандарт").draft.statements
    assert not verify(draft("Стандарт"), set(), ST, "c01", program="Стандарт").draft.statements


# ---------------------------------------------------------------- п. 8: несклоняемое название после «в»


def test_uninflected_place_name_after_v_is_clinic():
    s = _slots("Можно ли сотруднику на приём к кардиологу в Верона?")
    assert s.get("clinic") == "Верона" and s.get("city") != "Верона"
    s = _slots("Можно ли к кардиологу в Москве?")
    assert "clinic" not in s
