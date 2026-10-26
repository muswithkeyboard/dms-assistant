"""Разбор отказа клиники в процедуре (этап w14-refusal, config/service_classes.yaml procedures / procedure_classes).

1. Слот услуги: глагол-процедура не теряется и нормализуется по словарю («отрезать ногу» → «ампутация ноги»),
   опечатка в глаголе («атрезать») — по сходству; отрицание глагола словаря — маркер отказа.
2. Класс процедуры по словарю; при вопросе о другом виде помощи (стоматология) класс не применяется.
3. guided: при отказе — вид помощи класса по матрице (допущение «по словарю», kind=inference), порядок плановой и
   экстренной госпитализации, исключения класса — каждое со ссылкой, прошедшей citations.verify; краткий вывод
   «оправдан ли отказ»; уточнения — в missing (ответ не блокируется). Checks и метка не меняются: «можно» /
   «нельзя» разбор не даёт.
Тексты пунктов — выдуманные (по образцу устройства программ, без текстов комплекта); тест на комплекте — SKIP
без data/derived/kit.sqlite.
"""
from __future__ import annotations

from dms_core.adapters.base import text_hash
from dms_core.citations import verify
from dms_core.config import load_yaml
from dms_core.constants import Label, Scenario, StatementKind
from dms_core.guided import procedure_cfg, procedure_class, procedure_synonyms, run_guided, text
from dms_core.scenarios.route import procedure_refusal, procedure_verb, refusal_marker, rule_slots
from dms_core.scenarios.sc01_refusal import decide_label
from dms_core.schemas import CareMatrix, ChatContext, Clause
from dms_core.store import KitStore
from fixtures.synth_kit import make_parsed
from helpers import kit_db_or_skip

PROG = "Программа «Стандарт»"
HOSP = "Программа «Стандарт». Стационарное обслуживание"
POLY = "Программа «Стандарт». Поликлиническое обслуживание"
CTX = ChatContext(contract_id="c01", program="Стандарт")
CLAUSES = [
    (None, f"{PROG} › Стационарное обслуживание", "Стационарное обслуживание:"),
    ("2.5", f"{PROG} › Стационарное обслуживание › 2.5",
     ("2.5. Плановая госпитализация осуществляется по направлению лечащего врача после согласования со "
      "Страховщиком.")),
    ("2.6", f"{PROG} › Стационарное обслуживание › 2.6",
     ("2.6. Экстренная госпитализация осуществляется при состоянии, требующем срочной медицинской помощи, "
      "бригадой скорой медицинской помощи.")),
    (None, f"{PROG} › Исключения из программы", "Исключения из программы:"),
    ("5", f"{PROG} › Исключения из программы › 5",
     "5. Пластические и косметические операции, протезирование конечностей и эндопротезирование суставов."),
    ("6", f"{PROG} › Исключения из программы › 6", "6. Лечение заболеваний зубов и пародонта у ортодонта."),
]


def _store() -> KitStore:
    p = make_parsed("c01")
    for i, (number, path, body) in enumerate(CLAUSES):
        p.clauses.append(Clause(id=f"c01.program#x{i}", doc_id="c01.program", contract_id="c01", number=number,
                                path=path, program="Стандарт", text=body, text_hash=text_hash(body)))
    p.care = [CareMatrix(contract_id="c01", program="Стандарт", care_type=HOSP, present=True, source_refs=["m0"]),
              CareMatrix(contract_id="c01", program="Стандарт", care_type=POLY, present=True, source_refs=["m1"])]
    st = KitStore(":memory:")
    st.save(p)
    st.save(make_parsed("c02"))
    return st


def _answer(st: KitStore, question: str):
    slots = rule_slots(question, CTX, st)
    run = run_guided("c01", question, scenario=Scenario.sc01, slots=dict(slots), context=CTX, store=st)
    v = verify(run.draft, run.refs, st, "c01", program="Стандарт", tools=run.tools)
    label, _n, _r = decide_label(v.draft.checks, has_alternative=bool(v.draft.alternatives),
                                 facts={"refusal": bool(slots.get("refusal"))})
    return slots, run, v, label


# ---------------------------------------------------------------- 1. слот услуги и маркер отказа

def test_procedure_verb_kept_in_service_slot() -> None:
    st = _store()
    s = rule_slots("клиника не дала отрезать ногу - оправдано?", CTX, st)
    assert s["service"] == "ампутация ноги" and s.get("refusal")
    assert rule_slots("отказали удалить аппендикс", CTX, st)["service"] == "удаление аппендикса"
    assert rule_slots("оперировать катаракту отказались", CTX, st)["service"] == "операция катаракты"
    # процедура уже названа существительным — второй раз не добавляется
    assert rule_slots("отказали в операции на колене", CTX, st)["service"].startswith("операции")


def test_procedure_verb_typo_and_unknown() -> None:
    assert procedure_verb("атрезать") == "ампутация"            # опечатка: сходство ≥ procedures.fuzzy
    assert procedure_verb("прооперировали") == "операция"
    assert procedure_verb("делать") is None and procedure_verb("ногу") is None


def test_negated_procedure_is_refusal() -> None:
    for q in ("не стали удалять кисту", "клиника не хочет оперировать грыжу", "не госпитализировали с аппендицитом",
              "не положили в больницу", "в стационар не положили", "не взяли в реанимацию"):
        assert refusal_marker(q), q
    assert procedure_refusal("можно ли удалить зуб мудрости?") is None
    assert refusal_marker("входит ли операция на колене?") is None


# ---------------------------------------------------------------- 2. класс процедуры

def test_procedure_class_by_dictionary() -> None:
    assert procedure_class("ампутация ноги").code == "surgery"
    assert procedure_class("роды").code == "childbirth"
    assert procedure_class("капельницу").code == "infusion"
    assert procedure_class("лонгете ногу").code == "immobilization"
    assert procedure_class("реанимацию").code == "intensive_care"
    # класс по словам вопроса, если в слоте услуги их нет («на дому»); вид помощи вопроса — из видов класса
    assert procedure_class("на дому", "отказали в капельнице на дому", "Помощь на дому").code == "infusion"
    # «удаление зуба» при вопросе о стоматологии — не хирургия стационара, а стоматологическое вмешательство (w14-fix)
    assert procedure_class("удаление зуба", "", "Стоматологическая помощь").code == "dental"
    assert procedure_class("справка в бассейн") is None
    assert "аппендэктомия" in procedure_synonyms("удаление аппендицита")


def test_procedure_config_refers_to_known_codes() -> None:
    cares = set((load_yaml("care_types.yaml") or {}).get("care_types") or {})
    cfg = procedure_cfg()
    assert cfg.classes and cfg.orders
    for pc in cfg.classes:
        assert pc.words and pc.care and set(pc.care) <= cares, pc.code
        assert set(pc.orders) <= set(cfg.orders), pc.code


def test_guided_text_keys_are_identifiers() -> None:
    # ключ без пробела после двоеточия («key:'…'») YAML читает как строку-ключ — шаблон пропадает молча
    keys = list((load_yaml("texts_ru/ui.yaml") or {}).get("guided") or {})
    assert all(k.isidentifier() for k in keys), [k for k in keys if not k.isidentifier()][:3]
    assert "clinic_refusal_check" in keys and "refusal_sum_care" in keys


# ---------------------------------------------------------------- 3. разбор отказа в guided

def test_refusal_analysis_gives_conclusion_with_verified_refs() -> None:
    st = _store()
    _slots, run, v, label = _answer(st, "клиника не дала отрезать ногу - оправдано?")
    d = run.draft
    # (г) краткий вывод: вид помощи по матрице, условия обоснованности отказа, что уточнить
    assert "Стационарное обслуживание" in d.summary and "Отказ может быть обоснован" in d.summary
    assert "(п. 2.5" in d.summary and "Уточните" in d.summary
    # (а) допущение по словарю — отдельным видом утверждения, со ссылкой на строку матрицы
    inf = [s for s in v.draft.statements if s.kind is StatementKind.inference]
    assert inf and all("по словарю видов помощи" in s.text.casefold() for s in inf)
    texts = [s.text for s in v.draft.statements]
    # (в) порядок плановой и экстренной госпитализации, (б) исключение класса — после verify
    assert text("refusal_order_planned", by_prog=text("refusal_by_program", program="Стандарт")) in texts
    assert text("refusal_order_emergency", by_prog=text("refusal_by_program", program="Стандарт")) in texts
    # «протезирование конечностей и эндопротезирование» — общие слова хирургии, не об ампутации (w14-fix)
    assert not any(t.startswith("Возможное основание отказа") for t in texts)
    assert not any("ортодонт" in c.quote for s in v.draft.statements for c in s.citations)   # не о классе
    assert all(s.citations for s in v.draft.statements)
    # (д) уточнения — не блокирующие
    assert text("refusal_missing_mode") in d.missing and text("refusal_missing_reason") in d.missing
    assert not d.questions
    # метка — как прежде: без подтверждённой клиники «можно» нет, «нельзя» разбор не даёт
    assert label not in (Label.allowed, Label.denied)
    assert d.checks.care_in_program != "yes" and d.checks.exclusion != "yes"


def test_no_refusal_no_analysis() -> None:
    st = _store()
    _s, run, _v, _l = _answer(st, "входит ли ампутация ноги в программу?")
    assert not any(s.kind is StatementKind.inference for s in run.draft.statements)
    assert text("refusal_missing_reason") not in run.draft.missing


def test_refusal_without_procedure_class_unchanged() -> None:
    st = _store()
    _s, run, _v, _l = _answer(st, "клиника отказала в справке для бассейна")
    assert not any(s.kind is StatementKind.inference for s in run.draft.statements)


# ---------------------------------------------------------------- комплект (SKIP без базы)

def test_kit_refusal_has_conclusion() -> None:
    from dms_core.engine import answer

    st = kit_db_or_skip()
    empty = text("refusal_sum_grounds", grounds="")[:10]
    for cid, prog in (("c01", "Рязань Стандарт"), ("c06", "Москва"), ("c10", "Стандарт")):
        a = answer("клиника не дала отрезать ногу - оправдано?", ChatContext(contract_id=cid, program=prog),
                   llm=None, store=st)
        assert a.summary.startswith("Вид помощи") and empty in a.summary, cid
        assert sum(c.verified for s in a.statements for c in s.citations) >= 3, cid
        assert a.label not in (Label.allowed, Label.denied), cid
