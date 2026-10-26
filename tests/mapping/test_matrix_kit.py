"""Матрица по разметке на настоящем договоре (ADR 0013): c06 («Ренессанс») как договор клиента с «не распознавать
формат», перечни программ размечены «оператором» (scripts/bench/mapping_compare.py) с флажком «Виды помощи из этого
перечня → матрица» (derive_care). Договор — ready; пары матрицы — пары адаптера с present=True (адаптер дополнительно
ставит present=False по тексту программы — по перечню «нет» не получить); с выбором клиники вопрос о целом виде
помощи даёт ту же метку, что исходный договор, care_in_program = yes по матрице разметки, ссылка проходит verify.
Нужны data/raw/ и data/derived/kit.sqlite — иначе SKIP; пишется только во временную папку."""
from __future__ import annotations

import shutil
import sys

from dms_core import engine, onboarding
from dms_core.config import derived_dir
from dms_core.schemas import ChatContext
from dms_core.store import KitStore
from fixtures import client_kit as ck
from helpers import ROOT, env, kit_or_skip, skip

sys.path.insert(0, str(ROOT / "scripts" / "bench"))

QUESTIONS = [("Москва", "Врач на дом", "Можно ли вызвать врача на дом из клиники «{n}»?"),
             ("Москва", "Поликлиника", "Можно ли сотруднику записаться в поликлинику «{n}»?"),
             ("Барнаул", "Врач на дом", "Можно ли вызвать врача на дом из клиники «{n}»?")]


def test_c06_derive_care_ready_pairs_and_labels_match_source():
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
    onboarding.process(cc.id, store=st, registry=reg, root=croot, run_selfcheck=False)
    maps = mc.operator_mappings(mc.client_tables(reg, st, croot, cc), derive=True)
    assert maps and all(m.derive_care for _r, m in maps)
    for ref, m in maps:
        reg.save_table_mapping(cc.id, ref, m)
    rep = onboarding.process(cc.id, store=st, registry=reg, root=croot, run_selfcheck=False)
    assert rep.readiness == "ready" and rep.counts["mapped_matrices"] == 27

    pairs = mc.compare_care(st, "c06", cc.id)
    # все пары разметки — пары адаптера с тем же present; не хватает только «нет» и «есть по тексту» адаптера
    assert pairs["mapped"] == pairs["pairs_exact"] == pairs["present_equal"] == pairs["mapped_present"]
    assert pairs["mapped_only_code"] == 0 and pairs["adapter"] - pairs["mapped"] <= 20

    with env(DMS_ENGINE_MODE="guided"):
        for program, care, q in QUESTIONS:
            row = next(r for r in st.clinics("c06", program=program)
                       if care in [p.strip() for p in r.care_type.split("|")])
            copy = next(r for r in st.clinics(cc.id, program=program) if r.source_ref.split("#", 1)[1] ==
                        row.source_ref.split("#", 1)[1])
            text = q.format(n=row.clinic_name)
            a1 = engine.answer(text, ChatContext(contract_id="c06", program=program), llm=None, store=st,
                               clinic_picks={"c06": row.id})
            mc.CAPTURE.pop(cc.id, None)
            a2 = engine.answer(text, ChatContext(contract_id=cc.id, program=program), llm=None, store=st,
                               clinic_picks={cc.id: copy.id})
            assert a1.label == a2.label, (program, care, a1.label, a2.label)
            checks = mc.CAPTURE[cc.id]["verified"].draft.checks
            src = mc.CAPTURE["c06"]["verified"].draft.checks
            assert checks.care_in_program == src.care_in_program == "yes", (program, care, checks)
            assert checks.clinic_in_program == src.clinic_in_program, (program, care, checks, src)
            cites = [c for s in a2.statements for c in s.citations if c.verified]
            assert any(c.table_ref and c.table_ref.endswith("#care") and c.quote.startswith(f"{program}: ")
                       for c in cites)
    shutil.rmtree(tmp, ignore_errors=True)


def test_c10_derive_care_gives_allowed_where_source_allowed():
    """СОГАЗ c10: лист перечня с флажком derive_care; вопросы ревизии, где исходный договор и эталон — «можно», с
    эталонным выбором клиники у копии — «можно» или «при условиях» (ложных «можно» / «нельзя» нет — mapping_compare)."""
    kit_or_skip()
    src_db = derived_dir() / "kit.sqlite"
    if not src_db.is_file():
        skip("нет data/derived/kit.sqlite")
    import mapping_compare as mc
    from review_eval import simulated_picks

    qs = {r["id"]: r for r in mc._questions("c10")}
    if "r6-086" not in qs:
        skip("нет data/derived/review/review6.jsonl")
    tmp = ck.workdir()
    (tmp / "derived").mkdir(parents=True)
    shutil.copyfile(src_db, tmp / "derived" / "kit.sqlite")
    st = KitStore(tmp / "derived" / "kit.sqlite")
    reg = ck.registry(tmp)
    croot = ck.clients_root(tmp)
    cc = mc.load_client(reg, st, croot, "c10")
    onboarding.process(cc.id, store=st, registry=reg, root=croot, run_selfcheck=False)
    for ref, m in mc.operator_mappings(mc.client_tables(reg, st, croot, cc), derive=True):
        reg.save_table_mapping(cc.id, ref, m)
    rep = onboarding.process(cc.id, store=st, registry=reg, root=croot, run_selfcheck=False)
    assert rep.readiness == "ready" and rep.counts["care_rows"] > 0
    with env(DMS_ENGINE_MODE="guided"):
        for qid in ("r6-086", "r6-089"):
            r = qs[qid]
            ctx = ChatContext(contract_id="c10", program=r["p"])
            a0 = engine.answer(r["q"], ctx, llm=None, store=st)
            pick = simulated_picks(r, a0.clinic_choices)["c10"][0]
            a1 = engine.answer(r["q"], ctx, llm=None, store=st, clinic_picks={"c10": pick})
            assert a1.label is not None and a1.label.value == "allowed"
            ctx2 = ChatContext(contract_id=cc.id, program=r["p"])
            b0 = engine.answer(r["q"], ctx2, llm=None, store=st)
            pick2 = mc.map_pick(st, "c10", cc.id, r["p"], pick, b0.clinic_choices[0])
            b = engine.answer(r["q"], ctx2, llm=None, store=st, clinic_picks={cc.id: pick2})
            # w10-fix2 (ревизия): строка неразмеченной таблицы без программы при ≥ 2 программах договора — условие,
            # а не «входит» (защита от «можно» по таблице чужой программы). У СОГАЗ таблица услуг t7 общая для обеих
            # программ, поэтому копия осторожнее исходного: «можно» или «при условиях», но не «нельзя» и не другое.
            assert b.label is not None and b.label.value in ("allowed", "conditional"), (qid, b.label)
    shutil.rmtree(tmp, ignore_errors=True)
