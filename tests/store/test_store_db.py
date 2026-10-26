"""KitStore: идемпотентное сохранение, обязательный contract_id, изоляция договоров, дедуп (ADR 0005)."""
from __future__ import annotations

import tempfile
from pathlib import Path

from dms_core.constants import DocStatus
from dms_core.store import KitStore
from fixtures.synth_kit import COMMON, make_group_docs, make_parsed
from helpers import ROOT  # noqa: F401 — настраивает sys.path


def _store() -> KitStore:
    st = KitStore(":memory:")
    st.save(make_parsed("c01"))
    st.save(make_parsed("c02"))
    g, docs, cls = make_group_docs()
    st.save_group(g, docs, cls)
    return st


def _raises(exc: type[BaseException], fn, *a, **kw) -> None:
    try:
        fn(*a, **kw)
    except exc:
        return
    raise AssertionError(f"ожидалось {exc.__name__}")


def test_reads_require_contract_id():
    st = _store()
    for fn in (st.contract, st.documents, st.clauses, st.clinics, st.care, st.revision, st.contract_warnings):
        _raises(ValueError, fn, None)
        _raises(ValueError, fn, "")
    _raises(ValueError, st.clause, None, number="1.1")
    _raises(ValueError, st.clause, "c01")          # ни номера, ни пути


def test_isolation_every_read_returns_only_own_contract():
    st = _store()
    for cid in ("c01", "c02"):
        assert st.contract(cid).id == cid
        assert {d.contract_id for d in st.documents(cid)} == {cid}
        assert {c.contract_id for c in st.clauses(cid)} == {cid}
        assert all(c.doc_id.startswith(cid + ".") for c in st.clauses(cid))
        assert {r.contract_id for r in st.clinics(cid)} == {cid}
        assert {m.contract_id for m in st.care(cid)} == {cid}
        for c in st.clause(cid, number="3.1"):
            assert c.contract_id == cid
        # одноимённая программа «Стандарт» есть в обоих — фильтр по программе не смешивает договоры
        assert {r.contract_id for r in st.clinics(cid, program="Стандарт")} == {cid}
    assert st.clinics("c01")[0].franchise_pct == 10.0
    assert st.clinics("c02")[0].franchise_pct == "—"


def test_group_docs_stored_but_not_in_contract_reads():
    st = _store()
    assert [d.id for d in st.group_documents("gk9")] == ["gk9.rules"]
    for cid in ("c01", "c02"):
        assert "gk9.rules" not in {d.id for d in st.documents(cid)}
        assert all(c.contract_id == cid for c in st.clauses(cid))
    _raises(ValueError, st.group_documents, "")


def test_save_is_idempotent_and_replaces_contract():
    st = _store()
    n = len(st.clauses("c01"))
    rev = st.revision("c01")
    st.save(make_parsed("c01"))
    assert len(st.clauses("c01")) == n
    assert st.revision("c01") != rev
    st.save(make_parsed("c01", extra=5))
    assert len(st.clauses("c01")) == n + 5
    st.save(make_parsed("c01"))
    assert len(st.clauses("c01")) == n
    assert len(st.clauses("c02")) == n                         # соседний договор не тронут
    assert [c.id for c in st.contracts()] == ["c01", "c02"]
    assert [g.id for g in st.groups()] == ["gk9"]


def test_save_rejects_foreign_rows():
    st = KitStore(":memory:")
    bad = make_parsed("c01")
    bad.clinics[0] = bad.clinics[0].model_copy(update={"contract_id": "c02"})
    _raises(ValueError, st.save, bad)
    bad = make_parsed("c01")
    bad.clauses[0] = bad.clauses[0].model_copy(update={"doc_id": "c02.contract"})
    _raises(ValueError, st.save, bad)
    bad = make_parsed("c01")
    bad.documents[0] = bad.documents[0].model_copy(update={"contract_id": None})
    _raises(ValueError, st.save, bad)
    bad = make_parsed("c01")
    bad.care[0] = bad.care[0].model_copy(update={"contract_id": "c02"})
    _raises(ValueError, st.save, bad)
    assert st.contracts() == []                                 # ничего не записалось


def test_dedup_texts_stored_once_links_stay_own():
    st = _store()
    stats = st.dedup_stats()
    # общие пункты одинаковы в c01 и c02 — хранятся один раз
    assert stats.unique_texts < stats.clauses
    assert stats.chars_saved >= sum(len(t) for _, t in COMMON)
    a = {c.text: c for c in st.clauses("c01")}
    b = {c.text: c for c in st.clauses("c02")}
    for _, text in COMMON:
        assert a[text].doc_id == "c01.contract" and b[text].doc_id == "c02.contract"
    st.delete_contract("c02")
    after = st.dedup_stats()
    assert after.clauses == len(st.clauses("c01")) + 1           # +1 пункт правил ГК
    assert after.unique_texts == after.clauses                   # осиротевшие тексты удалены


def test_clause_by_number_and_path():
    st = _store()
    assert len(st.clause("c01", number="3.1")) == 2               # номер повторяется в программах
    hit = st.clause("c01", path="Программа «Стандарт» › Стоматология › 3.1")
    assert len(hit) == 1 and "AIR FLOW" in hit[0].text
    assert len(st.clause("c01", path="Стоматология › 3.1")) == 1  # по окончанию пути
    assert st.clause("c01", path="%") == []                      # спецсимволы LIKE экранированы


def test_clauses_filter_by_doc_types_and_clinic_filters():
    st = _store()
    assert {c.doc_id for c in st.clauses("c01", doc_types=["contract"])} == {"c01.contract"}
    assert st.clauses("c01", doc_types=[]) == []
    assert len(st.clinics("c01", care_type="Помощь на дому")) == 1
    assert len(st.clinics("c01", by_referral=True)) == 1
    assert len(st.clinics("c01", city="Москва")) == 3
    assert st.clinics("c01", franchise_pct=10.0) and not st.clinics("c02", franchise_pct=10.0)
    _raises(TypeError, st.clinics, "c01", nonsense=1)
    assert [m.care_type for m in st.care("c01", program="Стандарт")] == ["dental", "inpatient"]


def test_doc_status_and_file_persistence():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sub" / "kit.sqlite"
        with KitStore(path) as st:
            st.save(make_parsed("c01"))
            st.set_doc_status("c01.program", DocStatus.ready, warning="проверено")
            _raises(KeyError, st.set_doc_status, "nope", DocStatus.ready)
            _raises(KeyError, st.contract, "c99")
            assert st.size_bytes() > 0
        with KitStore(path) as st:
            doc = {d.id: d for d in st.documents("c01")}["c01.program"]
            assert doc.status is DocStatus.ready and doc.warnings == ["проверено"]
            assert st.contract("c01").programs[0].name == "Стандарт"


def _with_shared_block(cid: str):
    """Договор + общий блок «Стандарт, Бизнес» из таблицы (id <doc_id>#t<т>r<с>c<я>l<строка>) и строка клиник
    с двумя видами помощи (id <source_ref>#<k>) — docs/CONTRACTS.md §1."""
    from dms_core.adapters.base import text_hash
    from dms_core.schemas import CareMatrix, Clause

    p = make_parsed(cid)
    text = "Общие для вариантов Стандарт и Бизнес: телемедицинская консультация врача круглосуточно."
    p.clauses.append(Clause(id=f"{cid}.program#t2r3c1l0", doc_id=f"{cid}.program", contract_id=cid, number=None,
                            path="Программа › Общие для вариантов", program="Стандарт, Бизнес", text=text,
                            text_hash=text_hash(text)))
    base = p.clinics[0]
    for k, care in enumerate(["Амбулаторно-поликлиническая помощь", "Телемедицина"]):
        p.clinics.append(base.model_copy(update={"id": f"{cid}.clinics#t0r9#{k}", "program": "Стандарт, Бизнес",
                                                 "care_type": care, "source_ref": "t0:r9"}))
    p.care.append(CareMatrix(contract_id=cid, program="Стандарт, Бизнес", care_type="telemed", present=True,
                             source_refs=["t2:r3"]))
    return p


def test_program_list_filter_matches_any_program():
    from dms_core.store.db import program_list, program_matches

    assert program_list("Стандарт, Бизнес") == ["Стандарт", "Бизнес"] and program_list(None) == []
    assert program_matches("Стандарт, Бизнес", "Бизнес") and not program_matches("Стандарт, Бизнес", "Стан")
    st = KitStore(":memory:")
    st.save(_with_shared_block("c01"))
    st.save(_with_shared_block("c02"))
    got = st.clause("c01", path="Программа › Общие для вариантов")
    assert [c.id for c in got] == ["c01.program#t2r3c1l0"]
    biz = st.clinics("c01", program="Бизнес")
    assert [r.id for r in biz] == ["c01.clinics#t0r9#0", "c01.clinics#t0r9#1"]
    assert {r.source_ref for r in biz} == {"t0:r9"} and {r.contract_id for r in biz} == {"c01"}
    assert len(st.clinics("c01", program="Стандарт")) == 5          # 3 своих + 2 из общего блока
    assert [m.care_type for m in st.care("c01", program="Бизнес")] == ["telemed"]
    assert st.clinics("c01", program="Премиум") == []
