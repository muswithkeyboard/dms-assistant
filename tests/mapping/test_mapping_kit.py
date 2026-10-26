"""Ревизия разметки без синтетики (ADR 0012 п. 5): договор комплекта c06 («Ренессанс») как договор клиента с
«не распознавать формат», таблицы перечня размечены «оператором» (scripts/bench/mapping_compare.py): одна разметка
«строки заголовка нет, программа — из названия таблицы» на все таблицы программ с той же раскладкой. Строки перечня
и их программы совпадают с разбором адаптера, выбор клиники по строке разметки даёт проверенную ссылку, профиль СК
по раскладке размечает договор c07 той же СК автоматически. Нужны data/raw/ и data/derived/kit.sqlite — иначе SKIP; пишется только во
временную папку."""
from __future__ import annotations

import shutil
import sys

from dms_core import onboarding
from dms_core import table_mapping as tm
from dms_core.config import derived_dir
from dms_core.schemas import ChatContext, TableMapping
from dms_core.store import KitStore
from fixtures import client_kit as ck
from helpers import ROOT, env, kit_or_skip, skip

sys.path.insert(0, str(ROOT / "scripts" / "bench"))


def test_c06_force_generic_mapping_matches_adapter_and_profile_maps_c07():
    kit_or_skip()
    src_db = derived_dir() / "kit.sqlite"
    if not src_db.is_file():
        skip("нет data/derived/kit.sqlite")
    import mapping_compare as mc

    tmp = ck.workdir()
    (tmp / "derived").mkdir(parents=True)
    shutil.copyfile(src_db, tmp / "derived" / "kit.sqlite")          # копия: общая база не трогается
    st = KitStore(tmp / "derived" / "kit.sqlite")
    reg = ck.registry(tmp)
    croot = ck.clients_root(tmp)

    cc = mc.load_client(reg, st, croot, "c06")
    rep0 = onboarding.process(cc.id, store=st, registry=reg, root=croot, run_selfcheck=False)
    assert rep0.adapter == "generic" and rep0.counts["clinics"] == 0
    tables = mc.client_tables(reg, st, croot, cc)
    # таблицы программ без строки заголовка (Дополнение §10): экран предлагает «строки заголовка нет» и программу из
    # названия таблицы, роли — по содержимому; одна разметка + «применить ко всем с такой же раскладкой»
    counts = tm.layout_counts(tables)
    first = next(t for t in tables if t[0].doc_id == f"{cc.id}.program" and t[0].index == 1)
    layout = tm.layout_signature(first[2])
    assert layout == "layout:7:0,3,6" and counts[layout] == 27
    sugg, _ = tm.suggest(first[2], [], counts[layout])
    assert sugg == TableMapping(header_row=None, program_from_title=True,
                                columns={0: "clinic_name", 2: "by_referral", 3: "address", 6: "code"})
    same = tm.same_signature_tables(tables, first[2], sugg)
    adapter_tables = {r.id.split("#")[1].split("r")[0] for r in st.clinics("c06")}
    assert len(same) == len(adapter_tables) == 27                        # все таблицы программ договора

    maps = mc.operator_mappings(tables)
    assert {tm.ref_key(r) for r, _ in maps} == {tm.ref_key(t[0]) for t in same}
    assert {m.model_dump_json() for _, m in maps} == {sugg.model_dump_json()}   # одна разметка на все таблицы
    for ref, m in maps:
        reg.save_table_mapping(cc.id, ref, m)
    assert mc.save_profiles(reg, tables, maps, "Ренессанс (оператор)") == 1    # один профиль — по раскладке
    rep = onboarding.process(cc.id, store=st, registry=reg, root=croot, run_selfcheck=False)
    assert rep.counts["mapped_tables"] == len(maps) == 27
    cmp = mc.compare_rows(st, "c06", cc.id)
    assert cmp["mapped"] == cmp["adapter"]
    assert cmp["matched_rows"] / cmp["adapter"] >= 0.95, cmp            # название + адрес совпали
    assert cmp["matched_with_program"] / cmp["adapter"] >= 0.95, cmp    # и программа строки = название таблицы
    assert {p.name for p in st.contract(cc.id).programs} == {r.program for r in st.clinics("c06")}
    # значки «по направлению» (картинки без текста, w9-fix п. 6) — колонка by_referral: та же метка, что у адаптера
    ref_a = {(r.source_ref.split("#")[1], r.program): r.by_referral for r in st.clinics("c06")}
    same_ref = [ref_a.get((r.source_ref.split("#")[1], r.program)) == r.by_referral for r in st.clinics(cc.id)]
    assert sum(same_ref) / len(same_ref) >= 0.95 and any(r.by_referral for r in st.clinics(cc.id))

    # выбор клиники по строке разметки → ответ с проверенной ссылкой на эту строку
    # (вопрос ревизии с эталонной строкой адаптера; строка адаптера → строка разметки по названию и адресу)
    rec = next((r for r in mc._questions("c06") if isinstance(r.get("clinic_row_id"), str) and r.get("p")), None)
    if rec is None:
        skip("нет вопросов ревизии c06 с эталонной строкой (data/derived/review)")
    q, program = rec["q"], rec["p"]
    with env(DMS_ENGINE_MODE="guided"):
        from dms_core.engine import answer

        a = answer(q, ChatContext(contract_id=cc.id, program=program), llm=None, store=st)
        assert a.clinic_choices
        pick = mc.map_pick(st, "c06", cc.id, program, rec["clinic_row_id"], a.clinic_choices[0])
        assert pick is not None
        a2 = answer(q, ChatContext(contract_id=cc.id, program=program), llm=None, store=st,
                    clinic_picks={cc.id: pick})
        assert a2.clinic_choices[0].selected == pick
        cites = [c for s in a2.statements for c in s.citations if c.verified]
        assert any(c.table_ref and c.table_ref.startswith(f"{cc.id}.program#t") for c in cites)
        assert all(c.contract_id == cc.id for c in cites)

    # другой договор той же СК (c07, та же ГК): профиль по раскладке размечает все таблицы программ сам — и те
    # программы, которых нет в c06 (Петрозаводск, Воронеж…)
    # без СК в карточке — только предложение (w9-fix п. 2а); с той же СК — сам
    cc70 = mc.load_client(reg, st, croot, "c07", legal="Юрлицо 7 без СК")
    rep70 = onboarding.process(cc70.id, store=st, registry=reg, root=croot, run_selfcheck=False)
    assert rep70.counts["mapped_tables"] == 0 and any("применён не автоматически" in w for w in rep70.warnings)
    cc7 = mc.load_client(reg, st, croot, "c07", legal="Юрлицо 7", insurer_name="Ренессанс (оператор)")
    rep7 = onboarding.process(cc7.id, store=st, registry=reg, root=croot, run_selfcheck=False)
    adapter7 = {r.id.split("#")[1].split("r")[0] for r in st.clinics("c07")}
    assert rep7.adapter == "generic" and rep7.counts["mapped_tables"] == len(adapter7) == 25
    # предупреждение сводное: одно на профиль, не по два на таблицу (w9-fix п. 6)
    assert sum("Применена разметка СК «Ренессанс (оператор)» к таблицам: 25" in w for w in rep7.warnings) == 1
    assert not any("Применена разметка СК" in w and "к таблице «" in w for w in rep7.warnings)
    cmp7 = mc.compare_rows(st, "c07", cc7.id)
    assert cmp7["mapped"] == cmp7["adapter"], cmp7
    assert cmp7["matched_with_program"] / cmp7["adapter"] >= 0.95, cmp7     # 25.10.2026: 1802 / 1802
    assert [p.used for p in reg.insurer_profiles()] == [1]                 # по договорам, не по таблицам
