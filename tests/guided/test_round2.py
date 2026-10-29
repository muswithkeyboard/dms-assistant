"""Круг 2 ревизии w16 на синтетике: справка из карточки договора со ссылкой на пункт, продолжение пункта-обрывка,
бессмысленный ввод и английская раскладка, кэш пунктов по ревизии, строка-страховой риск не вид помощи."""
from __future__ import annotations

from dms_core.adapters.base import text_hash
from dms_core.citations import verify
from dms_core.constants import Confidence, Scenario
from dms_core.engine import answer
from dms_core.guided import card_topics, care_row_matches, run_guided
from dms_core.inputcheck import is_gibberish, wrong_layout
from dms_core.schemas import ChatContext, Clause
from dms_core.store import KitStore
from fixtures.synth_kit import make_parsed

PERIOD = "Настоящий Договор вступает в силу с 00 часов 00 минут «01» января 2026 г. и действует до 31.12.2026 г."
HEADCOUNT = "Общая численность Застрахованных по настоящему Договору составляет 10 (десять) человек."
SUM_HEAD = "Страховая сумма на одного Застрахованного по настоящему Договору составляет:"
SUM_ROW = "по риску расходов на амбулаторно-поликлиническое обслуживание — 500 000 рублей"


def _store(*, with_period: bool = True) -> KitStore:
    st = KitStore(":memory:")
    p = make_parsed("c01")
    extra = [("5.1", PERIOD)] if with_period else []
    extra += [("2.1", HEADCOUNT), ("2.2", SUM_HEAD), (None, SUM_ROW), ("2.3", "Страховая премия уплачивается.")]
    for i, (number, text) in enumerate(extra):
        p.clauses.append(Clause(id=f"c01.contract#x{i}", doc_id="c01.contract", contract_id="c01", number=number,
                                path=f"Договор › {number or 'к п. 2.2'}", text=text, text_hash=text_hash(text)))
    st.save(p)
    return st


def _run(question: str, st: KitStore):
    ctx = ChatContext(group_id="gk9", contract_id="c01")
    run = run_guided("c01", question, scenario=Scenario.sc02_info, slots={}, context=ctx, store=st)
    return verify(run.draft, run.refs, st, "c01")


def test_card_topics():
    assert card_topics("Какой срок действия договора?") == ["period"]
    assert card_topics("Когда заканчивается полис?") == ["period"]
    assert card_topics("Сколько застрахованных по договору?") == ["headcount"]
    assert card_topics("Какая страховая компания у договора?") == ["insurer"]
    assert card_topics("Входит ли МРТ в программу?") == []
    assert card_topics("Какая страховая сумма?") == []
    assert card_topics("Какой период страхования?") == ["period"]
    assert card_topics("Численность застрахованных?") == ["headcount"]
    # другой срок / число / «кто страхует» — не справка о договоре (ревизия круга 2)
    for q in ["Кто страхует детей сотрудников?", "Сколько сотрудников можно добавить в договор?",
              "Сколько человек можно прикрепить к клинике?", "Срок действия гарантийного письма?",
              "Когда начинается действие полиса у нового сотрудника?"]:
        assert card_topics(q) == [], q
    # слот услуги из слов самой темы справку не отменяет, слот другой услуги — отменяет
    assert card_topics("срок действия", within="Какой срок действия договора?") == ["period"]
    assert card_topics("числа", within="До какого числа действует договор?") == ["period"]
    assert card_topics("МРТ", within="Какой срок действия полиса на МРТ?") == []


def test_period_from_card_cited_by_contract_clause():
    v = _run("Какой срок действия договора?", _store())
    texts = [s.text for s in v.draft.statements]
    assert texts == ["Договор действует с 01.01.2026 по 31.12.2026."]
    cit = v.draft.statements[0].citations
    assert cit[0].ref == "c01.contract#x0" and "вступает в силу" in v.citation(cit[0]).quote


def test_period_not_said_without_clause():
    # в тексте договора пункта со сроком нет — факт карточки не утверждается, а в «Что уточнить» — куда смотреть
    v = _run("Какой срок действия договора?", _store(with_period=False))
    assert not v.draft.statements
    assert any("срок действия" in m for m in v.draft.missing)


def test_headcount_from_card():
    v = _run("Сколько застрахованных по договору?", _store())
    assert [s.text for s in v.draft.statements] == ["Застрахованных по договору: 10."]


def test_dangling_clause_continued_by_next_rows():
    v = _run("Какая страховая сумма на одного застрахованного?", _store())
    texts = [s.text for s in v.draft.statements]
    assert SUM_HEAD in texts and SUM_ROW in texts       # значение — в строке после «составляет:»
    assert "Страховая премия уплачивается." not in texts  # следующий пункт с номером — уже не продолжение


def test_gibberish_and_layout():
    for t in ["фывапролд", "Фывапролд", "asdf qwer", "??", "ааааа", ""]:
        assert is_gibberish(t), t
    for t in ["привет", "колоноскопия", "лфк", "МРТ?", "Medsi", "Входит ли МРТ в программу?", "2.6.1", "№ 123/45",
              "J06.9", "20 км", "ЕМС"]:
        assert not is_gibberish(t), t
    assert wrong_layout("D[jlbn kb VHN d ghjuhfvve&") == "Входит ли МРТ в программу?"
    for t in ["Входит ли МРТ?", "Medsi", "K+31", "c05", "c01 c02", "J06", "in", "vs", "tot", "k"]:
        assert wrong_layout(t) is None, t


def test_engine_does_not_search_gibberish():
    st = _store()
    a = answer("фывапролд", ChatContext(group_id="gk9", contract_id="c01"), store=st)
    assert a.confidence is Confidence.insufficient and not a.statements and a.reply.lead.startswith("Не понял")
    a = answer("D[jlbn kb VHN&", ChatContext(group_id="gk9", contract_id="c01"), store=st)
    assert a.reply.options == ["Входит ли МРТ?"] and "английской раскладке" in a.reply.lead and a.reply.service


def test_clause_cache_follows_revision():
    st = _store()
    first = st.clauses("c01")
    assert st.clauses("c01") == first and st.clauses("c01") is not first   # свой список у каждого вызова
    p = make_parsed("c01")
    st.save(p)                                                             # переимпорт — новая ревизия
    assert len(st.clauses("c01")) == len(p.clauses) != len(first)
    assert [c.id for c in st.clauses("c01", doc_types=["program"])] == \
        [c.id for c in p.clauses if c.doc_id == "c01.program"]


def test_insurance_risk_row_is_not_care_type():
    row = "Страхование критических заболеваний (страх.сумма 100 тыс.руб., по диагностированию заболевания)"
    assert not care_row_matches(row, [["диагност"]])
    assert care_row_matches("Диагностика", [["диагност"]])


def test_bounded_cache_keeps_recent_entries():
    from dms_core.store.cache import BoundedCache

    c = BoundedCache(10, len, protect_sec=60)
    assert c.put("a", [1] * 6) and c.get("a") == [1] * 6
    # нужная недавно запись не вытесняется ради новой — новая не кэшируется (обход большой ГК)
    assert not c.put("b", [2] * 6) and c.get("b") is None and c.get("a") is not None
    old = BoundedCache(10, len, protect_sec=0)
    old.put("a", [1] * 6)
    assert old.put("b", [2] * 6) and old.get("a") is None      # давно не нужная — вытесняется
    assert not old.put("c", [3] * 11)                          # больше предела — не кэшируется


def test_card_through_engine_and_quote_has_value():
    # маршрутизатор кладёт «срок действия» в слот услуги — справка всё равно из карточки (ревизия круга 2)
    st = KitStore(":memory:")
    p = make_parsed("c01")
    long = "Страхователь обязуется передать список застрахованных. " * 8 + HEADCOUNT
    for i, (number, t) in enumerate([("5.1", PERIOD), ("2.1", long)]):
        p.clauses.append(Clause(id=f"c01.contract#x{i}", doc_id="c01.contract", contract_id="c01", number=number,
                                path=f"Договор › {number}", text=t, text_hash=text_hash(t)))
    st.save(p)
    ctx = ChatContext(group_id="gk9", contract_id="c01")
    a = answer("Какой срок действия договора?", ctx, store=st)
    assert [s.text for s in a.statements] == ["Договор действует с 01.01.2026 по 31.12.2026."]
    a = answer("Численность застрахованных?", ctx, store=st)
    assert [s.text for s in a.statements] == ["Застрахованных по договору: 10."]
    assert "10 (десять)" in a.statements[0].citations[0].quote      # цитата — окно со значением, а не начало пункта


def test_date_rx_left_boundary():
    import re

    from dms_core.guided import _date_rx

    rx = re.compile(_date_rx("2026-01-01"))
    assert rx.search("с 01.01.2026 г.") and rx.search("с «01» января 2026 г.") and rx.search("с 1 января 2026")
    assert not rx.search("с 11.01.2026 г.") and not rx.search("с «21» января 2026 г.")
