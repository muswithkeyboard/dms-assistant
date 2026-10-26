"""Разметка в конвейере подключения (ADR 0012): onboarding.process применяет разметку договора и профили СК, строки
перечня работают в движке (выбор клиники ADR 0010, «нет в перечне»), ссылки на них проходят citations.verify.
Свои маленькие docx; всё — во временной папке."""
from __future__ import annotations

from dms_core import onboarding
from dms_core import table_mapping as tm
from dms_core.citations import verify
from dms_core.constants import CLINIC_PICK_NONE
from dms_core.schemas import ChatContext, DraftAnswer, DraftCitation, DraftStatement, TableMapping
from fixtures import client_kit as ck
from fixtures import mapping_fixture as mf
from helpers import env

MAPPING = TableMapping(header_row=0, columns={1: "clinic_name", 2: "address", 3: "care_type", 4: "franchise_pct",
                                              5: "by_referral"}, program="Стандарт")


def _setup(tmp, legal="ООО Ромашка", group=None, **kw):
    reg = ck.registry(tmp)
    g = group or reg.create_client_group("Клиент М")
    cc = reg.add_client_contract(g.id, legal, **kw)
    src = mf.contract_docx(tmp / "src" / f"Договор {cc.id}.docx")
    ck.put_file(reg, ck.clients_root(tmp), g.id, cc.id, src, doc_type="contract")
    return reg, g, cc


def _tables(reg, st, tmp, cc):
    kc = onboarding.kit_contract(reg.client_group(cc.group_id), cc, reg.client_files(cc.id))
    return tm.list_tables(kc, ck.clients_root(tmp), {d.source_path: d.id for d in st.documents(cc.id)})


def _process(reg, st, tmp, cc):
    return onboarding.process(cc.id, store=st, registry=reg, root=ck.clients_root(tmp), run_selfcheck=False)


def test_mapping_applied_in_process_and_counts():
    tmp = ck.workdir()
    reg, _g, cc = _setup(tmp)
    st = ck.kit_store(tmp)
    rep0 = _process(reg, st, tmp, cc)
    assert rep0.adapter == "generic" and rep0.counts["clinics"] == 0 and rep0.counts["mapped_tables"] == 0
    (ref, _t, _rows), = _tables(reg, st, tmp, cc)
    reg.save_table_mapping(cc.id, ref, MAPPING)
    rep = _process(reg, st, tmp, cc)
    assert rep.readiness == "limited" and rep.counts["mapped_tables"] == 1 and rep.counts["clinics"] == 6
    assert rep.counts["programs"] == 1 and [p.name for p in st.contract(cc.id).programs] == ["Стандарт"]
    assert any(w.startswith("Применена разметка договора") for w in rep.warnings)
    assert not any("перечня клиник нет" in w for w in rep.warnings)
    assert {r.id for r in st.clinics(cc.id)} >= {"u01.contract#t1r2#1", "u01.contract#t1r8"}


def test_mapped_rows_pass_citation_verify():
    tmp = ck.workdir()
    reg, _g, cc = _setup(tmp)
    st = ck.kit_store(tmp)
    _process(reg, st, tmp, cc)
    (ref, _t, _r), = _tables(reg, st, tmp, cc)
    reg.save_table_mapping(cc.id, ref, MAPPING)
    _process(reg, st, tmp, cc)
    rid = "u01.contract#t1r4"

    def draft(quote):
        return DraftAnswer(contract_id=cc.id, summary="", statements=[DraftStatement(
            text="Клиника Медцентр Василёк есть в перечне программы Стандарт",
            citations=[DraftCitation(ref=rid, quote=quote)])])

    ok = verify(draft("Медцентр Василёк"), {rid}, st, cc.id, program="Стандарт")
    assert list(ok.citations) == [rid] and ok.citations[rid].verified and ok.citations[rid].table_ref == rid
    bad = verify(draft("Клиника Тверца"), {rid}, st, cc.id, program="Стандарт")
    assert not bad.citations and bad.dropped                 # цитата — не название клиники этой строки


def test_guided_clinic_pick_and_none_on_mapped_list():
    from dms_core.engine import answer

    tmp = ck.workdir()
    reg, _g, cc = _setup(tmp)
    st = ck.kit_store(tmp)
    _process(reg, st, tmp, cc)
    (ref, _t, _r), = _tables(reg, st, tmp, cc)
    reg.save_table_mapping(cc.id, ref, MAPPING)
    _process(reg, st, tmp, cc)
    q = "Можно ли сотруднику на приём терапевта в клинику «Медцентр Василёк»?"
    ctx = ChatContext(contract_id=cc.id, program="Стандарт")
    with env(DMS_ENGINE_MODE="guided"):
        a = answer(q, ctx, llm=None, store=st)
        assert a.clinic_choices and any(r.id == "u01.contract#t1r4" for r in a.clinic_choices[0].candidates)
        a2 = answer(q, ctx, llm=None, store=st, clinic_picks={cc.id: "u01.contract#t1r4"})
        cites = [c for s in a2.statements for c in s.citations if c.verified]
        assert any(c.table_ref == "u01.contract#t1r4" for c in cites)
        assert a2.label is None or a2.label.value != "allowed"      # матрицы нет — «можно» не ставится
        a3 = answer("Можно ли на приём терапевта в клинику «Клиника Несуществующая-Звезда»?", ctx, llm=None,
                    store=st, clinic_picks={cc.id: CLINIC_PICK_NONE})
        assert a3.clinic_choices and a3.clinic_choices[0].selected == CLINIC_PICK_NONE


def test_profile_auto_applied_to_other_contract_and_used_counter():
    tmp = ck.workdir()
    reg, g, cc = _setup(tmp)
    st = ck.kit_store(tmp)
    _process(reg, st, tmp, cc)
    (_ref, _t, rows), = _tables(reg, st, tmp, cc)
    p = reg.save_insurer_profile("СК Тест", tm.signature(rows, MAPPING.header_row), MAPPING)
    assert p.id == "ip1"
    # у договора не указана СК — профиль только предлагается (w9-fix п. 2а), не применяется сам
    _reg, _g2, cc0 = _setup(tmp, legal="ООО Без СК", group=g)
    rep0 = _process(reg, st, tmp, cc0)
    assert rep0.counts["mapped_tables"] == 0 and any("применён не автоматически" in w and "не указана СК" in w
                                                      for w in rep0.warnings)
    # другая СК — тоже только предложение
    _reg, _g3, cc3 = _setup(tmp, legal="ООО Другая", group=g, insurer_name="СК Другая")
    assert _process(reg, st, tmp, cc3).counts["mapped_tables"] == 0
    # та же СК (без регистра, кавычек и слова «страхование») — сам
    _reg, _g2, cc2 = _setup(tmp, legal="ООО Лютик", group=g, insurer_name="АО «ск тест страхование»")
    rep = _process(reg, st, tmp, cc2)
    assert rep.counts["mapped_tables"] == 1 and rep.counts["clinics"] == 6
    assert any("Применена разметка СК «СК Тест» к таблицам: 1" in w for w in rep.warnings)
    assert {r.contract_id for r in st.clinics(cc2.id)} == {cc2.id}            # строки — свои у договора
    assert reg.insurer_profile("ip1").used == 1
    _process(reg, st, tmp, cc2)                      # повторная обработка того же договора — счётчик тот же
    assert reg.insurer_profile("ip1").used == 1
    # профиль хранит только разметку, не данные документа
    assert "Ромашка" not in reg.insurer_profile("ip1").model_dump_json()


def test_force_generic_and_mapping_ignored_for_known_insurer():
    tmp = ck.workdir()
    reg = ck.registry(tmp)
    g = reg.create_client_group("Клиент Р")
    cc = reg.add_client_contract(g.id, "ООО Р", force_generic=True)
    for f in ck.ren_files(tmp):
        ck.put_file(reg, ck.clients_root(tmp), g.id, cc.id, f)
    st = ck.kit_store(tmp)
    rep = _process(reg, st, tmp, cc)
    assert rep.adapter == "generic" and rep.counts["clinics"] == 0
    cc2 = reg.add_client_contract(g.id, "ООО Р2")
    for f in ck.ren_files(tmp):
        ck.put_file(reg, ck.clients_root(tmp), g.id, cc2.id, f)
    _process(reg, st, tmp, cc2)
    reg.save_table_mapping(cc2.id, tm.TableRef(doc_id=f"{cc2.id}.program", kind="docx_table", index=0),
                           TableMapping(columns={0: "clinic_name"}))
    rep2 = _process(reg, st, tmp, cc2)
    assert rep2.adapter == "renaissance" and rep2.counts["mapped_tables"] == 0
    assert any("разметка таблиц не применялась" in w for w in rep2.warnings)


def test_migration_keeps_registry_and_mapping_roundtrip():
    tmp = ck.workdir()
    reg = ck.registry(tmp)
    assert reg.migrate() == 5
    ref = tm.TableRef(doc_id="u01.clinics", kind="xlsx_sheet", sheet="Лист 1")
    reg.save_table_mapping("u01", ref, MAPPING)
    reg.save_table_mapping("u01", ref, MAPPING.model_copy(update={"program": "Другая"}))
    assert [(r, m.program) for r, m in reg.table_mappings("u01")] == [(ref, "Другая")]
    assert reg.delete_table_mapping("u01", ref) and reg.table_mappings("u01") == []
    a = reg.save_insurer_profile("СК", "подпись", MAPPING)
    b = reg.save_insurer_profile("СК 2", "подпись", MAPPING)           # та же подпись — обновление
    assert a.id == b.id and len(reg.insurer_profiles()) == 1 and b.insurer_name == "СК 2"
    assert reg.delete_insurer_profile(a.id) and not reg.delete_insurer_profile(a.id)
