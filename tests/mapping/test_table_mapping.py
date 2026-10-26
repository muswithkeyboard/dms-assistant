"""Разметка таблиц перечня оператором (ADR 0012, CONTRACTS §10) на своих маленьких docx / xlsx: таблицы документов
с объединёнными ячейками, подпись заголовка, предложение ролей и профиль, применение разметки, проверка разметки."""
from __future__ import annotations

import datetime as dt

from dms_core import table_mapping as tm
from dms_core.kit import KitContract, KitFile
from dms_core.schemas import InsurerProfile, TableMapping, TableRef
from fixtures import client_kit as ck
from fixtures import mapping_fixture as mf


def _kc(tmp, *paths):
    files = [KitFile(path=p.name, title=p.stem, fmt=p.suffix.lstrip("."), doc_type="contract", scope="contract",
                     size=p.stat().st_size, sha256="0") for p in paths]
    return KitContract(id="u01", group_id="ug1", legal_entity="ООО", files=files)


def _docx_table():
    tmp = ck.workdir()
    p = mf.contract_docx(tmp / "Договор.docx")
    tables = tm.list_tables(_kc(tmp, p), tmp, {"Договор.docx": "u01.contract"})
    return tables


def _profile(mapping, sig, pid="ip1"):
    now = dt.datetime.now(dt.UTC)
    return InsurerProfile(id=pid, insurer_name="СК Тест", signature=sig, mapping=mapping, created_at=now,
                          updated_at=now)


def test_signature_normalizes_case_yo_marks_footnotes():
    rows = [["№", "Наименование ЛПУ¹", "  Адрес [^2] ", "Ёлка-палка*"], ["1", "x", "y", "z"]]
    assert tm.signature(rows, 0) == "наименование лпу|адрес|елка палка"
    assert tm.signature(rows, 5) == "" and tm.signature([], 0) == ""


def test_list_tables_skips_one_row_and_expands_merged_cells():
    tables = _docx_table()
    assert len(tables) == 1                                  # подпись сторон (одна строка) пропущена
    ref, title, rows = tables[0]
    assert ref == TableRef(doc_id="u01.contract", kind="docx_table", index=1) and title == "Договор"
    assert all(len(r) == len(mf.HEADER) for r in rows)      # объединение на всю ширину — та же сетка колонок
    assert rows[1] == ["Москва", "", "", "", "", ""]
    assert rows[2][1] == "Клиника Ромашка" and rows[3][1] == ""


def test_vertical_merge_repeats_text_down():
    tmp = ck.workdir()
    p = mf.vmerge_docx(tmp / "Перечень.docx")
    (_ref, _t, rows), = tm.list_tables(_kc(tmp, p), tmp, {"Перечень.docx": "u01.contract"})
    assert rows == [["Клиника", "Адрес"], ["Сеть Альфа", "ул. Первая, 1"], ["Сеть Альфа", "ул. Вторая, 2"]]


def test_suggest_guesses_roles_by_header_words():
    _ref, _t, rows = _docx_table()[0]
    m, pid = tm.suggest(rows, [])
    assert pid is None and m is not None and m.header_row == 0
    assert m.columns == {1: "clinic_name", 2: "address", 3: "care_type", 4: "franchise_pct", 5: "by_referral"}


def test_suggest_prefers_profile_by_signature():
    _ref, _t, rows = _docx_table()[0]
    own = TableMapping(header_row=0, columns={1: "clinic_name"}, program="Стандарт")
    p = _profile(own, tm.signature(rows, 0))
    other = _profile(TableMapping(columns={0: "clinic_name"}), "другая подпись", "ip2")
    assert tm.suggest(rows, [other, p]) == (own, "ip1")
    assert tm.profile_for(rows, [other, p]) is p and tm.profile_for(rows, [other]) is None


def test_suggest_xlsx_header_below_title_rows():
    tmp = ck.workdir()
    p = mf.clinics_xlsx(tmp / "Перечень.xlsx")
    (ref, _t, rows), = tm.list_tables(_kc(tmp, p), tmp, {"Перечень.xlsx": "u01.clinics"})
    assert ref.kind == "xlsx_sheet" and ref.sheet == "Список МО"
    m, _ = tm.suggest(rows, [])
    # «по направлению Страховщика» в колонке «Телефон» — предложение by_referral по содержимому (этап w10-fix2)
    assert m.header_row == 3 and m.columns == {0: "program", 1: "clinic_name", 2: "address", 3: "by_referral"}


def test_apply_rows_sections_continuation_franchise_referral_and_ids():
    ref, title, rows = _docx_table()[0]
    m, _ = tm.suggest(rows, [])
    out, warns = tm.apply("u01", ref, rows, m.model_copy(update={"program": "Стандарт"}), ref.doc_id,
                          title=tm.table_label(ref, title))
    got = [(r.id, r.clinic_name, r.care_type, r.franchise_pct, r.by_referral) for r in out]
    assert got == [
        ("u01.contract#t1r2#1", "Клиника Ромашка", "Поликлиника", 10.0, False),
        ("u01.contract#t1r2#2", "Клиника Ромашка", "Стоматология", 10.0, False),
        ("u01.contract#t1r3", "Клиника Ромашка", "Поликлиника", "—", False),      # второй адрес той же клиники
        ("u01.contract#t1r4", "Медцентр Василёк", "Поликлиника", None, True),
        ("u01.contract#t1r6", "Здоровье Плюс", "Стационар", 15.0, True),        # r5 — повтор заголовка
        ("u01.contract#t1r8", "Клиника Тверца", "Поликлиника", None, False),     # «по согласованию» — не число
    ]
    assert out[0].source_ref == "u01.contract#t1r2" and out[2].address == "г. Москва, ул. Полевая, д. 7"
    assert {r.program for r in out} == {"Стандарт"} and all(r.contract_id == "u01" for r in out)
    assert any("повторов заголовка — 1" in w for w in warns) and any("франшиза в строках — 1" in w for w in warns)
    assert not any("программа не указана" in w for w in warns)
    assert all("Ромашка" not in w for w in warns)             # в предупреждениях нет текстов ячеек


def test_apply_sections_become_care_type_and_no_program_source():
    ref, _t, rows = _docx_table()[0]
    m = TableMapping(columns={1: "clinic_name", 2: "address"})
    out, warns = tm.apply("u01", ref, rows, m, ref.doc_id)
    assert [(r.clinic_name, r.care_type) for r in out][-1] == ("Клиника Тверца", "Тверь")
    assert out[0].care_type == "Москва" and len(out) == 5
    # источника программы нет (w9-fix п. 1г): «программы договора» «Перечень клиник договора» больше не появляется —
    # в пробном разборе программа пустая и предупреждение; единственная программа договора — fallback_program
    assert {r.program for r in out} == {""} and not tm.has_program_source(m)
    assert any("программа не указана" in w for w in warns)
    out2, warns2 = tm.apply("u01", ref, rows, m, ref.doc_id, fallback_program="Стандарт")
    assert {r.program for r in out2} == {"Стандарт"} and not any("программа не указана" in w for w in warns2)


def test_apply_xlsx_refs_and_program_column():
    tmp = ck.workdir()
    p = mf.clinics_xlsx(tmp / "Перечень.xlsx")
    (ref, _t, rows), = tm.list_tables(_kc(tmp, p), tmp, {"Перечень.xlsx": "u01.clinics"})
    m = TableMapping(header_row=3, columns={0: "program", 1: "clinic_name", 2: "address", 3: "by_referral"})
    out, _w = tm.apply("u01", ref, rows, m, ref.doc_id)
    assert [(r.id, r.program, r.by_referral) for r in out] == [
        ("u01.clinics#Список МО!5", "Стандарт", False), ("u01.clinics#Список МО!6", "Стандарт", True),
        ("u01.clinics#Список МО!7", "Премиум", False)]


def test_single_column_numbered_list_with_address_label():
    rows = [["Медицинские учреждения"], ["Поликлиника"], ["1. Клиника Ромашка Адрес: г. Москва, ул. Лесная, 1"],
            ["2. Медцентр Василёк"], ["Стоматология"], ["3. Дента Адрес: г. Тверь"]]
    ref = TableRef(doc_id="u01.contract", kind="docx_table", index=0)
    out, _w = tm.apply("u01", ref, rows, TableMapping(columns={0: "clinic_name"}, program="Стандарт"), ref.doc_id)
    assert [(r.clinic_name, r.address, r.care_type) for r in out] == [
        ("Клиника Ромашка", "г. Москва, ул. Лесная, 1", "Поликлиника"), ("Медцентр Василёк", None, "Поликлиника"),
        ("Дента", "г. Тверь", "Стоматология")]


def test_validate_errors():
    rows = [["Наименование", "Адрес"], ["А", "Б"]]
    for mapping, code in ((TableMapping(columns={1: "address"}), "mapping_no_name"),
                          (TableMapping(columns={0: "clinic_name", 5: "address"}), "mapping_bad_column"),
                          (TableMapping(header_row=7, columns={0: "clinic_name"}), "mapping_bad_header")):
        try:
            tm.validate(rows, mapping)
        except tm.MappingError as e:
            assert e.code == code and e.message
        else:
            raise AssertionError(code)


def test_franchise_and_referral_values():
    assert tm.parse_franchise("10,5 %") == (10.5, True)
    assert tm.parse_franchise("не применяется") == ("—", True) and tm.parse_franchise("") == (None, True)
    assert tm.parse_franchise("по согласованию") == (None, False)
    assert tm.parse_referral("По направлению Страховщика") and tm.parse_referral("да") and tm.parse_referral("+")
    assert not tm.parse_referral("+7 495 000") and not tm.parse_referral("") and not tm.parse_referral("Дальний")
