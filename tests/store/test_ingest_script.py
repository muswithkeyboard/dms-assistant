"""scripts/ingest_kit.py на синтетике: статусы документов, пропуск без адаптера, ошибка разбора, отчёт."""
from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path

from dms_core.kit import KitContract, KitFile, KitGroup
from fixtures.synth_kit import make_group_docs, make_parsed
from helpers import ROOT


def _load_script():
    spec = importlib.util.spec_from_file_location("ingest_kit", ROOT / "scripts" / "ingest_kit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _kf(path: str) -> KitFile:
    return KitFile(path=path, title=path, fmt="docx", doc_type="contract", scope="contract", size=1, sha256="0")


def test_ingest_synthetic_kit():
    mod = _load_script()
    group = KitGroup(id="gk9", name="ГК 9", files=[_kf("rules.docx")], contracts=[
        KitContract(id=cid, group_id="gk9", legal_entity=cid, files=[_kf(f"synth/{cid}/contract.docx"), _kf(f"synth/{cid}/memo.pdf")])
        for cid in ("c01", "c02", "c03", "c04")
    ])

    def fake_parse(kc, root):
        if kc.id == "c03":
            raise RuntimeError("нет адаптера generic для договора c03")
        if kc.id == "c04":
            raise ValueError("битый файл")
        p = make_parsed(kc.id)
        if kc.id == "c02":                       # документ без пунктов и строк — полнота не подтверждена
            p.clinics = []
        return p

    def fake_group(g, root):
        _, docs, cls = make_group_docs(g.id)
        return docs, cls

    saved = (mod.kit.scan, mod.adapters.parse_contract, getattr(mod.adapters, "parse_group_docs", None))
    mod.kit.scan = lambda root: [group]
    mod.adapters.parse_contract = fake_parse
    mod.adapters.parse_group_docs = fake_group
    try:
        with tempfile.TemporaryDirectory() as tmp:
            db, rep = Path(tmp) / "kit.sqlite", Path(tmp) / "ingest_report.json"
            rc = mod.main(["--root", tmp, "--db", str(db), "--report", str(rep)])
            report = json.loads(rep.read_text(encoding="utf-8"))
    finally:
        mod.kit.scan, mod.adapters.parse_contract = saved[0], saved[1]
        if saved[2] is None:
            del mod.adapters.parse_group_docs
        else:
            mod.adapters.parse_group_docs = saved[2]
    assert rc == 1                                            # c04 — error
    cs = report["contracts"]
    assert [cs[c]["status"] for c in ("c01", "c02", "c03", "c04")] == ["ok", "ok", "skipped", "error"]
    assert {d["status"] for d in cs["c01"]["documents"]} == {"ready"}
    st02 = {d["doc_id"]: d for d in cs["c02"]["documents"]}
    assert st02["c02.clinics"]["status"] == "extracted" and st02["c02.clinics"]["warnings"]
    # у c02 премии не сходятся с графиком взносов: договор не ready, а checked с предупреждением в отчёте
    assert st02["c02.contract"]["status"] == "checked"
    assert any("≠" in w for w in st02["c02.contract"]["warnings"])
    assert st02["c02.program"]["status"] == "ready"
    # сбой разбора — все файлы договора в отчёте со статусом error
    assert {d["status"] for d in cs["c04"]["documents"]} == {"error"} and len(cs["c04"]["documents"]) == 2
    assert report["statuses"]["contracts"] == {"ready": 4, "extracted": 1, "checked": 1, "error": 2}
    assert report["statuses"]["groups"] == {"ready": 1}
    assert report["groups"]["gk9"]["documents"][0]["status"] == "ready"
    assert cs["c01"]["clauses_numbered"] == cs["c01"]["clauses"] == 7
    assert cs["c01"]["clinics"] == 3 and cs["c01"]["programs"] == ["Стандарт"]
    assert cs["c01"]["premium"] == {"premium_sum": 500_000.0, "premium_sum_complete": True,
                                    "payments_sum": 500_000.0, "match": True}
    assert cs["c02"]["premium"]["match"] is False             # 420 000 ≠ 500 000
    assert cs["c01"]["files_not_parsed"] == ["synth/c01/memo.pdf"]   # файл описи без документа
    assert report["groups"]["gk9"]["documents"][0]["doc_id"] == "gk9.rules"
    assert report["dedup"]["chars_saved"] > 0 and report["db_bytes"] > 0


def test_premium_check_incomplete():
    mod = _load_script()
    c = make_parsed("c01").contract
    c.programs[0].persons = None
    assert mod._premium_check(c)["match"] is None


def _statuses(mutate) -> dict[str, tuple[str, list[str]]]:
    """Статусы документов синтетического договора c01 после _mark_statuses (mutate правит ParsedContract)."""
    from dms_core.store import KitStore

    mod = _load_script()
    p = make_parsed("c01")
    mutate(p)
    with tempfile.TemporaryDirectory() as tmp, KitStore(Path(tmp) / "kit.sqlite") as store:
        store.save(p)
        mod._mark_statuses(store, p, mod._premium_check(p.contract))
        return {d.id: (d.status.value, d.warnings) for d in store.documents("c01")}


def test_adapter_reconciliation_error_keeps_document_checked():
    def add_errors(p):
        p.warnings += ["c01: Σ премий п. 2.3 = 1 ≠ п. 2.4 = 2",                       # карточка → договор
                       "c01.clinics: нет перечня клиник для программ ['Премиум']",     # конкретный документ
                       "c01: п. 4.1.2 договора содержит два взаимоисключающих варианта"]  # находка, не ошибка
    st = _statuses(add_errors)
    assert st["c01.contract"][0] == "checked" and any("≠" in w for w in st["c01.contract"][1])
    assert st["c01.clinics"][0] == "checked" and any("нет перечня" in w for w in st["c01.clinics"][1])
    assert st["c01.program"] == ("ready", [])


def test_completeness_by_doc_type():
    def strip(p):
        for c in p.clauses:
            if c.doc_id == "c01.contract":
                c.number = None                  # договор без пунктов с номером — полнота не подтверждена
        p.clauses = [c for c in p.clauses if c.doc_id != "c01.program"]
        p.contract.number = None
    st = _statuses(strip)
    assert st["c01.contract"][0] == "extracted" and "пункты с номером" in st["c01.contract"][1][0]
    assert st["c01.program"][0] == "extracted"
    assert st["c01.clinics"] == ("ready", [])


def test_card_without_number_is_checked_not_ready():
    def no_number(p):
        p.contract.number = None
    st = _statuses(no_number)
    assert st["c01.contract"][0] == "checked" and "номер договора" in st["c01.contract"][1][0]
