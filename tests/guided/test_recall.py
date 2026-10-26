"""Полнота SC 01 без ложных «можно» / «нельзя» (этап w5-recall): синтетика на каждое правило, без сети.

2. Сокращения («ПСА», «ЭКГ», «ПЭТ-КТ») — как есть, не леммы pymorphy («ПСА» ≠ «пёс»).
3. Услуга без главного слова (одни уточнения, одна анатомия, одно прилагательное) — «не нашли», а не совпадение.
4. Помощь за рубежом (ГК 3: п. 3.5 и 4.3 «…услуг, предусмотренных п. 3.5») — только к вопросу о загранице;
   «исключения просмотрены» — не по Памятке и не по пункту-отсылке.
5. Условия в строках таблиц и таблиц лимитов; вид действия (лечение ≠ реабилитация).
6. Словари: синонимы (прививка ↔ вакцинация), анатомия без общих слов, разбор «вен» → «вена».
7. Отрицание «не представляется возможной» внутри «входит». Плюс: оговорка исключения — только своего пункта
   перечня; лимит обращений исчерпан («шестой приём» при «не более 5 раз») → «нельзя» со ссылкой на лимит.
"""
from __future__ import annotations

from dms_core.adapters.base import text_hash
from dms_core.constants import DocType, Label
from dms_core.guided import (
    _negated,
    _Run,
    _Sections,
    asked_count,
    bound_executor,
    caution_scope,
    check_coverage,
    condition_kinds,
    coverage_cfg,
    coverage_hits,
    question_abroad,
    reference_only,
    service_actions,
    service_terms,
    terms_hit,
)
from dms_core.schemas import Clause, Document
from dms_core.store import KitStore
from fixtures.synth_kit import NOW, make_parsed
from guided.test_coverage import PROG, _sc01, _store, _supported
from helpers import skip_unless_limit_deny

# ---------------------------------------------------------------- 2. сокращения


def test_abbreviation_kept_as_is_not_lemmatized():
    t = service_terms("Анализ на ПСА")
    assert t and "пса" in t[0].required and "пес" not in t[0].required
    assert any(terms_hit(x, "лабораторная диагностика: ПСА общий, ПСА свободный") for x in t) is False  # «анализ»
    ekg = service_terms("ЭКГ")
    assert ekg[0].required == ("экг",)
    assert any(terms_hit(x, "Электрокардиография (ЭКГ) в покое") for x in ekg)
    pet = service_terms("ПЭТ-КТ")
    assert any(terms_hit(x, "лабораторная и инструментальная диагностика | ПЭТ/КТ; ПЭТ/МРТ | да") for x in pet)


# ---------------------------------------------------------------- 3. главное слово услуги


def test_service_without_head_word_is_not_searched():
    # одни уточнения («после удаления»), одна анатомия, прилагательное без существительного — «не нашли»
    assert service_terms("зубов после удаления") == []
    assert service_terms("колена") == []
    t = service_terms("планового терапевта")
    assert t and set(t[0].required) == {"планов", "терапе"}            # специалист вернулся в обязательные
    t = service_terms("имплантация зубов после удаления")
    assert t and t[0].required[0] == "имплан" and "зуб" in t[0].optional
    # специалист, назначивший услугу, — необязательное слово
    t = service_terms("Гастроэнтеролог капсульную эндоскопию")
    assert "гастроэнтероло" in t[0].optional and "капсул" in t[0].required


# ---------------------------------------------------------------- 4. заграница, Памятка, отсылка

ABROAD = [
    ("III", f"{PROG} › Раздел III", "III. ОБЪЕМ МЕДИЦИНСКИХ УСЛУГ"),
    ("3.1", f"{PROG} › Раздел III › 3.1", "3.1. Амбулаторно-поликлиническая помощь:"),
    ("3.1.1", f"{PROG} › Раздел III › 3.1.1", "3.1.1. Иммунопрофилактика клещевого энцефалита по показаниям."),
    ("3.5", f"{PROG} › Раздел III › 3.5",
     ("3.5. Медицинские и иные услуги, предоставляемые вне территории Российской Федерации при состоянии, "
      "требующем экстренной помощи")),
    ("3.5.1", f"{PROG} › Раздел III › 3.5.1", "3.5.1. Лечение кариеса зубов в экстренных случаях."),
    ("IV", f"{PROG} › Раздел IV", "IV. ИСКЛЮЧЕНИЯ ИЗ ПРОГРАММЫ:"),
    ("4.2", f"{PROG} › Раздел IV › 4.2",
     "4.2. Медицинские услуги, обращение за которыми не является страховым случаем и не оплачивается:"),
    ("4.2.1", f"{PROG} › Раздел IV › 4.2.1", "4.2.1. Услуги косметолога и пластического хирурга."),
    ("4.3", f"{PROG} › Раздел IV › 4.3",
     ("4.3. В отношении медицинских услуг, предусмотренных п. 3.5 настоящей Программы, не являются страховым "
      "случаем и не оплачиваются:")),
    ("4.3.1", f"{PROG} › Раздел IV › 4.3.1", "4.3.1. Профилактические мероприятия; иммунопрофилактика."),
]


def test_abroad_section_applies_only_to_abroad_question():
    st = _store(ABROAD)
    s = _Sections(st.clauses("c01"))
    by_num = {c.number: c for c in st.clauses("c01") if c.number}
    assert s.abroad(by_num["3.5.1"]) and s.abroad(by_num["4.3.1"])           # предок 3.5 и ссылка «п. 3.5»
    assert not s.abroad(by_num["3.1.1"]) and not s.abroad(by_num["4.2.1"])
    # вопрос о помощи в России: исключение 4.3.1 (только для п. 3.5) не даёт «нельзя»
    _run, v, label, _ = _sc01(st, {"service": "иммунопрофилактика клещевого энцефалита"})
    assert v.draft.checks.exclusion != "yes" and label is not Label.denied
    assert v.draft.checks.care_in_program == "yes"                             # п. 3.1.1
    # вопрос о загранице: «входит» из п. 3.1 (помощь в России) не берётся
    assert question_abroad("Сотрудник за границей — покроют лечение кариеса?")
    run = _Run(cid="c01", st=st, execute=bound_executor("c01", st))
    hits = coverage_hits(run, "Стандарт", service_terms("иммунопрофилактика"), abroad=True)
    assert {h.clause.number for h in hits} == {"4.3.1"}
    assert not question_abroad("Сотрудник в командировке в Казани заболел")


def test_exclusions_seen_not_by_memo_or_reference_clause():
    # исключения есть только в Памятке и пункте-отсылке программы — «исключения просмотрены» не ставится
    p = make_parsed("c01")
    p.documents.append(Document(id="c01.memo", contract_id="c01", group_id="gk9", title="memo", doc_type=DocType.memo,
                                source_path="synth/c01/memo.docx", original_format="docx", sha256="0" * 64,
                                uploaded_at=NOW))
    rows = [
        ("c01.program", None, f"{PROG} › Программа включает", "Программа включает:"),
        ("c01.program", "1.1", f"{PROG} › Программа включает › 1.1",
         "1.1. Ультразвуковое исследование органов брюшной полости по направлению лечащего врача."),
        ("c01.program", "5", f"{PROG} › Исключения из программы",
         "Исключения из программы указаны в п. 4 Правил страхования."),
        ("c01.memo", None, "Памятка › Исключения из программы", "Исключения из программы:"),
        ("c01.memo", None, "Памятка › Исключения из программы", "Косметология, пластическая хирургия, массаж."),
    ]
    for i, (doc, number, path, text) in enumerate(rows):
        p.clauses.append(Clause(id=f"{doc}#y{i}", doc_id=doc, contract_id="c01", number=number, path=path,
                                program="Стандарт", text=text, text_hash=text_hash(text)))
    st = KitStore(":memory:")
    st.save(p)
    assert reference_only(p.clauses[-3])
    run = _Run(cid="c01", st=st, execute=bound_executor("c01", st))
    cov = check_coverage(run, "Стандарт", "УЗИ брюшной полости", "not_found")
    assert cov.care == "yes" and cov.exclusion == "not_found"


# ---------------------------------------------------------------- 5. таблицы и вид действия


def test_table_cell_conditions_and_limits_table():
    assert "other" in condition_kinds("маммография | да | При наличии медицинских показаний")
    assert "other" in condition_kinds("помощь | да | в случае поездки в регион обслуживания")
    assert "referral" in condition_kinds("ПЭТ | да | Однократно по направлению Страховщика")
    rows = [
        ("III", f"{PROG} › Раздел III", "III. ОБЪЕМ МЕДИЦИНСКИХ УСЛУГ"),
        ("3.1", f"{PROG} › Раздел III › 3.1", "3.1. Программа включает:"),
        (None, f"{PROG} › Раздел III › 3.1 › табл. 2, стр. 1",
         "Инструментальные исследования: Маммография | Ограничение объема услуг: При наличии медицинских показаний"),
    ]
    st = _store(rows)
    s = _Sections(st.clauses("c01"))
    row = next(c for c in st.clauses("c01") if "табл." in c.path)
    assert s.section(row)[0] == "included"                        # без ячейки «да / нет» — по заголовкам
    run = _Run(cid="c01", st=st, execute=bound_executor("c01", st))
    cov = check_coverage(run, "Стандарт", "Маммография", "not_found")
    assert cov.care == "yes" and "other" in cov.conditions


def test_action_kind_must_match():
    assert service_actions("Реабилитация после ковида") == ("rehabilitation",)
    assert service_actions("Лечение ковида") == ("treatment",)
    treat = service_terms("лечение ковида")
    rehab_row = "РАСШИРЕНИЯ | Реабилитация после перенесенной COVID-19 | да"
    assert not any(terms_hit(t, rehab_row) for t in treat)
    assert any(terms_hit(t, rehab_row) for t in service_terms("реабилитация после ковида"))
    # в исключениях несовпадение вида действия — упоминание (исключения «просмотренными» не считаются)
    rows = [(None, f"{PROG} › Исключения из программы", "Исключения из программы:"),
            ("2", f"{PROG} › Исключения из программы › 2", "2. Реабилитация после перенесенной COVID-19.")]
    st = _store(rows)
    run = _Run(cid="c01", st=st, execute=bound_executor("c01", st))
    hits = coverage_hits(run, "Стандарт", treat)
    assert [h.section for h in hits] == ["exclusion_mention"]


# ---------------------------------------------------------------- 6. словари


def test_dictionaries_synonyms_and_anatomy():
    cfg = coverage_cfg()
    assert not {"отдел", "малый", "полость"} & cfg.context
    assert {"сердце", "вена", "кожа"} <= cfg.anatomy
    vac = service_terms("прививка от гриппа")
    assert any(terms_hit(t, "Вакцинация против гриппа в офисе Страхователя") for t in vac)
    assert any(terms_hit(t, "Иммунопрофилактика гриппа сезонная") for t in vac)
    t = service_terms("склеротерапия вен")                         # «вен» → «вена» (анатомия), а не «вено»
    assert t[0].required == ("склеро",) and t[0].optional


# ---------------------------------------------------------------- 7. отрицание и оговорки


def test_negation_inside_included_and_caution_of_own_item():
    assert _negated("Оказание помощи на дому в данном районе не представляется возможной.")
    assert _negated("Услуга в выходные дни не оказывается.")
    svc = service_terms("гирудотерапия")
    seg = ("биорезонансную терапию, аутогемотерапию, гирудотерапию, лечение методом ударно-волновой терапии "
           "(за исключением случаев, предусмотренных программой)")
    scope = caution_scope(seg, svc)
    assert "за исключением" not in scope and "гирудотерапию" in scope
    # оговорка в своём пункте перечня — остаётся
    own = caution_scope("гирудотерапию (кроме случаев по назначению врача), аутогемотерапию", svc)
    assert "кроме" in own
    # незакрытая скобка в конце фрагмента («…согласно п.» — фрагмент обрезан по точке) — тоже чужая
    assert "кроме" not in caution_scope("гирудотерапию, диетологии(кроме обращений согласно п.", svc)


def test_limit_exceeded_with_same_service_unit_and_period():
    skip_unless_limit_deny()
    # w5-recall2: лимит сравнивается при совпадении услуги, единицы и периода — тогда «нельзя» со ссылкой на лимит
    assert asked_count("уже 5 раз была у психотерапевта, на шестой приём не записывают") == 6
    assert asked_count("Пятый сеанс массажа") == 5
    assert asked_count("во вторник на приём не записали") is None
    rows = [(None, f"{PROG} › Программа включает", "Программа включает:"),
            ("1.1", f"{PROG} › Программа включает › 1.1",
             "1.1. Консультации психотерапевта не более 5 раз в год по направлению врача.")]
    st = _store(rows)
    q = "Застрахованная уже 5 раз была у психотерапевта, на шестой приём не записывают. Правы?"
    _run, v, label, _rule = _sc01(st, {"service": "психотерапевта"}, question=q)
    assert v.draft.checks.exclusion == "yes" and label is Label.denied
    assert any("не более 5 раз" in c.quote for st in _supported(v, "exclusion") for c in st.citations)
    # в пределах лимита — не «нельзя»
    _run, v2, label2, _ = _sc01(st, {"service": "психотерапевта"}, question="Четвёртый приём психотерапевта — по ДМС?")
    assert v2.draft.checks.exclusion != "yes" and label2 is not Label.denied


def test_reduced_service_needs_a_noun():
    # «плановую госпитализацию» без слова процедуры — одно прилагательное: оговорки о «плановой санации» не берутся
    t = service_terms("плановую госпитализацию")
    assert all(x.reduced() is None for x in t)
    rows = [(None, f"{PROG} › Исключения из программы", "Исключения из программы:"),
            ("7", f"{PROG} › Исключения из программы › 7",
             "7. плановая (вне обострения) санация полости рта, если не предусмотрено программой.")]
    st = _store(rows)
    run = _Run(cid="c01", st=st, execute=bound_executor("c01", st))
    assert coverage_hits(run, "Стандарт", t) == []
