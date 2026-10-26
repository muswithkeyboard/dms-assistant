"""Дефекты ревизии разметки таблиц (этап w9-fix, ADR 0012): таблица премий вместо перечня (проверка содержимого,
строки «Итого», программа-фантом, пустой вид помощи не даёт «можно»), автоприменение профиля только своей СК и по
сильной подписи, примечания «^^^^^ Внимание …» — в условия строки выше, блоки «Вариант № N …» в одной колонке,
допуск раскладки по колонке названия, значки «по направлению». Свои маленькие таблицы и docx; без data/."""
from __future__ import annotations

import datetime as dt

from dms_core import onboarding
from dms_core import table_mapping as tm
from dms_core.guided import clinic_care_fits
from dms_core.schemas import ChatContext, InsurerProfile, TableMapping, TableRef
from fixtures import client_kit as ck
from fixtures import mapping_fixture as mf
from helpers import env

REF = TableRef(doc_id="u01.program", kind="docx_table", index=0)
PREMIUM = [["Название Программы", "Страховая премия за одно лицо", "Количество"],
           ["Бизнес", "27 002,00", "1"], ["Стандарт", "13 500,00", "2"], ["Итого:", "54 004,00", "3"]]


def _profile(mapping, sig, insurer="СК Тест", pid="ip1"):
    now = dt.datetime.now(dt.UTC)
    return InsurerProfile(id=pid, insurer_name=insurer, signature=sig, mapping=mapping, created_at=now,
                          updated_at=now)


# ---------------------------------------------------------------- п. 1: таблица премий, «Итого», фантом, вид помощи

def test_content_check_flags_premium_table_and_passes_clinic_list():
    m = TableMapping(header_row=0, columns={0: "clinic_name"}, program="Бизнес")
    reasons = tm.content_check(PREMIUM, m)
    assert reasons and "сумм" in reasons[0]                       # колонки адреса нет, а колонки с суммами есть
    nums = [["Название", "Адрес"], ["1", "27 002,00"], ["Итого:", "54 004,00"]]
    got = tm.content_check(nums, TableMapping(header_row=0, columns={0: "clinic_name", 1: "address"}))
    assert any("колонке названия" in r for r in got) and any("колонке адреса" in r for r in got)
    _r, _t, rows = _clinic_table()
    good = TableMapping(header_row=0, columns={1: "clinic_name", 2: "address", 3: "care_type"}, program="Стандарт")
    assert tm.content_check(rows, good) == []


def test_apply_skips_totals_numbers_and_sums_in_name_column():
    m = TableMapping(header_row=0, columns={0: "clinic_name"}, program="Стандарт")
    out, warns = tm.apply("u01", REF, PREMIUM, m, REF.doc_id, title="Т")
    assert [r.clinic_name for r in out] == ["Бизнес", "Стандарт"]          # «Итого:» — не клиника
    assert any("итогов, чисел и сумм" in w and "— 1" in w for w in warns)
    assert tm.not_clinic_name("27 002,00") and tm.not_clinic_name("< 20%") and tm.not_clinic_name("Всего по договору")
    assert not tm.not_clinic_name("МЦ Здоровье") and not tm.not_clinic_name("ЦКБ")


def test_no_program_source_has_no_phantom_program_and_is_not_applied():
    """Без колонки программы, программы для таблицы и программы из названия строки не получают «программу
    договора» «Перечень клиник договора»: в onboarding такая разметка не применяется (предупреждение)."""
    tmp = ck.workdir()
    reg = ck.registry(tmp)
    g = reg.create_client_group("Клиент Ф")
    cc = reg.add_client_contract(g.id, "ООО Ф")
    ck.put_file(reg, ck.clients_root(tmp), g.id, cc.id, mf.contract_docx(tmp / "src" / "Договор.docx"),
                doc_type="contract")
    st = ck.kit_store(tmp)
    onboarding.process(cc.id, store=st, registry=reg, root=ck.clients_root(tmp), run_selfcheck=False)
    kc = onboarding.kit_contract(reg.client_group(cc.group_id), cc, reg.client_files(cc.id))
    (ref, _t, _rows), = tm.list_tables(kc, ck.clients_root(tmp), {d.source_path: d.id for d in st.documents(cc.id)})
    reg.save_table_mapping(cc.id, ref, TableMapping(header_row=0, columns={1: "clinic_name", 2: "address"}))
    rep = onboarding.process(cc.id, store=st, registry=reg, root=ck.clients_root(tmp), run_selfcheck=False)
    assert rep.counts["mapped_tables"] == 0 and rep.counts["clinics"] == 0
    assert any("не указано, откуда программа" in w for w in rep.warnings)
    assert tm.default_program() not in {p.name for p in st.contract(cc.id).programs}


def _flat_docx(path):
    """Договор неизвестной СК: пункты и перечень без вида помощи (ни колонки, ни подзаголовков разделов)."""
    from docx import Document

    d = Document()
    d.add_paragraph("1. Предмет договора")
    d.add_paragraph("1.1. Страховщик организует медицинскую помощь в клиниках перечня по программе «Стандарт».")
    t = d.add_table(rows=0, cols=3)
    for vals in (("№", "Наименование", "Адрес"), ("1", "Медцентр Василёк", "г. Москва, пр-т Мира, д. 5"),
                 ("2", "Клиника Ромашка", "г. Москва, ул. Лесная, д. 1")):
        row = t.add_row()
        for c, v in zip(row.cells, vals, strict=True):
            c.text = v
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(path)
    return path


def test_empty_care_type_row_never_gives_allowed():
    """Строка перечня без вида помощи не подходит к виду помощи вопроса (guided.clinic_care_fits): выбор такой
    строки — «клиника есть в перечне, вид помощи не указан», clinic_in_program не yes, метки «можно» нет."""
    from dms_core import engine

    assert not clinic_care_fits("", [["терапевт"]]) and not clinic_care_fits("  ", [["терапевт"]])
    tmp = ck.workdir()
    reg = ck.registry(tmp)
    g = reg.create_client_group("Клиент В")
    cc = reg.add_client_contract(g.id, "ООО В")
    ck.put_file(reg, ck.clients_root(tmp), g.id, cc.id, _flat_docx(tmp / "src" / "Договор.docx"), doc_type="contract")
    st = ck.kit_store(tmp)
    onboarding.process(cc.id, store=st, registry=reg, root=ck.clients_root(tmp), run_selfcheck=False)
    kc = onboarding.kit_contract(reg.client_group(cc.group_id), cc, reg.client_files(cc.id))
    (ref, _t, _rows), = tm.list_tables(kc, ck.clients_root(tmp), {d.source_path: d.id for d in st.documents(cc.id)})
    reg.save_table_mapping(cc.id, ref, TableMapping(header_row=0, columns={1: "clinic_name", 2: "address"},
                                                    program="Стандарт"))
    onboarding.process(cc.id, store=st, registry=reg, root=ck.clients_root(tmp), run_selfcheck=False)
    rows = st.clinics(cc.id, program="Стандарт")
    assert rows and all(not r.care_type for r in rows)
    rid = next(r.id for r in rows if r.clinic_name == "Медцентр Василёк")
    captured = {}
    orig = engine._one_contract

    def one(*a, **k):
        res = orig(*a, **k)
        captured["checks"] = res["verified"].draft.checks
        return res

    engine._one_contract = one
    try:
        with env(DMS_ENGINE_MODE="guided"):
            ctx = ChatContext(contract_id=cc.id, program="Стандарт")
            q = "Можно ли к терапевту в клинику «Медцентр Василёк»?"
            a = engine.answer(q, ctx, llm=None, store=st, clinic_picks={cc.id: rid})
            assert a.label is None or a.label.value != "allowed"
            assert captured["checks"].clinic_in_program != "yes"
            assert any("без вида помощи" in s.text for s in a.statements)        # сведение, не «есть в перечне»
            a2 = engine.answer(q, ctx, llm=None, store=st)             # без выбора — тоже не «можно»
            assert a2.label is None or a2.label.value != "allowed"
            assert captured["checks"].clinic_in_program != "yes"
    finally:
        engine._one_contract = orig


def _clinic_table():
    tmp = ck.workdir()
    p = mf.contract_docx(tmp / "Договор.docx")
    from test_table_mapping import _kc

    return tm.list_tables(_kc(tmp, p), tmp, {"Договор.docx": "u01.contract"})[0]


# ---------------------------------------------------------------- п. 2: автоприменение профиля

def test_auto_profile_needs_same_insurer_strong_signature_and_content():
    _r, _t, rows = _clinic_table()
    m = TableMapping(header_row=0, columns={1: "clinic_name", 2: "address", 3: "care_type"}, program="Стандарт")
    p = _profile(m, tm.signature(rows, 0), insurer="АО «СК Тест»")
    assert tm.insurer_key("ООО «Тест Страхование»") == tm.insurer_key("СК ТЕСТ") == "тест"
    assert tm.auto_profile(rows, [p], "Тест страхование")[0] is p
    assert tm.auto_profile(rows, [p], None) == (None, "why_no_insurer")
    assert tm.auto_profile(rows, [p], "Альфа") == (None, "why_other_insurer")
    assert tm.profile_for(rows, [p], None) is p                               # предложение — остаётся
    # слабая подпись: заголовок из двух ячеек; раскладка без адреса
    two = [["Клиника", "Город"], ["Клиника Ромашка", "Москва"], ["Медцентр Василёк", "Тверь"]]
    m2 = TableMapping(header_row=0, columns={0: "clinic_name", 1: "city"}, program="Стандарт")
    p2 = _profile(m2, tm.signature(two, 0))
    assert not tm.strong_signature(p2.signature, m2) and tm.auto_profile(two, [p2], "СК Тест") == (None, "why_weak")
    assert not tm.strong_signature("layout:7:0,3,6", TableMapping(header_row=None, columns={0: "clinic_name"}))
    # содержимое не похоже на перечень — профиль не применяется сам
    mp = TableMapping(header_row=0, columns={0: "clinic_name", 1: "legal_name", 2: "city"}, program="Х")
    p3 = _profile(mp, tm.signature(PREMIUM, 0))
    assert tm.auto_profile(PREMIUM, [p3], "СК Тест") == (None, "why_suspicious")


# ---------------------------------------------------------------- п. 3: примечания к строке выше

NOTES = [["МО", "", "", ""], ["Поликлиника | Врач на дом", "", "", ""],
         ["Клиника А", tm.icon_mark(), "Москва, ул. Ленина, д.1", "П1"],
         ["^^^^^ Внимание предыдущая клиника: только взрослое отделение.", "", "", ""],
         ["Клиника Б", "", "Москва, ул. Мира, д.2", "П2"],
         ["* Примечание: по записи.", "", "", ""],
         ["Стоматология", "", "", ""], ["Зуб", "", "Москва, ул. Зубная, д.3", "С1"]]


def test_note_rows_go_to_access_note_not_care_type():
    m = TableMapping(header_row=None, program_from_title=True,
                     columns={0: "clinic_name", 1: "by_referral", 2: "address", 3: "code"})
    out, warns = tm.apply("u01", REF, NOTES, m, REF.doc_id, title="Т")
    by = {r.clinic_name: r for r in out}
    assert by["Клиника А"].access_note == "Внимание предыдущая клиника: только взрослое отделение."
    assert by["Клиника Б"].care_type == "Поликлиника | Врач на дом"          # примечание — не подзаголовок
    assert by["Клиника Б"].access_note == "Примечание: по записи."
    assert by["Зуб"].care_type == "Стоматология" and {r.program for r in out} == {"МО"}
    assert any("примечаний к строке клиники выше — 2" in w for w in warns)
    assert tm.is_note("Внимание: вход со двора") and not tm.is_note("Поликлиника")


# ---------------------------------------------------------------- п. 4: блоки «Вариант № N …» в одной колонке

ZETTA = [["Виды медицинской помощи и Медицинские учреждения"],
         ["Общие для вариантов: 1 Рязань Стандарт, 2 Москва Премиум"],
         ["1. Клиника Общая Адрес: 390000, Рязанская обл, Рязань г, Ленина ул, д.1"],
         ["Вариант № 1 Рязань Стандарт"],
         ["Программа \"Стандарт\". Поликлиническое обслуживание."],
         ["1. Клиника Рязанская Адрес: 390000, Рязанская обл, Рязань г, Мира ул, д.2"],
         ["Вариант № 2 Москва Премиум"],
         ["Франшиза (требуется активация) - 15%. программа \"Стандарт\". Стоматологическое обслуживание."],
         ["1. Зуб Москва Адрес: 101000, Москва г, Тверская ул, д.3"],
         ["2. Зуб Москва-2 Адрес: 101000, Москва г, Арбат ул, д.4"],
         ["3. Зуб Москва-3 Адрес: 101000, Москва г, Арбат ул, д.6"],
         ["4. Зуб Москва-4 Адрес: 101000, Москва г, Арбат ул, д.8"],
         ["Общие для всех вариантов"],
         ["1. Телемедицина Адрес: 101000, Москва г, Онлайн ул, д.5"]]


def test_variant_blocks_set_program_franchise_and_ids():
    sugg, _ = tm.suggest(ZETTA, [])
    assert sugg == TableMapping(header_row=0, columns={0: "clinic_name"}, program_from_title=True)
    out, warns = tm.apply("u01", REF, ZETTA, sugg, REF.doc_id, title="Т")
    got = {(r.clinic_name, r.program) for r in out}
    assert ("Клиника Общая", "Рязань Стандарт") in got and ("Клиника Общая", "Москва Премиум") in got
    assert ("Клиника Рязанская", "Рязань Стандарт") in got and ("Клиника Рязанская", "Москва Премиум") not in got
    assert {r.program for r in out if r.clinic_name.startswith("Зуб")} == {"Москва Премиум"}
    assert {r.program for r in out if r.clinic_name == "Телемедицина"} == {"Рязань Стандарт", "Москва Премиум"}
    zub = next(r for r in out if r.clinic_name == "Зуб Москва")
    assert zub.franchise_pct == 15.0 and zub.care_type.startswith("Программа \"Стандарт\". Стоматолог")
    assert next(r for r in out if r.clinic_name == "Клиника Рязанская").franchise_pct is None
    assert zub.address.startswith("101000") and zub.id == "u01.program#t0r8"
    assert {r.id for r in out if r.clinic_name == "Клиника Общая"} == {"u01.program#t0r2#v1", "u01.program#t0r2#v2"}
    assert len({r.id for r in out}) == len(out) and not any("программа не указана" in w for w in warns)
    assert len([r for r in out if r.clinic_name == "Телемедицина"]) == 2


def test_match_program_to_contract_programs():
    known = ["Рязань Стандарт", "Москва Премиум"]
    assert onboarding._match_program("рязань  стандарт", known) == "Рязань Стандарт"
    assert onboarding._match_program("Вариант 2 Москва Премиум (взрослые)", known) == "Москва Премиум"
    assert onboarding._match_program("Тверь", known) == "Тверь"


# ---------------------------------------------------------------- п. 5: допуск раскладки, похожие таблицы

def test_layout_tolerates_sparse_name_column_and_similar_tables():
    main = [["Пермь", "", "", ""], ["Поликлиника", "", "", ""]] + \
        [[f"Клиника {i}", "", f"Пермь, ул. Мира, д.{i}", f"П{i}"] for i in range(6)]
    sparse = [["Волжский", "", "", ""], ["Поликлиника", "", "", ""], ["Сеть Медси", "", "Волжский, ул. Мира, д.1", "П9"]] + \
        [["", "", f"Волжский, ул. Ленина, д.{i}", "П9"] for i in range(8)]
    m = TableMapping(header_row=None, program_from_title=True, columns={0: "clinic_name", 2: "address", 3: "code"})
    assert tm.layout_signature(main) == "layout:4:0,2,3" and tm.layout_signature(sparse) == "layout:4:2,3"
    assert tm.signature_fits(sparse, m, tm.mapping_signature(main, m))
    tables = [(REF, "П", main), (TableRef(doc_id="u01.program", kind="docx_table", index=1), "П", sparse)]
    assert len(tm.same_signature_tables(tables, main, m)) == 2
    # код в другой колонке — не та же раскладка
    other = [[r[0], r[3], r[2], ""] for r in main]
    assert not tm.signature_fits(other, m, tm.mapping_signature(main, m))
    assert tm.similar(main, sparse) and not tm.similar(main, PREMIUM)


def test_preview_flags_similar_unmapped_and_applied_profile():
    from dms_api.service import Service

    main = [["Пермь", "", "", ""]] + [[f"Клиника {i}", "", f"Пермь, ул. Мира, д.{i}", f"П{i}"] for i in range(6)]
    alike = [["Тверь", "", "", ""]] + [[f"Клиника {i}", "", f"Тверь, ул. Мира, д.{i}", ""] for i in range(6)]
    pv = Service._preview(REF, "П", alike, {}, [], {}, None, [main])
    assert pv["similar_unmapped"] is True and pv["applied_profile"] is None and pv["suspicious"] == []
    m = TableMapping(header_row=None, program_from_title=True, columns={0: "clinic_name", 2: "address", 3: "code"})
    p = _profile(m, tm.layout_signature(main))
    pv2 = Service._preview(REF, "П", main, {}, [p], {}, "СК Тест", [])
    assert pv2["applied_profile"] == "ip1" and pv2["profile_id"] == "ip1" and pv2["similar_unmapped"] is False
    assert Service._preview(REF, "П", main, {}, [p], {}, None, [])["applied_profile"] is None


# ---------------------------------------------------------------- п. 6: ошибки, значки, сводные предупреждения

def test_negative_column_message_and_icon_referral():
    try:
        tm.validate(PREMIUM, TableMapping(columns={-1: "address", 0: "clinic_name"}))
    except tm.MappingError as e:
        assert e.code == "mapping_bad_column" and "-1" in e.message and "Колонка 0" not in e.message
    else:
        raise AssertionError("отрицательная колонка прошла проверку")
    assert tm.parse_referral(tm.icon_mark()) and not tm.parse_referral("")
    tmp = ck.workdir()
    prog = next(p for p in ck.ren_files(tmp) if "Программа" in p.name)
    grid = tm._docx_grid(prog)[0]
    assert grid[2][1] == tm.icon_mark() and tm.layout_signature(grid) == tm.layout_signature(
        [[("" if c == tm.icon_mark() else c) for c in r] for r in grid])           # значки в раскладку не входят
    roles = tm._content_roles(grid)
    assert roles is not None and roles.get(1) == "by_referral"
    m = TableMapping(header_row=None, program_from_title=True, columns=roles)
    out, _w = tm.apply("u01", REF, grid, m, REF.doc_id)
    assert [r.by_referral for r in out] == [True, True, False]
    assert out[1].access_note == "Внимание предыдущая клиника: только взрослое отделение."
