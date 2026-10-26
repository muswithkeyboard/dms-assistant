"""Помощники тестов этапа w2-verify: синтетическая ГК из двух почти одинаковых договоров (tests/fixtures/synth_kit.py)."""
from __future__ import annotations

from dms_core.schemas import Clause, ClinicRow, DraftAnswer, DraftCitation, DraftStatement
from dms_core.store import KitStore
from fixtures.synth_kit import COMMON, make_group_docs, make_parsed

COMMON_TEXT = dict(COMMON)


def store(*, c03_without_clinics: bool = False) -> KitStore:
    st = KitStore(":memory:")
    st.save(make_parsed("c01"))
    st.save(make_parsed("c02"))
    if c03_without_clinics:
        p = make_parsed("c03")
        p.documents = [d for d in p.documents if d.doc_type.value != "clinics"]
        p.clinics = []
        p.care = []
        st.save(p)
    g, docs, cls = make_group_docs()
    st.save_group(g, docs, cls)
    return st


def clause(st: KitStore, cid: str, number: str, program: str | None = None) -> Clause:
    found = [c for c in st.clause(cid, number=number) if program is None or c.program == program]
    assert found, (cid, number)
    return found[0]


def row(st: KitStore, cid: str, care_word: str) -> ClinicRow:
    return next(r for r in st.clinics(cid) if care_word in r.care_type)


def cite(c: Clause, quote: str | None = None) -> DraftCitation:
    return DraftCitation(ref=c.id, quote=quote if quote is not None else c.text)


def stmt(text: str, *cits: DraftCitation, kind: str = "fact") -> DraftStatement:
    return DraftStatement(text=text, citations=list(cits), kind=kind)


def draft(cid: str, *statements: DraftStatement, **kw) -> DraftAnswer:
    return DraftAnswer(contract_id=cid, summary=kw.pop("summary", f"вывод {cid}"), statements=list(statements), **kw)
