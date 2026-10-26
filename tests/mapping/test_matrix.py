"""Матрица «программа × вид помощи» по разметке оператора (ADR 0013, CONTRACTS §11): значение ячейки, широкая и
длинная таблица, виды помощи из перечня (derive_care), проверка содержимого, предложение разметки; в конвейере —
readiness ready, «нельзя» только по явному «нет», текст лимита в ячейке не даёт ни «можно», ни «нельзя», ссылки
строк матрицы проходят citations.verify. Свои маленькие docx / xlsx; всё — во временной папке."""
from __future__ import annotations

from dms_core import onboarding
from dms_core import table_mapping as tm
from dms_core.citations import verify
from dms_core.schemas import (
    ChatContext,
    ClinicRow,
    DraftAnswer,
    DraftCitation,
    DraftStatement,
    TableMapping,
    TableRef,
)
from fixtures import client_kit as ck
from fixtures import mapping_fixture as mf
from helpers import env

REF = TableRef(doc_id="u01.contract", kind="docx_table", index=2)
WIDE = [mf.WIDE_HEADER, *mf.WIDE_ROWS]
WIDE_MAP = TableMapping(role="care_matrix", matrix_layout="wide", header_row=0, columns={0: "care_type"},
                        program_cols={1: "", 2: ""})
PROGRAMS = ["Стандарт", "Премиум"]
LIST_MAP = TableMapping(header_row=0, columns={0: "program", 1: "clinic_name", 2: "address", 3: "care_type"})


def test_cell_value_words_marks_and_skips():
    for v in ("да", "Да", "+", "входит", "Включено", "✓", "V", "да, входит", "Включено в программу", " + "):
        assert tm.cell_value(v) is True, v
    for v in ("нет", "НЕТ", "—", "-", "–", "не входит", "не включено", "x", "Х", "×"):
        assert tm.cell_value(v) is False, v
    for v in ("", "  ", None, "до 100 000 руб.", "5 раз в год", "по направлению врача", "не более 3", "без стоматологии",
              "да при наличии направления", "в"):
        assert tm.cell_value(v) is None, v


def test_wide_matrix_cells_refs_skips_and_program_match():
    res = tm.apply_matrix_rows("u01", REF, WIDE, WIDE_MAP, PROGRAMS)
    got = {(m.program, m.care_type): (m.present, m.source_refs) for m in res.rows}
    assert got[("Стандарт", "Амбулаторно-поликлиническая помощь")] == (True, ["u01.contract#t2r1c1"])
    assert got[("Премиум", "Амбулаторно-поликлиническая помощь")] == (True, ["u01.contract#t2r1c2"])
    assert got[("Стандарт", "Стоматологическая помощь")][0] is False      # явное «нет»
    assert got[("Стандарт", "Помощь на дому")][0] is False                 # «—»
    assert got[("Премиум", "Стационарная помощь")][0] is True
    assert ("Стандарт", "Стационарная помощь") not in got                  # лимит текстом — строки нет
    assert not any(c == "Итого" for _p, c in got)                          # итог — не вид помощи
    assert len(res.rows) == 7 and res.skipped_total == 1
    assert res.skipped == [{"cell": "строка 5, колонка 2", "text": "до 100 000 руб. в год"}]
    rows, warns = tm.apply_matrix("u01", REF, WIDE, WIDE_MAP, "u01.contract", PROGRAMS, title="Договор, таблица 3")
    assert len(rows) == 7 and any("ячеек матрицы с текстом не «да» / «нет»" in w and "— 1" in w for w in warns)
    # колонка с названием не программы договора — предупреждение (строки остаются)
    alien = WIDE_MAP.model_copy(update={"program_cols": {1: "Стандарт", 2: "Золотой"}})
    _rows, warns = tm.apply_matrix("u01", REF, WIDE, alien, "u01.contract", PROGRAMS)
    assert any("«Золотой» не совпала" in w for w in warns)
    # программа колонки — по нормализации («Программа «Стандарт»» → «Стандарт»)
    assert tm.match_program("Программа «Стандарт»", PROGRAMS) == "Стандарт"
    assert tm.match_program("СТАНДАРТ", PROGRAMS) == "Стандарт" and tm.match_program("Золотой", PROGRAMS) is None


def test_wide_conflict_pair_dropped():
    rows = [["Вид помощи", "Стандарт"], ["Стоматология", "да"], ["Стоматология", "нет"], ["Поликлиника", "да"]]
    m = TableMapping(role="care_matrix", matrix_layout="wide", header_row=0, columns={0: "care_type"},
                     program_cols={1: ""})
    res = tm.apply_matrix_rows("u01", REF, rows, m, ["Стандарт"])
    assert [(x.care_type, x.present) for x in res.rows] == [("Поликлиника", True)] and res.conflicts == 1


def test_long_matrix_present_split_and_single_program():
    rows = [["Программа", "Вид помощи", "Входит"], ["Стандарт", "Поликлиника", "да"],
            ["Стандарт", "Стоматология", "нет"], ["Премиум", "Стоматология\nПомощь на дому", "+"],
            ["Премиум", "Стационар", "лимит 300 000 руб."], ["", "Скорая", "да"]]
    m = TableMapping(role="care_matrix", matrix_layout="long", header_row=0,
                     columns={0: "program", 1: "care_type", 2: "present"})
    res = tm.apply_matrix_rows("u01", REF, rows, m, PROGRAMS)
    got = {(x.program, x.care_type): (x.present, x.source_refs) for x in res.rows}
    assert got == {("Стандарт", "Поликлиника"): (True, ["u01.contract#t2r1"]),
                   ("Стандарт", "Стоматология"): (False, ["u01.contract#t2r2"]),
                   ("Премиум", "Стоматология"): (True, ["u01.contract#t2r3#1"]),
                   ("Премиум", "Помощь на дому"): (True, ["u01.contract#t2r3#2"])}
    assert res.skipped_total == 1                         # «лимит …» — пропуск; строка без программы — не учтена
    # без колонки «входит»: строка таблицы = вид помощи входит; программа — единственная программа договора
    m2 = TableMapping(role="care_matrix", matrix_layout="long", header_row=0, columns={1: "care_type"})
    res2 = tm.apply_matrix_rows("u01", REF, rows, m2, ["Стандарт"])
    assert {x.program for x in res2.rows} == {"Стандарт"} and all(x.present for x in res2.rows)


def test_validate_matrix_errors():
    def code(m, programs=PROGRAMS):
        try:
            tm.validate_matrix(WIDE, m, single_program=len(programs) == 1)
        except tm.MappingError as e:
            return e.code
        return None

    assert code(WIDE_MAP) is None
    assert code(WIDE_MAP.model_copy(update={"columns": {}})) == "mapping_no_care"
    assert code(WIDE_MAP.model_copy(update={"program_cols": {}})) == "mapping_no_program"
    assert code(WIDE_MAP.model_copy(update={"program_cols": {9: "Х"}})) == "mapping_bad_column"
    assert code(WIDE_MAP.model_copy(update={"matrix_layout": None})) == "mapping_invalid"
    long_m = TableMapping(role="care_matrix", matrix_layout="long", header_row=0, columns={0: "care_type"})
    assert code(long_m) == "mapping_no_program" and code(long_m, ["Стандарт"]) is None


def test_matrix_from_clinics_splits_care_and_refs():
    rows = [ClinicRow(id="u01.p#t1r2", contract_id="u01", doc_id="u01.p", program="ВИП",
                      care_type="Поликлиника | Врач на дом", clinic_name="Ромашка", source_ref="u01.p#t1r2"),
            ClinicRow(id="u01.p#t1r3", contract_id="u01", doc_id="u01.p", program="ВИП", care_type="Поликлиника",
                      clinic_name="Василёк", source_ref="u01.p#t1r3"),
            ClinicRow(id="u01.p#t1r4", contract_id="u01", doc_id="u01.p", program="ВИП", care_type="",
                      clinic_name="Без вида", source_ref="u01.p#t1r4"),
            ClinicRow(id="u01.p#t1r5", contract_id="u01", doc_id="u01.p", program="", care_type="Стоматология",
                      clinic_name="Без программы", source_ref="u01.p#t1r5")]
    got = {(m.program, m.care_type): m.source_refs for m in tm.matrix_from_clinics("u01", rows)}
    assert got == {("ВИП", "Поликлиника"): ["u01.p#t1r2#care", "u01.p#t1r3#care"],
                   ("ВИП", "Врач на дом"): ["u01.p#t1r2#care"]}
    assert all(m.present for m in tm.matrix_from_clinics("u01", rows))


def test_matrix_check_and_suggest():
    assert tm.matrix_check(WIDE, WIDE_MAP, PROGRAMS) == []
    # колонка видов помощи — названия клиник, в колонках программ — адреса: подозрительно
    bad = [mf.LIST_HEADER, *mf.LIST_ROWS]
    m = TableMapping(role="care_matrix", matrix_layout="wide", header_row=0, columns={1: "care_type"},
                     program_cols={2: "Стандарт", 3: "Премиум"})
    reasons = tm.matrix_check(bad, m, PROGRAMS)
    assert any("видов помощи" in r for r in reasons) and any("«да» / «нет»" in r for r in reasons)
    alien = WIDE_MAP.model_copy(update={"program_cols": {1: "Альфа", 2: "Бета"}})
    assert any("программ, совпавших" in r for r in tm.matrix_check(WIDE, alien, PROGRAMS))
    # предложение: ≥ 2 колонок заголовка — программы договора, слева — виды помощи
    sugg, pid = tm.suggest(WIDE, [], programs=PROGRAMS)
    assert pid is None and sugg == TableMapping(role="care_matrix", matrix_layout="wide", header_row=0,
                                                columns={0: "care_type"}, program_cols={1: "Стандарт", 2: "Премиум"})
    # без программ (первая обработка, этап w10-fix2): программы — текст заголовков
    assert tm.suggest(WIDE, [])[0] == TableMapping(role="care_matrix", matrix_layout="wide", header_row=0,
                                                   columns={0: "care_type"}, program_cols={1: "", 2: ""})
    # длинная: заголовок «Программа / Вид помощи / Входит»
    long_rows = [["Программа", "Вид помощи", "Входит"], ["Стандарт", "Поликлиника", "да"]]
    sl, _ = tm.suggest(long_rows, [], programs=PROGRAMS)
    assert sl == TableMapping(role="care_matrix", matrix_layout="long", header_row=0,
                              columns={0: "program", 1: "care_type", 2: "present"})
    # перечень клиник с программами — не матрица
    assert tm.suggest(bad, [], programs=PROGRAMS)[0].role == "clinic_list"


def test_profile_mapping_keeps_header_programs_generic():
    m = WIDE_MAP.model_copy(update={"program_cols": {1: "Стандарт", 2: "Премиум (ВИП)"}})
    assert tm.profile_mapping(WIDE, m).program_cols == {1: "", 2: "Премиум (ВИП)"}


# ---------------------------------------------------------------- конвейер подключения

def _setup(tmp, files):
    reg = ck.registry(tmp)
    g = reg.create_client_group("Клиент Матрица")
    cc = reg.add_client_contract(g.id, "ООО Матрица")
    for src, doc_type in files:
        ck.put_file(reg, ck.clients_root(tmp), g.id, cc.id, src, doc_type=doc_type)
    return reg, cc


def _process(reg, st, tmp, cc):
    return onboarding.process(cc.id, store=st, registry=reg, root=ck.clients_root(tmp), run_selfcheck=False)


def _tables(reg, st, tmp, cc):
    kc = onboarding.kit_contract(reg.client_group(cc.group_id), cc, reg.client_files(cc.id))
    return tm.list_tables(kc, ck.clients_root(tmp), {d.source_path: d.id for d in st.documents(cc.id)})


def _matrix_contract(tmp, *, matrix=True):
    reg, cc = _setup(tmp, [(mf.matrix_docx(tmp / "src" / "Договор.docx"), "contract")])
    st = ck.kit_store(tmp)
    _process(reg, st, tmp, cc)
    lst, wide = _tables(reg, st, tmp, cc)
    reg.save_table_mapping(cc.id, lst[0], LIST_MAP)
    if matrix:
        reg.save_table_mapping(cc.id, wide[0], WIDE_MAP)
    return reg, st, cc, _process(reg, st, tmp, cc)


def test_process_wide_matrix_ready_counts_and_warnings():
    tmp = ck.workdir()
    _reg, st, cc, rep = _matrix_contract(tmp)
    assert rep.readiness == "ready" and rep.counts["care_rows"] == 7 and rep.counts["mapped_matrices"] == 1
    assert rep.counts["mapped_tables"] == 1 and rep.counts["clinics"] == 4
    assert any(w.startswith("Применена разметка матрицы видов помощи к таблицам: 1, строк матрицы — 7") for w in
               rep.warnings)
    assert any("Не учтено ячеек матрицы" in w and "— 1" in w for w in rep.warnings)
    care = {(m.program, m.care_type): m.present for m in st.care(cc.id)}
    assert care[("Стандарт", "Стоматологическая помощь")] is False
    # без матрицы — limited, подсказка про матрицу
    tmp2 = ck.workdir()
    _r, _s, _c, rep2 = _matrix_contract(tmp2, matrix=False)
    assert rep2.readiness == "limited" and rep2.counts["care_rows"] == 0
    assert any("Виды помощи из этого перечня → матрица" in w for w in rep2.warnings)


def test_process_derive_care_from_list():
    tmp = ck.workdir()
    reg, cc = _setup(tmp, [(mf.matrix_docx(tmp / "src" / "Договор.docx"), "contract")])
    st = ck.kit_store(tmp)
    _process(reg, st, tmp, cc)
    lst, _wide = _tables(reg, st, tmp, cc)
    reg.save_table_mapping(cc.id, lst[0], LIST_MAP.model_copy(update={"derive_care": True}))
    rep = _process(reg, st, tmp, cc)
    assert rep.readiness == "ready" and rep.counts["care_rows"] == 4 and rep.counts["mapped_matrices"] == 1
    assert all(m.present for m in st.care(cc.id))
    assert any("Виды помощи из перечня клиник → матрица: строк матрицы — 4" in w for w in rep.warnings)


def test_explicit_no_conflicts_with_derived_yes_dropped():
    """Матрица говорит «нет», а в перечне есть клиника этого вида помощи — противоречие: пара не учитывается (ни
    «можно», ни «нельзя»); совпадающие пары сливают ссылки."""
    lst_ref = TableRef(doc_id="u01.contract", kind="docx_table", index=1)
    clinics, _w = tm.apply("u01", lst_ref, [mf.LIST_HEADER, *mf.LIST_ROWS], LIST_MAP, "u01.contract")
    derived = tm.matrix_from_clinics("u01", clinics)
    rows = [["Вид помощи", "Премиум"], ["Помощь на дому", "нет"], ["Стоматология", "да"]]
    m = WIDE_MAP.model_copy(update={"program_cols": {1: ""}})
    explicit = tm.apply_matrix_rows("u01", REF, rows, m, PROGRAMS).rows
    care, dropped = onboarding._merge_care([], explicit, derived)
    pairs = {(x.program, x.care_type): x for x in care}
    assert dropped == 1 and ("Премиум", "Помощь на дому") not in pairs
    assert pairs[("Премиум", "Стоматология")].source_refs == ["u01.contract#t2r2c1", "u01.contract#t1r4#care"]


def test_long_matrix_xlsx_in_process():
    tmp = ck.workdir()
    reg, cc = _setup(tmp, [(mf.matrix_docx(tmp / "src" / "Договор.docx"), "contract"),
                           (mf.long_matrix_xlsx(tmp / "src" / "Виды помощи.xlsx"), "program")])
    st = ck.kit_store(tmp)
    _process(reg, st, tmp, cc)
    tables = _tables(reg, st, tmp, cc)
    sheet = next(t for t in tables if t[0].kind == "xlsx_sheet")
    sugg, _ = tm.suggest(sheet[2], [], programs=PROGRAMS)
    assert sugg is not None and sugg.matrix_layout == "long"
    reg.save_table_mapping(cc.id, tables[0][0], LIST_MAP)
    reg.save_table_mapping(cc.id, sheet[0], sugg)
    rep = _process(reg, st, tmp, cc)
    assert rep.readiness == "ready" and rep.counts["care_rows"] == 4
    refs = {m.care_type: m.source_refs for m in st.care(cc.id, "Премиум")}
    assert refs["Помощь на дому"] == [f"{cc.id}.program#Виды помощи!4#2"]


def test_matrix_rows_pass_citation_verify():
    tmp = ck.workdir()
    _reg, st, cc, _rep = _matrix_contract(tmp)
    ref = f"{cc.id}.contract#t1r2c1"                              # «Стоматологическая помощь» × «Стандарт» — «нет»
    draft = DraftAnswer(contract_id=cc.id, summary="", statements=[DraftStatement(
        text="Стоматологическая помощь не входит в программу Стандарт", citations=[DraftCitation(ref=ref, quote="")])])
    v = verify(draft, {ref}, st, cc.id, program="Стандарт")
    assert v.citations[ref].verified and v.citations[ref].quote == "Стандарт: Стоматологическая помощь — не входит"


def test_guided_labels_by_matrix_no_yes_and_limit_text():
    """«нет» в ячейке → «нельзя»; «да» + выбранная клиника → «можно»; лимит текстом → ни «можно», ни «нельзя»."""
    from dms_core.engine import answer

    tmp = ck.workdir()
    _reg, st, cc, _rep = _matrix_contract(tmp)
    clinics = {(r.program, r.clinic_name): r.id for r in st.clinics(cc.id)}
    with env(DMS_ENGINE_MODE="guided"):
        a = answer("Входит ли стоматологическая помощь в программу?", ChatContext(contract_id=cc.id,
                                                                                   program="Стандарт"),
                   llm=None, store=st)
        assert a.label is not None and a.label.value == "denied"
        cites = [c for s in a.statements for c in s.citations if c.verified]
        assert any(c.table_ref == f"{cc.id}.contract#t1r2c1" for c in cites)
        q = "Можно ли вызвать врача на дом из клиники «Клиника Ромашка»?"
        ctx = ChatContext(contract_id=cc.id, program="Премиум")
        a2 = answer(q, ctx, llm=None, store=st, clinic_picks={cc.id: clinics[("Премиум", "Клиника Ромашка")]})
        assert a2.label is not None and a2.label.value == "allowed"
        q3 = "Можно ли на плановую госпитализацию в «Здоровье Плюс»?"
        ctx3 = ChatContext(contract_id=cc.id, program="Стандарт")
        a3 = answer(q3, ctx3, llm=None, store=st, clinic_picks={cc.id: clinics[("Стандарт", "Здоровье Плюс")]})
        assert a3.label is None or a3.label.value not in ("allowed", "denied")


def test_matrix_profile_auto_applied_same_insurer_only():
    tmp = ck.workdir()
    reg = ck.registry(tmp)
    st = ck.kit_store(tmp)
    g = reg.create_client_group("Клиент П")
    ccs = []
    for legal, insurer in (("ООО Первый", "СК Матрица"), ("ООО Второй", "АО «СК Матрица»"), ("ООО Третий", "СК Иная")):
        cc = reg.add_client_contract(g.id, legal, insurer_name=insurer)
        ck.put_file(reg, ck.clients_root(tmp), g.id, cc.id, mf.matrix_docx(tmp / "src" / cc.id / "Договор.docx"),
                    doc_type="contract")
        _process(reg, st, tmp, cc)
        ccs.append(cc)
    lst, wide = _tables(reg, st, tmp, ccs[0])
    reg.save_insurer_profile("СК Матрица", tm.mapping_signature(lst[2], LIST_MAP), LIST_MAP)
    reg.save_insurer_profile("СК Матрица", tm.mapping_signature(wide[2], WIDE_MAP),
                             tm.profile_mapping(wide[2], WIDE_MAP))
    rep = _process(reg, st, tmp, ccs[1])
    assert rep.readiness == "ready" and rep.counts["mapped_matrices"] == 1 and rep.counts["care_rows"] == 7
    assert any("Применена разметка матрицы СК «СК Матрица» к таблицам: 1" in w for w in rep.warnings)
    rep3 = _process(reg, st, tmp, ccs[2])                         # другая СК — только предложение
    assert rep3.counts["mapped_matrices"] == 0 and rep3.counts["care_rows"] == 0
