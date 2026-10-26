"""Строгая личность клиники, раздел «за рубежом», отказ клиники (этап w5-strict): синтетика без сети.

1. clinic_in_program = yes / absent — только строго (tools/clinics.strict_name_match, confident_name): числа
   обязаны совпасть, слитное / раздельное / дефисное написание — одно, сокращение и одно описательное слово —
   не подтверждение; филиал по улице — по лемме улицы целиком.
2. Длинный заголовок раздела «вне территории РФ» без знака препинания в первых 200 знаках — подпункты и сам
   заголовок относятся к загранице (_Sections.abroad) и не участвуют в вопросах о помощи в России.
3. Отказ клиники / требование оплаты (маркеры config/route_markers.yaml refusal) — «можно» недопустимо: условие
   «клиника отказала — уточните причину».
Плюс синтетические аналоги ложных «можно» / «нельзя» ревизии 4 (названия клиник и тексты — выдуманные).
"""
from __future__ import annotations

from dms_core.adapters.base import text_hash
from dms_core.citations import verify
from dms_core.constants import Label, Scenario
from dms_core.guided import _Run, _Sections, bound_executor, coverage_hits, run_guided, service_terms, street_word_match
from dms_core.scenarios.route import refusal_marker, rule_slots
from dms_core.scenarios.sc01_refusal import decide_label
from dms_core.schemas import CareMatrix, ChatContext, Clause, ClinicRow
from dms_core.store import KitStore
from dms_core.tools.clinics import confident_name, distinct_overlap, name_score, strict_name_match
from fixtures.synth_kit import make_parsed

PROG = "Программа «Стандарт»"
CTX = ChatContext(group_id="gk9", contract_id="c01", program="Стандарт")
APC = "Амбулаторно-поликлиническая помощь"

# Выдуманный перечень клиник: (программа, вид помощи, название, город, адрес)
CLINICS = [
    ("Стандарт", APC, "ООО «БРИЗМЕДИКА»", "Москва", "Москва, Чистопрудный б-р, д. 7"),
    ("Стандарт", APC, "ФГБУ «52 КДЦ» Минтранса России", "Москва", "Москва, ул. Лесная, 3"),
    ("Премиум", APC, "ФГБУ «12 КДЦ» Минтранса России", "Москва", "Москва, ул. Полевая, 9"),
    ("Стандарт", APC, "«Лучистая поликлиника №3» ООО", "Москва", "Москва, ул. Садовая, 1"),
    ("Стандарт", APC, "ООО «МЦВ-3»", "Москва", "Москва, ул. Луговая, 2"),
    ("Стандарт", APC, "ЦКБ «Центральная клиническая больница Связьстроя»", "Москва", "Москва, ул. Речная, 5"),
    ("Стандарт", APC, "ГКБ № 77 им. Н.Н. Ветлугина", "Москва", "Москва, ул. Озёрная, 8"),
    ("Стандарт", APC, "ООО «Пилюля.РУ»", "Москва", "Москва, ул. Тихая, 4"),
    ("Стандарт", APC, "«Ласточка» АО", "Москва", "Москва, Краснопресненская наб., 12"),
    ("Стандарт", APC, "«Верба» ООО", "Москва", "Москва, ул. Новая, 6"),
    ("Стандарт", APC, "«Добрый лекарь» ООО", "Москва", "Москва, ул. Мирная, 10"),
    ("Премиум", APC, "МЦ ЗАРЕЧЬЕ (ОРБИТА МИР)", "Москва", "Москва, ул. Заречная, 1"),
    ("Стандарт", APC, "Клиника «Сота-1»", "Москва", "Москва, ул. Первая, 1"),
    ("Стандарт", APC, "Клиника «Сота-2»", "Москва", "Москва, ул. Вторая, 2"),
]

INCLUDED = [
    (None, f"{PROG} › Программа включает", "Программа включает:"),
    ("1.1", f"{PROG} › Программа включает › 1.1",
     "1.1. Приёмы врачей-специалистов: невролога, терапевта, эндокринолога, кардиолога, гинеколога в поликлинике."),
    ("1.2", f"{PROG} › Программа включает › 1.2",
     "1.2. Ультразвуковые исследования органов брюшной полости, электрокардиография."),
]
EXCLUSIONS = [
    (None, f"{PROG} › Исключения из программы", "Исключения из программы:"),
    ("4.1", f"{PROG} › Исключения из программы › 4.1",
     "4.1. Пластические операции с эстетической целью, косметология, гомеопатия."),
]


def _cl(n: int, number: str | None, path: str, text: str) -> Clause:
    return Clause(id=f"c01.program#r{n}", doc_id="c01.program", contract_id="c01", number=number, path=path,
                  program="Стандарт", text=text, text_hash=text_hash(text))


def _kit(rows: list[tuple[str | None, str, str]] | None = None, clinics=CLINICS) -> KitStore:
    p = make_parsed("c01")
    for i, (number, path, text) in enumerate(rows if rows is not None else INCLUDED + EXCLUSIONS):
        p.clauses.append(_cl(i, number, path, text))
    p.clinics = [
        ClinicRow(id=f"c01.clinics#s{i}", contract_id="c01", doc_id="c01.clinics", program=prog, care_type=ct,
                  clinic_name=name, city=city, address=addr, franchise_pct=None, source_ref=f"s{i}")
        for i, (prog, ct, name, city, addr) in enumerate(clinics)
    ]
    p.care = [CareMatrix(contract_id="c01", program="Стандарт", care_type="outpatient", present=True,
                         source_refs=["m1"])]
    st = KitStore(":memory:")
    st.save(p)
    return st


def _sc01(st: KitStore, question: str, slots: dict | None = None):
    """Вопрос → слоты правилами → guided → verify → метка (как движок без модели)."""
    s = slots if slots is not None else rule_slots(question, CTX, st)
    s = {**s, "program": "Стандарт"}
    run = run_guided("c01", question, scenario=Scenario.sc01, slots=dict(s), context=CTX, store=st)
    v = verify(run.draft, run.refs, st, "c01", program="Стандарт", tools=run.tools)
    label, _note, _rule = decide_label(v.draft.checks, has_alternative=bool(v.draft.alternatives))
    if not v.citations:
        label = Label.insufficient
    return s, v, label


def _clinic(question: str, clinic: str, street: str | None = None, service: str = "невролога") -> str:
    slots = {"clinic": clinic, "service": service, "care_type": APC}
    if street:
        slots["street"] = street
    _s, v, label = _sc01(_kit(), question, slots)
    value = v.draft.checks.clinic_in_program
    if value != "yes":
        assert label is not Label.allowed, (question, value)
    return value


# ---------------------------------------------------------------- 1. строгая личность клиники


def test_strict_numbers_must_match():
    assert not strict_name_match("52 КДЦ Минтранса", "12 КДЦ Минтранса России")
    assert not strict_name_match("12 КДЦ Минтранса", "ФГБУ «52 КДЦ» Минтранса России")
    assert strict_name_match("52 КДЦ Минтранса", "ФГБУ «52 КДЦ» Минтранса России")


def test_strict_number_alone_is_not_identity():
    # «№3» — единственное отличительное слово: без совпадения остальных слов — не та клиника
    assert not strict_name_match("Лучистой поликлинике №3", "ООО «МЦВ-3»")
    assert not strict_name_match("Поликлиника №3", "ООО «МЦВ-3»")
    assert strict_name_match("Лучистой поликлинике №3", "«Лучистая поликлиника №3» ООО")


def test_strict_joined_hyphen_separate_spelling():
    for q in ("Бриз-Медика", "Бриз Медика", "БРИЗМЕДИКА", "Бриз-Медике", "Бриз Медику"):
        assert strict_name_match(q, "ООО «БРИЗМЕДИКА»"), q
    assert strict_name_match("БРИЗМЕДИКЕ", "ООО «Бриз-Медика»")
    assert strict_name_match("Вега-Медицина", "ЧУЗ «Вега Медицина»")
    # склейка находит кандидата и для нечёткого подбора (clinics_query → name_score)
    row = ClinicRow(id="x", contract_id="c01", doc_id="d", program="Стандарт", care_type="c",
                    clinic_name="ООО «БРИЗМЕДИКА»", source_ref="x")
    assert name_score("Бриз-Медике", row) >= 85
    assert distinct_overlap("Бриз Медика", row)


def test_strict_acronym_is_only_candidate():
    # «ИМ» ↔ «им.» — совпадение по первым буквам: кандидат, но не подтверждение личности
    assert not strict_name_match("Инновационная Медицина", "ГКБ № 77 им. Н.Н. Ветлугина")
    assert not strict_name_match("БСМП", "Больница скорой медицинской помощи")


def test_strict_weak_word_alone_is_not_identity():
    assert not strict_name_match("клиника «Центральная»", "ЦКБ «Центральная клиническая больница Связьстроя»")
    assert not strict_name_match("Центральной", "Клиника Центральная")
    assert strict_name_match("Добром лекаре", "«Добрый лекарь» ООО")
    assert strict_name_match("Пилюля.ру", "ООО «Пилюля.РУ»")


def test_confident_name_for_absent():
    assert confident_name("Ромашка")
    assert confident_name("52 КДЦ")
    assert not confident_name("Инновационная Медицина")      # описательное + общее слово
    assert not confident_name("Центральная клиника")
    assert not confident_name("Томск", {"томск"})              # город
    assert not confident_name("Клиника")


def test_street_by_whole_lemma():
    assert street_word_match("Варшавке", "Варшавское")
    assert street_word_match("Покровке", "Покровка")
    assert street_word_match("Чистопрудном", "Чистопрудный")
    assert not street_word_match("Краснопресненской", "Красногорск")
    assert not street_word_match("Красногорской", "Краснопресненская")


def test_network_branch_by_street():
    assert _clinic("Невролог в Ласточке на Краснопресненской", "Ласточке", street="Краснопресненской") == "yes"
    assert _clinic("Невролог в Ласточке на Красногорской", "Ласточке", street="Красногорской") == "not_found"


def test_rows_with_different_numbers_are_unclear():
    # запрос без номера, а в перечне «Сота-1» и «Сота-2» — какая клиника, не ясно
    assert _clinic("Невролог в клинике Сота", "Сота") == "not_found"
    assert _clinic("Невролог в клинике Сота-2", "Сота-2") == "yes"


def test_absent_only_confident_and_without_similarity():
    assert _clinic("Невролог в клинике Ромашка", "Ромашка") == "absent"
    # похоже нестрого (склейка, сокращение, общее слово) — «не нашли», а не «нет в перечне»
    assert _clinic("Невролог в клинике Инновационная Медицина", "Инновационная Медицина") == "not_found"
    assert _clinic("Невролог в клинике «Центральная»", "Центральная") == "not_found"


# ---------------------------------------------------------------- аналоги ложных меток ревизии 4


def test_r4_005_006_weak_word_central_not_yes():
    assert _clinic("Положен ли приём невролога в клинике «Центральная»?", "Центральная") != "yes"
    assert _clinic("Положен ли приём невролога в Клинике Центральной?", "Клинике Центральной") != "yes"


def test_r4_013_number_52_not_12():
    # «52 КДЦ» есть в программе — да; «12 КДЦ» — только в другой программе: не «да» по чужому номеру
    assert _clinic("Положен ли приём невролога в 52 КДЦ Минтранса?", "52 КДЦ Минтранса") == "yes"


def test_r4_014_number_12_not_52():
    assert _clinic("Положен ли приём невролога в 12 КДЦ Минтранса?", "12 КДЦ Минтранса") != "yes"


def test_r4_036_joined_name_with_street():
    assert _clinic("Можно ли к неврологу в Бриз-Медику на Чистопрудном?", "Бриз-Медику",
                   street="Чистопрудном") == "yes"


def test_r4_132_278_joined_name_not_absent():
    assert _clinic("Положен ли приём невролога в Бриз-Медике?", "Бриз-Медике") == "yes"
    assert _clinic("Положен ли приём невролога в Бриз Медика?", "Бриз Медика") == "yes"


def test_r4_001_family_polyclinic_number():
    assert _clinic("Положен ли приём кардиолога в Лучистой поликлинике №3?", "Лучистой поликлинике №3",
                   service="кардиолога") == "yes"


def test_r4_x01_270_acronym_innovative_not_yes():
    assert _clinic("Положен ли приём невролога в клинике Инновационная Медицина?", "Инновационная Медицина") \
        == "not_found"


def test_r4_279_dot_ru():
    assert _clinic("Положен ли приём невролога в Пилюля.ру?", "Пилюля.ру") == "yes"


def test_r4_022_other_program_clinic_not_yes():
    assert _clinic("Можно ли к гинекологу в Орбита Мир в Заречье?", "Орбита Мир", service="гинеколога") != "yes"


TABLE = [
    ("7.1", f"{PROG} › Раздел VII › 7.1", "7.1. Перечень услуг программы:"),
    (None, f"{PROG} › Раздел VII › 7.1 › табл. 1, стр. 1",
     "первичные и повторные приемы специалистов | терапевт | да"),
    (None, f"{PROG} › Раздел VII › 7.1 › табл. 1, стр. 2",
     "первичные и повторные приемы специалистов | онколог | да | До установления диагноза"),
    *EXCLUSIONS,
]


def test_r4_195_table_comment_is_condition():
    st = _kit(TABLE)
    _s, v, label = _sc01(st, "Положен ли приём онколога в Вербе?", {"clinic": "Вербе", "service": "онколога",
                                                                    "care_type": APC})
    assert v.draft.checks.clinic_in_program == "yes"
    assert "other" in v.draft.checks.conditions
    assert label is not Label.allowed


# ---------------------------------------------------------------- 3. отказ клиники блокирует «можно»

REFUSALS = [
    "Не взяли по ДМС к неврологу в Вербе, сказали платите сами. Почему?",       # r4-249
    "Меня завернули с приёма невролога в Вербе, говорят не по страховке",     # r4-250
    "Регистратура Вербы требует доплату за приём невролога, правильно ли?",   # r4-251
    "В Вербе отказали в приёме невролога, сказали только платно",             # r4-268
    "В Вербе не берут по полису к неврологу",                                  # r4-269, r4-293
    "В Вербе невролог не ведёт приём по ДМС",
    "В Вербе сказали, что невролога по ДМС не взяли",
]


def test_refusal_markers_from_route_config():
    for q in REFUSALS:
        assert refusal_marker(q), q
    assert refusal_marker("Положен ли приём невролога в Вербе?") is None


def test_refusal_blocks_allowed_with_condition():
    st = _kit()
    for q in REFUSALS:
        _s, v, label = _sc01(st, q, {"clinic": "Вербе", "service": "невролога", "care_type": APC})
        ch = v.draft.checks
        assert label is not Label.allowed, q
        assert ch.clinic_in_program == "yes", q
        # условие «клиника отказала — уточните причину» держится ссылкой на строку перечня клиник
        if ch.care_in_program == "yes" and ch.exclusion == "absent":
            assert "other" in ch.conditions, q
            assert any("отказ" in s.text and "conditions.other" in s.supports for s in v.draft.statements), q


def test_no_refusal_no_blocking_condition():
    st = _kit()
    _s, v, _label = _sc01(st, "Положен ли приём невролога в Вербе?",
                          {"clinic": "Вербе", "service": "невролога", "care_type": APC})
    assert v.draft.checks.clinic_in_program == "yes"
    assert "other" not in v.draft.checks.conditions


# ---------------------------------------------------------------- 2. раздел о помощи за рубежом

LONG_HEAD = ("3.5. Медицинские и иные услуги предоставляемые вне территории Российской Федерации при состоянии "
             "Застрахованного лица требующего оказания экстренной или неотложной медицинской помощи для устранения "
             "угрозы жизни и или устранения острой боли в объеме согласованном Страховщиком.")
ABROAD = [
    *INCLUDED,
    ("3.5", f"{PROG} › Раздел III › 3.5", LONG_HEAD),
    ("3.5.1", f"{PROG} › Раздел III › 3.5.1", "3.5.1. Амбулаторно-поликлиническое обслуживание при острой боли:"),
    ("3.5.1.1", f"{PROG} › Раздел III › 3.5.1.1", "3.5.1.1. Приемы врачей по неврологии и кардиологии."),
    *EXCLUSIONS,
]


def test_abroad_long_heading_without_punctuation():
    assert "." not in LONG_HEAD[5:200] and ":" not in LONG_HEAD[:200]
    st = _kit(ABROAD)
    s = _Sections(st.clauses("c01"))
    got = {c.number: s.abroad(c) for c in st.clauses("c01") if c.number}
    assert got["3.5"] and got["3.5.1"] and got["3.5.1.1"]
    assert not got["1.1"] and not got["4.1"]
    run = _Run(cid="c01", st=st, execute=bound_executor("c01", st))
    rf = {h.clause.number for h in coverage_hits(run, "Стандарт", service_terms("приём невролога"))}
    assert rf and not any(n and n.startswith("3.5") for n in rf)
    ab = {h.clause.number for h in coverage_hits(run, "Стандарт", service_terms("приём невролога"), abroad=True)}
    assert ab and all(n and n.startswith("3.5") for n in ab)


def test_abroad_not_cited_in_russia_question():
    st = _kit(ABROAD)
    _s, v, _label = _sc01(st, "Положен ли приём невролога в Вербе?",
                          {"clinic": "Вербе", "service": "невролога", "care_type": APC})
    abroad_ids = {c.id for c in st.clauses("c01") if c.number and c.number.startswith("3.5")}
    refs = {c.ref for x in v.draft.statements for c in x.citations}
    assert refs and not refs & abroad_ids, refs
    assert not v.draft.contradictions


def test_abroad_count_on_kit_copy():
    """На базе комплекта (копия в DMS_DATA_DIR): все пункты 3.5.x программ ГК 3 (c09–c13) — abroad=True."""
    from dms_core.store import default_path
    from helpers import skip

    if not default_path().is_file():
        skip("нет базы разбора комплекта (data/derived/kit.sqlite)")
    st = KitStore(default_path())
    total = 0
    for cid in ("c09", "c10", "c11", "c12", "c13"):
        cls = st.clauses(cid)
        s = _Sections(cls)
        sub = [c for c in cls if c.program and c.number and (c.number == "3.5" or c.number.startswith("3.5."))]
        total += len(sub)
        assert sum(s.abroad(c) for c in sub) == len(sub), cid
    if not total:
        skip("в базе нет пунктов 3.5.x ГК 3")


def test_without_extra_payment_is_not_refusal():
    # «СК добавит клинику без доплаты» — не требование оплаты (эталон own-gk3-04 ушёл бы в SC 01 → «нельзя»)
    assert refusal_marker("СК ответила, что добавит клинику Верба без доплаты. Что дальше?") is None
    assert refusal_marker("Верба требует доплату за приём")
