"""Таблицы перечня без строки заголовка (Дополнение §10 к CONTRACTS, ADR 0012): по таблице на программу, первая
строка — название программы. Подпись по раскладке, программа из названия таблицы, предложение экрана, профиль по
раскладке и «применить ко всем таблицам с такой же раскладкой» — на своём маленьком docx (tests/fixtures)."""
from __future__ import annotations

import datetime as dt

from test_table_mapping import _kc

from dms_core import table_mapping as tm
from dms_core.schemas import InsurerProfile, TableMapping
from fixtures import client_kit as ck
from fixtures import mapping_fixture as mf

LAYOUT = TableMapping(header_row=None, columns={0: "clinic_name", 2: "address", 3: "code"}, program_from_title=True)


def _tables(crooked=False):
    tmp = ck.workdir()
    p = mf.program_tables_docx(tmp / "Программа.docx", crooked=crooked)
    return tm.list_tables(_kc(tmp, p), tmp, {"Программа.docx": "u01.program"})


def test_layout_signature_same_for_program_tables_and_has_no_text():
    tables = _tables()
    assert len(tables) == 2
    sigs = {tm.layout_signature(rows) for _r, _t, rows in tables}
    assert sigs == {"layout:4:0,2,3"}                       # колонки 0, 2, 3 заполнены; название программы не входит
    assert tm.mapping_signature(tables[0][2], LAYOUT) == "layout:4:0,2,3"
    assert tm.layout_counts(tables) == {"layout:4:0,2,3": 2}
    assert tm.layout_signature([["Только название"], ["Подзаголовок"]]) == ""


def test_suggest_header_none_and_title_only_when_layout_repeats():
    rows = _tables()[0][2]
    one, _ = tm.suggest(rows, [], 1)
    assert one is None or one.header_row is not None        # одна таблица в договоре — прежнее предложение
    m, pid = tm.suggest(rows, [], 2)
    assert pid is None and m == LAYOUT                      # роли по содержимому: название, адрес, код «П5фВ»


def test_apply_program_from_title_rows_and_programs():
    tables = _tables()
    got = {}
    for ref, title, rows in tables:
        out, warns = tm.apply("u01", ref, rows, LAYOUT, ref.doc_id, title=title)
        got[ref.index] = out
        assert not [w for w in warns if "не одна ячейка" in w]
    biz, tver = got[0], got[1]
    assert {r.program for r in biz} == {"Бизнес"} and {r.program for r in tver} == {"Тверь"}
    assert [r.clinic_name for r in biz] == ["Клиника Ромашка", "Клиника Ромашка", "Медцентр Василёк", "Здоровье Плюс"]
    assert [r.care_type for r in biz] == ["Поликлиника | Врач на дом"] * 3 + ["Стоматология"]
    assert biz[1].address == "г. Москва, ул. Полевая, д. 7" and biz[0].code == "П5фВ"
    assert biz[0].source_ref == "u01.program#t0r2"           # строка-название (r0) и подзаголовок (r1) — не данные


def test_header_none_without_program_from_title():
    ref, title, rows = _tables()[1]
    m = TableMapping(header_row=None, columns={0: "clinic_name", 2: "address"}, program="Стандарт")
    tm.validate(rows, m)                                    # заголовка нет — проверять нечего
    out, _ = tm.apply("u01", ref, rows, m, ref.doc_id, title=title)
    # первая строка («Тверь») — подзаголовок, вид помощи следующих строк заменяет подзаголовок «Поликлиника»
    assert [r.clinic_name for r in out] == ["Клиника Тверца", "Лаборатория Тест"]
    assert {r.program for r in out} == {"Стандарт"} and {r.care_type for r in out} == {"Поликлиника"}
    out2, warns2 = tm.apply("u01", ref, rows, m.model_copy(update={"program": None}), ref.doc_id, title=title)
    assert {r.program for r in out2} == {""} and any("программа не указана" in w for w in warns2)
    assert tm.signature(rows, None) == ""


def test_crooked_title_row_warns_and_table_is_not_parsed():
    tables = _tables(crooked=True)
    ref, title, rows = tables[2]
    assert tm.layout_signature(rows) == "layout:4:0,2,3"     # та же раскладка — «применить ко всем» её заденет
    out, warns = tm.apply("u01", ref, rows, LAYOUT, ref.doc_id, title=title)
    assert out == [] and len(warns) == 1 and "не одна ячейка" in warns[0] and "Без названия" not in warns[0]


def test_title_row_skips_long_text_above_title():
    text = "Программа добровольного медицинского страхования «Х»: для обращения позвоните по номеру пульта " * 2
    rows = [[text, "", ""], ["Пермь", "", ""], ["Поликлиника", "", ""], ["Клиника", "г. Пермь, ул. Ленина, д. 1", "К1"]]
    assert tm.title_row(rows) == 1 and tm.title_text(rows, 1) == "Пермь"
    assert tm.title_row([["А", "Б"], ["В", ""]]) is None and tm.title_row([]) is None


def test_profile_by_layout_and_same_signature_tables():
    tables = _tables(crooked=True)
    rows0 = tables[0][2]
    now = dt.datetime.now(dt.UTC)
    p = InsurerProfile(id="ip1", insurer_name="СК Тест", signature=tm.mapping_signature(rows0, LAYOUT), mapping=LAYOUT,
                       created_at=now, updated_at=now)
    assert all(tm.profile_for(rows, [p]) is p for _r, _t, rows in tables)
    assert tm.suggest(tables[1][2], [p]) == (LAYOUT, "ip1")
    same = tm.same_signature_tables(tables, rows0, LAYOUT)
    assert [t[0].index for t in same] == [0, 1, 2]
    # разметка с заголовком: «та же подпись» — по строке заголовка (у таблиц программ её нет — только сама)
    head = TableMapping(header_row=0, columns={0: "clinic_name"})
    assert [t[0].index for t in tm.same_signature_tables(tables, rows0, head)] == [0]
