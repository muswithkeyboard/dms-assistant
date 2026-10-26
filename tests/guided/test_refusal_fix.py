"""Разбор отказа в процедуре — исправления по ревизии 8 (этап w14-fix, config/service_classes.yaml v3).

1. Исключение — основание отказа, только если в нём названа сама процедура, слово её класса или анатомия вопроса;
   общие слова («операция», «удаление», «пластический» у хирургии) недостаточны; названное после «кроме …» — не
   основание; самое конкретное — первым.
2. Пункт о порядке госпитализации — не из раздела о взносах / неуплате; раздел о порядке оказания помощи — раньше.
3. Вид помощи для проверки клиники — из разбора (класс → вид помощи матрицы), а не слово услуги.
4. Масштаб процедуры: ампутация — только стационар, удаление папилломы — поликлиника.
5. Слот услуги без «кто / где отказал»; «ГКБ 11» — слот клиники по перечню.
6. Словарь: «отнимать», «класть», «не перевели в реанимацию», «резать не будем», опечатки; «ОРИТ», «ЭКО»,
   «реанимашка» — не клиника.
7. Договор без программ — разбор по пунктам договора.
8. Тексты: «может быть обоснован, в том числе», плановый порядок без «соответствует документам», короткий номер
   пункта — с разделом.
9. ГК: одинаковое уточнение — один раз.
10. Новые классы: МРТ, ЭКО, прививка, гемодиализ, пломба, ринопластика, липосакция, стентирование.
Тексты пунктов — выдуманные (по образцу устройства программ, без текстов комплекта).
"""
from __future__ import annotations

from dms_core.adapters.base import text_hash
from dms_core.citations import verify
from dms_core.constants import Scenario, StatementKind
from dms_core.guided import (
    _class_exclusions,
    _order_clause,
    _Run,
    bound_executor,
    class_care,
    procedure_cfg,
    procedure_class,
    procedure_classes,
    run_guided,
    text,
)
from dms_core.scenarios.route import (
    fix_typos,
    procedure_refusal,
    procedure_verb,
    refusal_marker,
    rule_slots,
)
from dms_core.schemas import CareMatrix, ChatContext, Clause, ClinicRow
from dms_core.store import KitStore
from fixtures.synth_kit import make_parsed

PROG = "Программа «Стандарт»"
CTX = ChatContext(contract_id="c01", program="Стандарт")
HOSP = "Программа «Стандарт». Стационарное обслуживание"
POLY = "Программа «Стандарт». Поликлиническое обслуживание"
EXCL = f"{PROG} › Страховщик не оплачивает"
CLAUSES = [
    # раздел о взносах: «экстренная госпитализация» здесь — не порядок оказания помощи (дефект 2)
    (None, "Договор › РАЗМЕР, СРОКИ И ПОРЯДОК ВНЕСЕНИЯ СТРАХОВОЙ ПРЕМИИ", "РАЗМЕР И ПОРЯДОК ВНЕСЕНИЯ СТРАХОВОЙ ПРЕМИИ",
     None),
    ("2.8.1", "Договор › РАЗМЕР, СРОКИ И ПОРЯДОК ВНЕСЕНИЯ СТРАХОВОЙ ПРЕМИИ › 2.8.1",
     ("2.8.1. При неуплате очередного взноса Страховщик организует и оплачивает исключительно экстренную "
      "госпитализацию по экстренным показаниям."), None),
    (None, f"{PROG} › Порядок оказания медицинской помощи", "Порядок оказания медицинской помощи:", "Стандарт"),
    ("5.1", f"{PROG} › Порядок оказания медицинской помощи › 5.1",
     ("5.1. Плановая госпитализация осуществляется по направлению лечащего врача после согласования со "
      "Страховщиком."), "Стандарт"),
    ("5.2", f"{PROG} › Порядок оказания медицинской помощи › 5.2",
     ("5.2. Экстренная госпитализация осуществляется по экстренным показаниям бригадой скорой медицинской "
      "помощи."), "Стандарт"),
    (None, EXCL, "Страховщик не оплачивает:", "Стандарт"),
    ("1", f"{EXCL} › 1", "1. Пластические и косметические операции, протезирование и эндопротезирование.", "Стандарт"),
    ("2", f"{EXCL} › 2", "2. Трансплантация органов и тканей, кроме переливания крови.", "Стандарт"),
    ("3", f"{EXCL} › 3", "3. Удаление папиллом, бородавок и родинок без медицинских показаний.", "Стандарт"),
    ("4", f"{EXCL} › 4", "4. Экстракорпоральные методы лечения: гемодиализ, плазмаферез.", "Стандарт"),
    ("6", f"{EXCL} › 6", "6. Лечение заболеваний коленного сустава методом артроскопии по желанию пациента.",
     "Стандарт"),
]


def _store(*, programs: bool = True) -> KitStore:
    p = make_parsed("c01")
    for i, (number, path, body, prog) in enumerate(CLAUSES):
        doc = "c01.contract" if path.startswith("Договор") else "c01.program"
        p.clauses.append(Clause(id=f"{doc}#x{i}", doc_id=doc, contract_id="c01", number=number, path=path,
                                program=prog if programs else None, text=body, text_hash=text_hash(body)))
    if programs:
        p.care = [CareMatrix(contract_id="c01", program="Стандарт", care_type=HOSP, present=True, source_refs=["m0"]),
                  CareMatrix(contract_id="c01", program="Стандарт", care_type=POLY, present=True, source_refs=["m1"])]
        p.clinics.append(ClinicRow(id="c01.clinics#t1r9", contract_id="c01", doc_id="c01.clinics", program="Стандарт",
                                   care_type="Стационарная помощь",
                                   clinic_name="Городская клиническая больница № 11", city="Рязань",
                                   source_ref="t1:r9"))
    else:
        p.care, p.clinics = [], []
        p.contract.programs = []
    st = KitStore(":memory:")
    st.save(p)
    st.save(make_parsed("c02"))
    return st


def _run(st: KitStore, question: str, ctx: ChatContext = CTX):
    slots = rule_slots(question, ctx, st)
    run = run_guided("c01", question, scenario=Scenario.sc01, slots=dict(slots), context=ctx, store=st)
    v = verify(run.draft, run.refs, st, "c01", program=ctx.program, tools=run.tools)
    return slots, run, v


def _excl(v) -> list[str]:
    return [" ".join(c.quote for c in s.citations) for s in v.draft.statements
            if s.text.startswith("Возможное основание отказа")]


# ---------------------------------------------------------------- 1. исключения

def test_general_class_words_are_not_grounds() -> None:
    st = _store()
    _s, run, v = _run(st, "отказали в удалении щитовидки")
    # «удаление» / «операции» — общие слова: ни «пластические операции», ни «удаление папиллом» не основание
    assert _excl(v) == []
    assert text("refusal_ground_exclusion") not in run.draft.summary
    _s, run, v = _run(st, "клиника не дала отрезать ногу - оправдано?")
    assert _excl(v) == []                                   # «эндопротезирование» — не об ампутации


def test_procedure_and_class_words_are_grounds_most_specific_first() -> None:
    st = _store()
    _s, run, v = _run(st, "ринопластику не одобрили, правомерно?")
    ex = _excl(v)
    assert ex and "Пластические и косметические операции" in ex[0]       # слово класса «пластический»
    assert text("refusal_ground_exclusion") in run.draft.summary
    _s, run, v = _run(st, "отказали удалить папиллому")
    assert "папиллом" in _excl(v)[0]                                       # сама процедура
    _s, run, v = _run(st, "отказ в артроскопии колена после травмы")
    assert "артроскопии" in _excl(v)[0]                                    # процедура + анатомия
    # гемодиализ назван в исключении прямо — исключение находит проверка покрытия (как прежде), разбор не нужен
    _s, run, v = _run(st, "не берут на гемодиализ")
    assert any("Экстракорпоральные" in c.quote for s in v.draft.statements for c in s.citations)
    run2 = _Run(cid="c01", st=st, execute=bound_executor("c01", st))
    got = _class_exclusions(run2, "Стандарт", procedure_classes("диализ"), "диализ", "", False)
    assert got and "Экстракорпоральные" in got[0][0].quote                 # слово класса «экстракорпоральный метод»


def test_after_except_is_not_a_ground() -> None:
    st = _store()
    _s, _r, v = _run(st, "переливание крови не сделали, отказ правомерен?")
    assert not any("переливания крови" in x for x in _excl(v))


def test_exclusion_selection_unit() -> None:
    st = _store()
    run = _Run(cid="c01", st=st, execute=bound_executor("c01", st))
    cls = procedure_classes("удаление щитовидки", "отказали в удалении щитовидки")
    assert _class_exclusions(run, "Стандарт", cls, "удаление щитовидки", "", False) == []
    cls = procedure_classes("липосакция", "")
    got = _class_exclusions(run, "Стандарт", cls, "липосакция", "", False)
    assert got and "пластический" in got[0][1]


# ---------------------------------------------------------------- 2. порядок госпитализации

def test_order_clause_skips_premium_section_and_prefers_procedure_order() -> None:
    st = _store()
    run = _Run(cid="c01", st=st, execute=bound_executor("c01", st))
    got = _order_clause(run, "Стандарт", procedure_cfg().orders["emergency"], False)
    assert got is not None and got[1].number == "5.2"                     # не 2.8.1 о неуплате взноса
    got = _order_clause(run, "Стандарт", procedure_cfg().orders["planned"], False)
    assert got is not None and got[1].number == "5.1"


# ---------------------------------------------------------------- 3–4. вид помощи

def test_scale_major_and_minor() -> None:
    surgery = procedure_class("ампутация стопы")
    assert surgery.code == "surgery"
    # «хирург» (вид помощи слота — поликлиника) не делает ампутацию амбулаторной
    assert class_care(surgery, "ампутация стопы", "хирург отказался ампутировать стопу",
                      "Амбулаторно-поликлиническая помощь") == ("inpatient",)
    for svc in ("холецистэктомию", "эндопротезировании сустава", "операции сердце шунтирование"):
        pc = procedure_class(svc)
        assert class_care(pc, svc) == ("inpatient",), svc
    for svc in ("удаление папилломы", "вскрытие абсцесса", "наложение швов пореза"):
        assert class_care(procedure_class(svc), svc)[0] == "outpatient", svc


def test_clinic_checked_by_analysis_care() -> None:
    st = _store()
    q = "в ГКБ 11 отказали ампутировать палец"
    slots, run, v = _run(st, q)
    assert slots.get("clinic") and "ампутация" in slots["service"]
    # клиника — строка «Стационарная помощь»: при проверке по виду помощи разбора нет «другого вида помощи»
    texts = " ".join(s.text for s in v.draft.statements)
    assert "относится к другим видам помощи" not in texts
    assert "Стационарное обслуживание" in run.draft.summary


# ---------------------------------------------------------------- 5–6. слоты и словарь

def test_service_slot_without_actors_and_places() -> None:
    st = _store()
    assert rule_slots("хирург отказался ампутировать стопу", CTX, st)["service"] == "ампутация стопы"
    assert rule_slots("роддом не принял на роды по ДМС", CTX, st)["service"] == "роды"
    assert rule_slots("больница отказала в операции на сердце", CTX, st)["service"].split()[0] == "операции"
    assert rule_slots("в травмпункте отказали зашить порез", CTX, st)["service"] == "наложение швов пореза"
    assert rule_slots("гинеколог не стал удалять полип", CTX, st)["service"] == "удаление полипа"
    # без отказа и без глагола-процедуры слот прежний
    assert rule_slots("входит ли приём хирурга?", CTX, st).get("service") != ""


def test_numbered_clinic_by_type_abbreviation() -> None:
    st = _store()
    s = rule_slots("ГКБ 11 Рязань отказала ампутировать палец", CTX, st)
    assert s.get("clinic", "").startswith("ГКБ 11") and "ГКБ" not in s["service"]


def test_dictionary_gaps() -> None:
    assert procedure_verb("отнимать") == "ампутация" and procedure_verb("класть") == "госпитализация"
    assert procedure_verb("прооперирвали") == "операция"                  # опечатка в форме глагола
    for q in ("мужа не перевели в реанимацию", "хирург сказал резать не будем",
              "скорая не захотела госпитализировать бабушку", "ампутацию пальца не сделали",
              "капельницу не поставили", "в реанимашку не взяли", "атказали в госпитализацыи"):
        assert refusal_marker(q), q
    assert procedure_refusal("справку не сделали") is None                # нет слова процедуры
    assert fix_typos("отказали в апперации на мениске") == "отказали в операции на мениске"
    assert fix_typos("ампутацыю пальца") == "ампутацию пальца"
    assert fix_typos("не положили в больнитсу") == "не положили в больницу"
    assert fix_typos("в Медси") == "в Медси"                               # имя с заглавной — как есть


def test_procedure_words_are_not_clinic_slot() -> None:
    st = _store()
    for q in ("отказали в ОРИТ после операции", "отказали в ЭКО", "в реанимашку не взяли",
              "атказали в госпитализацыи"):
        s = rule_slots(q, CTX, st)
        assert "clinic" not in s and s.get("service"), q


# ---------------------------------------------------------------- 7. договор без программ

def test_generic_contract_refusal_analysis() -> None:
    st = _store(programs=False)
    ctx = ChatContext(contract_id="c01")
    _s, run, v = _run(st, "клиника не дала отрезать ногу - оправдано?", ctx)
    texts = [s.text for s in v.draft.statements]
    assert text("refusal_order_planned", by_prog=text("refusal_by_contract")) in texts
    assert "матрицы видов помощи у договора нет" in run.draft.summary
    assert not any(s.kind is StatementKind.inference for s in v.draft.statements)   # матрицы нет — допущения нет
    assert run.draft.checks.care_in_program != "yes"


# ---------------------------------------------------------------- 8. тексты

def test_texts_are_not_exhaustive_nor_promises() -> None:
    st = _store()
    _s, run, v = _run(st, "ринопластику не одобрили, правомерно?")
    assert "Отказ может быть обоснован, в том числе если" in run.draft.summary
    assert "по словарю видов помощи (допущение)" in run.draft.summary
    planned = [s.text for s in v.draft.statements if s.text.startswith("Плановая госпитализация")]
    assert planned and all("соответствует документам" not in t for t in planned)
    # короткий номер пункта исключения — с названием раздела
    ex = [s.text for s in v.draft.statements if s.text.startswith("Возможное основание отказа")]
    assert ex and "(Страховщик не оплачивает › п. 1)" in ex[0]


# ---------------------------------------------------------------- 9. ГК

def test_group_same_missing_once() -> None:
    from dms_core.engine import answer

    st = _store()
    a = answer("клиника не дала отрезать ногу - оправдано?", ChatContext(group_id="gk9", program="Стандарт"),
               llm=None, store=st)
    reason = text("refusal_missing_reason")
    assert a.missing.count(reason) == 1
    assert not any(m.endswith(": " + reason) for m in a.missing)


# ---------------------------------------------------------------- 10. новые классы

def test_new_procedure_classes() -> None:
    want = {"МРТ коленного сустава": "diagnostics", "ЭКО": "reproductive", "прививке гриппа": "vaccination",
            "гемодиализ": "dialysis", "пломбу": "dental", "ринопластику": "plastic", "липосакции": "plastic",
            "стентировании сосудов сердца": "cardiovascular", "лазерной коррекции зрения": "vision_correction"}
    for svc, code in want.items():
        assert procedure_class(svc).code == code, svc
    # основной класс — слово, которое в услуге раньше: «операция варикоза, говорят косметика» — хирургия
    got = procedure_classes("операция варикоза косметики")
    assert got[0].code == "surgery" and "plastic" in {x.code for x in got}
    # стоматология: «удаление зуба» при вопросе о стоматологии — стоматологическое вмешательство
    assert procedure_class("удаление зуба", "", "Стоматологическая помощь").code == "dental"
