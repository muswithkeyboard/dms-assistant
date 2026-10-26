"""Этап w10-fix2 (ревизия w10-matrix): «по направлению» в тексте строки перечня, утечка табличных пунктов между
программами, перепутанные колонки программ матрицы, программа «*», матрица при первой обработке и профиль без
названий программ, «нет» матрицы против клиник перечня — противоречие. Свои маленькие docx; всё — во временной
папке."""
from __future__ import annotations

from dms_core import guided, onboarding
from dms_core import table_mapping as tm
from dms_core.engine import answer
from dms_core.schemas import CareMatrix, ChatContext, Clause, ClinicRow, TableMapping, TableRef
from fixtures import client_kit as ck
from fixtures import mapping_fixture as mf
from helpers import env

REF = TableRef(doc_id="u01.clinics", kind="xlsx_sheet", sheet="Список")
WIDE_A = TableMapping(role="care_matrix", matrix_layout="wide", header_row=0, columns={0: "care_type"},
                      program_cols={1: "", 2: ""})


# ---------------------------------------------------------------- 1. «по направлению» в тексте строки

def _sogaz_like(n_plain: int) -> list[list[str]]:
    """Перечень с колонкой «Телефон»: «по направлению» — только в строках ниже предпросмотра."""
    rows = [["Программа страхования", "Наименование ЛПУ", "Адрес ЛПУ", "Телефон", "Виды медицинской помощи"]]
    for i in range(n_plain):
        rows.append(["Стандарт", f"Клиника {i}", f"г. Москва, ул. Лесная, д. {i + 1}", "+7 (495) 000-00-00",
                     "Амбулаторно-поликлиническая помощь"])
    for i in range(3):
        rows.append(["Стандарт", f"ГКБ {i}", f"г. Москва, ул. Мира, д. {i + 1}",
                     "Организация услуг по направлению страховщика", "Стационарная помощь"])
    return rows


def test_referral_in_row_text_without_column_role():
    rows = _sogaz_like(30)
    # оператор не отметил колонку «Телефон»: строки «по направлению» — всё равно by_referral
    m = TableMapping(header_row=0, columns={0: "program", 1: "clinic_name", 2: "address", 4: "care_type"})
    out, _w = tm.apply("c", REF, rows, m, "u01.clinics")
    refs = {r.clinic_name: r.by_referral for r in out}
    assert refs["ГКБ 0"] is True and refs["Клиника 0"] is False
    assert tm.referral_in_text("Только по направлению врача") and not tm.referral_in_text("Направление: север")


def test_suggest_referral_column_by_whole_table():
    rows = _sogaz_like(30)                    # первые 15 строк (предпросмотр) — без «по направлению»
    assert not any("направлен" in c for r in rows[:tm.HEADER_SCAN] for c in r)
    m, _ = tm.suggest(rows, [])
    assert m is not None and m.columns.get(3) == "by_referral"


# ---------------------------------------------------------------- 2. утечка табличных пунктов между программами

def _attack(tmp, *, matrix: bool, insurer: str = "СК Атака", legal: str = "ООО А", reg=None, st=None):
    reg = reg or ck.registry(tmp)
    st = st or ck.kit_store(tmp)
    g = reg.create_client_group(f"Клиент {legal}")
    cc = reg.add_client_contract(g.id, legal, insurer_name=insurer)
    ck.put_file(reg, ck.clients_root(tmp), g.id, cc.id, mf.attack_docx(tmp / "src" / cc.id / "Договор.docx"),
                doc_type="contract")
    onboarding.process(cc.id, store=st, registry=reg, root=ck.clients_root(tmp), run_selfcheck=False)
    kc = onboarding.kit_contract(reg.client_group(cc.group_id), cc, reg.client_files(cc.id))
    tables = tm.list_tables(kc, ck.clients_root(tmp), {d.source_path: d.id for d in st.documents(cc.id)})
    lst, wide = tables[0], tables[1]
    reg.save_table_mapping(cc.id, lst[0], tm.suggest(lst[2], [])[0])
    if matrix:
        reg.save_table_mapping(cc.id, wide[0], tm.suggest(wide[2], [])[0])
    rep = onboarding.process(cc.id, store=st, registry=reg, root=ck.clients_root(tmp), run_selfcheck=False)
    return reg, st, cc, rep, tables


def _ask(st, cc, q, program, clinic):
    rows = [r for r in st.clinics(cc.id, program=program) if r.clinic_name == clinic]
    with env(DMS_ENGINE_MODE="guided"):
        return answer(q, ChatContext(contract_id=cc.id, program=program), llm=None, store=st,
                      clinic_picks={cc.id: rows[0].id} if rows else None)


def test_mixed_yes_no_row_is_condition_not_included():
    cl = Clause(id="u.contract#t1r3", doc_id="u.contract", contract_id="u", number=None, path="Д › табл. 2, стр. 4",
                text="Помощь на дому | нет | да", text_hash="x")
    assert guided._cell_section(cl) == ("condition", False)
    one = cl.model_copy(update={"text": "Массаж | да"})
    assert guided._cell_section(one) == ("included", False)


def test_unmapped_tables_do_not_leak_between_programs():
    """Размечен только перечень: строка матрицы «Помощь на дому | нет | да» не даёт «можно» для «Москвы»; таблица
    услуг программы «Москва» (название — абзац перед таблицей) — пункты программы «Москва», «Массаж | да» не даёт
    «можно» для «Барнаула»."""
    tmp = ck.workdir()
    _reg, st, cc, rep, _t = _attack(tmp, matrix=False)
    assert rep.counts["mapped_tables"] == 1 and rep.counts["programs"] == 2
    serv = [c for c in st.clauses(cc.id) if "#t2r" in c.id]
    assert serv and all(c.program == "Москва" for c in serv)
    home = _ask(st, cc, "Можно ли вызвать врача на дом из Клиники Альфа?", "Москва", "Клиника Альфа")
    assert home.label is not None and home.label.value != "allowed"
    mass = _ask(st, cc, "Положен ли классический массаж в Клинике Бета?", "Барнаул", "Клиника Бета")
    assert mass.label is not None and mass.label.value != "allowed"


def test_table_row_without_program_no_yes_in_multi_program_contract():
    """Строка таблицы без программы (название таблицы программу не называет) при двух программах — не «входит»."""
    tmp = ck.workdir()
    reg, st, cc, _rep, _t = _attack(tmp, matrix=False)
    # та же таблица услуг, но программа в названии не названа: пункты без программы
    rows = [c for c in st.clauses(cc.id) if "#t2r" in c.id]
    st.save(_with_clauses(st, cc.id, {c.id: c.model_copy(update={"program": None}) for c in rows}))
    mass = _ask(st, cc, "Положен ли классический массаж в Клинике Бета?", "Барнаул", "Клиника Бета")
    assert mass.label is not None and mass.label.value != "allowed"
    del reg


def _with_clauses(st, cid, repl):
    """ParsedContract договора из базы с заменой пунктов (для проверки правила guided без новой обработки)."""
    from dms_core.adapters.base import ParsedContract

    return ParsedContract(contract=st.contract(cid), documents=list(st.documents(cid)),
                          clauses=[repl.get(c.id, c) for c in st.clauses(cid)], clinics=list(st.clinics(cid)),
                          care=list(st.care(cid)), warnings=[])


# ---------------------------------------------------------------- 3–5. проверка матрицы и предложение

def test_swapped_program_columns_suspicious():
    rows = mf.ATTACK_WIDE
    ok = WIDE_A.model_copy(update={"program_cols": {1: "Москва", 2: "Барнаул"}})
    swap = WIDE_A.model_copy(update={"program_cols": {1: "Барнаул", 2: "Москва"}})
    assert tm.swapped_columns(rows, ok, ["Москва", "Барнаул"]) == []
    assert tm.swapped_columns(rows, swap, ["Москва", "Барнаул"])                    # заголовки — программы договора
    assert tm.swapped_columns(rows, swap, [])                                       # программ нет: заголовок другой колонки
    assert any("перепутаны" in r for r in tm.matrix_check(rows, swap, ["Москва", "Барнаул"]))
    # переименование без чужого заголовка — не подозрительно
    ren = WIDE_A.model_copy(update={"program_cols": {1: "Москва ВИП", 2: ""}})
    assert tm.swapped_columns(rows, ren, ["Москва ВИП", "Барнаул"]) == []


def test_star_program_not_suspicious():
    star = TableMapping(role="care_matrix", matrix_layout="long", header_row=0, columns={0: "care_type", 1: "present"},
                        program=tm.ALL_PROGRAMS)
    assert tm.matrix_check(mf.ATTACK_SERV, star, ["Москва", "Барнаул"]) == []
    # содержимое проверяется по видам помощи: колонка «видов помощи» из адресов — подозрительно
    bad = [["Адрес", "Входит"], ["г. Москва, ул. Лесная, д. 1", "да"], ["г. Барнаул, ул. Мира, д. 7", "да"]]
    assert any("видов помощи" in r for r in tm.matrix_check(bad, star, ["Москва", "Барнаул"]))


def test_wide_matrix_suggested_without_programs_and_not_for_limits():
    m, _ = tm.suggest(mf.ATTACK_WIDE, [])
    assert m == WIDE_A
    limits = [["Вид медицинской помощи", "Страховая сумма", "Франшиза"],
              ["Амбулаторно-поликлиническая помощь", "1 000 000", "10 %"],
              ["Стоматологическая помощь", "200 000", "—"]]
    got, _ = tm.suggest(limits, [])
    assert got is None or got.role != "care_matrix"
    assert tm.suggest([mf.LIST_HEADER, *mf.LIST_ROWS], [])[0].role == "clinic_list"


def test_matrix_profile_signature_without_program_names():
    """Профиль широкой матрицы подходит договору той же СК с другими программами: подпись — раскладка и роли,
    программы — из заголовков нового договора."""
    other = [["Вид медицинской помощи", "Казань", "Самара"], *[r for r in mf.ATTACK_WIDE[1:]]]
    sig = tm.mapping_signature(mf.ATTACK_WIDE, WIDE_A)
    assert sig == tm.mapping_signature(other, WIDE_A) and "москва" not in sig
    assert tm.strong_signature(sig, WIDE_A)
    named = WIDE_A.model_copy(update={"program_cols": {1: "Москва", 2: "Барнаул"}})
    prof = tm.profile_mapping(mf.ATTACK_WIDE, named)
    assert prof.program_cols == {1: "", 2: ""}
    assert {c: p for c, p in tm.wide_programs(other, prof).items()} == {1: "Казань", 2: "Самара"}


def test_matrix_profile_applies_to_contract_with_other_programs():
    tmp = ck.workdir()
    reg, st = ck.registry(tmp), ck.kit_store(tmp)
    _r, _s, cc, _rep, tables = _attack(tmp, matrix=True, reg=reg, st=st)
    lst, wide = tables[0], tables[1]
    reg.save_insurer_profile("СК Атака", tm.mapping_signature(lst[2], tm.suggest(lst[2], [])[0]),
                             tm.suggest(lst[2], [])[0])
    reg.save_insurer_profile("СК Атака", tm.mapping_signature(wide[2], WIDE_A), tm.profile_mapping(wide[2], WIDE_A))
    _r2, _s2, cc2, rep2, _t2 = _attack(tmp, matrix=False, insurer="АО «СК Атака»", legal="ООО Б", reg=reg, st=st)
    assert rep2.counts["mapped_matrices"] == 1 and rep2.readiness == "ready"
    del cc, cc2


# ---------------------------------------------------------------- 6. «нет» матрицы против клиник перечня

def test_list_conflicts_by_care_key():
    clinic = ClinicRow(id="u#t0r1", contract_id="u", doc_id="u.c", program="Москва", care_type="Стоматология",
                       clinic_name="Клиника Альфа", source_ref="u.c#t0r1")
    no = CareMatrix(contract_id="u", program="Москва", care_type="Стоматологическая помощь", present=False,
                    source_refs=["u.c#t1r2c1"])
    got = tm.list_conflicts([no], [clinic])
    assert got == [(no, ["u.c#t0r1#care"])]
    assert tm.list_conflicts([no.model_copy(update={"program": "Барнаул"})], [clinic]) == []
    care, dropped = onboarding._merge_care([], [no], [], got)
    assert dropped == 0 and sorted(m.present for m in care) == [False, True]


def test_matrix_no_vs_list_clinic_is_contradiction_not_denied():
    """Матрица: «Стоматологическая помощь» у «Барнаула» — «нет», а в перечне «Барнаула» Клиника Бета — стоматология
    (флажка derive_care нет): предупреждение в отчёте, ответ — противоречие с обоими фрагментами, не «нельзя»."""
    tmp = ck.workdir()
    _reg, st, cc, rep, _t = _attack(tmp, matrix=True)
    assert any("матрица говорит «нет», а в перечне есть клиники" in w and "Барнаул: Стоматологическая помощь" in w
               for w in rep.warnings)
    with env(DMS_ENGINE_MODE="guided"):
        a = answer("Входит ли стоматологическая помощь в программу?",
                   ChatContext(contract_id=cc.id, program="Барнаул"), llm=None, store=st)
    assert a.label is not None and a.label.value == "conditional"
    cites = {c.table_ref for s in a.statements for c in s.citations if c.verified}
    assert f"{cc.id}.contract#t1r2c2" in cites and any((r or "").endswith("#care") for r in cites)
    b = _ask(st, cc, "Можно ли сотруднику к стоматологу в Клинику Бета?", "Барнаул", "Клиника Бета")
    assert b.label is not None and b.label.value == "conditional"
