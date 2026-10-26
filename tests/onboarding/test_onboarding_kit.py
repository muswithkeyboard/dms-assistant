"""Ревизия подключения без синтетики (ADR 0011): договор комплекта c05 («Ренессанс») загружается заново как договор
клиента — счётчики разбора и ответы движка без модели совпадают с ответами по исходному договору.

Нужны data/raw/ (файлы договора), data/derived/kit.sqlite (исходный договор) и наборы ревизий
data/derived/review/review6.jsonl, review7.jsonl — иначе SKIP. Пишется только во временную папку: копия базы и
файлы клиента там же (правило данных этапа w8)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from dms_core import kit, onboarding
from dms_core.config import derived_dir
from dms_core.schemas import ChatContext
from dms_core.store import KitStore
from fixtures import client_kit as ck
from helpers import env, kit_or_skip, skip

SOURCE = "c05"
N_QUESTIONS = 15


def _questions() -> list[dict]:
    rows: list[dict] = []
    for name in ("review6.jsonl", "review7.jsonl"):
        p = derived_dir() / "review" / name
        if not p.is_file():
            skip(f"нет набора ревизии {name}")
        rows += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [r for r in rows if r.get("c") == SOURCE and not r.get("g")][:N_QUESTIONS]


def _verified(a) -> list:
    statements = list(a.statements) + [s for pc in a.per_contract for s in pc.statements]
    return [c for s in statements for c in s.citations if c.verified]


def test_kit_contract_reloaded_as_client_gives_same_answers():
    root = kit_or_skip()
    src_db = derived_dir() / "kit.sqlite"
    if not src_db.is_file():
        skip("нет data/derived/kit.sqlite")
    questions = _questions()
    from dms_core.engine import answer

    tmp = ck.workdir()
    (tmp / "derived").mkdir(parents=True)
    shutil.copyfile(src_db, tmp / "derived" / "kit.sqlite")          # копия, не ссылка: общая база не трогается
    st = KitStore(tmp / "derived" / "kit.sqlite")
    reg = ck.registry(tmp)
    g = reg.create_client_group("Клиент ревизии")
    cc = reg.add_client_contract(g.id, "Юрлицо ревизии")
    kc = next(c for grp in kit.scan(root) for c in grp.contracts if c.id == SOURCE)
    for kf in kc.files:
        ck.put_file(reg, ck.clients_root(tmp), g.id, cc.id, root / kf.path, doc_type=kf.doc_type)

    rep = onboarding.process(cc.id, store=st, registry=reg, root=ck.clients_root(tmp), run_selfcheck=False)
    assert rep.readiness == "ready" and rep.adapter == "renaissance"
    src = st.contract(SOURCE)
    assert rep.counts == {"clauses": len(st.clauses(SOURCE)),
                          "numbered": sum(1 for c in st.clauses(SOURCE) if c.number),
                          "programs": len(src.programs), "clinics": len(st.clinics(SOURCE)),
                          "care_rows": len(st.care(SOURCE)), "mapped_tables": 0, "mapped_matrices": 0}
    assert [p.name for p in st.contract(cc.id).programs] == [p.name for p in src.programs]

    with env(DMS_ENGINE_MODE="guided"):
        for r in questions:
            a1 = answer(r["q"], ChatContext(contract_id=SOURCE, program=r.get("p")), llm=None, store=st)
            a2 = answer(r["q"], ChatContext(contract_id=cc.id, program=r.get("p")), llm=None, store=st)
            c1, c2 = _verified(a1), _verified(a2)
            assert (a2.scenario, a2.label, len(c2)) == (a1.scenario, a1.label, len(c1)), r["id"]
            # ссылки — на документы своего договора: id отличаются только префиксом договора (ADR 0005)
            assert all(c.contract_id == cc.id and c.doc_id.startswith(f"{cc.id}.") for c in c2), r["id"]
            assert [c.doc_id.split(".", 1)[1] for c in c2] == [c.doc_id.split(".", 1)[1] for c in c1], r["id"]
    assert Path(st.path).parent == tmp / "derived"
