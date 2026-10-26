"""citations.verify на разобранном комплекте (data/derived/kit.sqlite): своя цитата жива, тот же текст чужого
договора ГК — отброс «чужой договор». Без базы — SKIP (`docs/rules.md` п. 4, 9)."""
from __future__ import annotations

import time

from dms_core import citations as C
from dms_core.schemas import DraftAnswer, DraftCitation, DraftStatement
from dms_core.store import KitStore
from dms_core.store.db import default_path
from helpers import skip


def _kit() -> KitStore:
    if not default_path().is_file():
        skip("нет data/derived/kit.sqlite")
    return KitStore(default_path())


def test_kit_own_quote_live_and_twin_foreign():
    st = _kit()
    cids = [c.id for c in st.contracts()]
    by_hash: dict[str, list] = {}
    for cid in cids:
        for c in st.clauses(cid):
            if len(c.text) >= 60:
                by_hash.setdefault(c.text_hash, []).append(c)
    twins = next((v for v in by_hash.values() if len({c.contract_id for c in v}) > 1), None)
    if twins is None:
        skip("в базе нет одинаковых пунктов разных договоров")
    own = twins[0]
    other = next(c for c in twins if c.contract_id != own.contract_id)
    quote = " ".join(own.text.split()[:8])
    d = DraftAnswer(contract_id=own.contract_id, summary="s", statements=[
        DraftStatement(text=quote, citations=[DraftCitation(ref=own.id, quote=quote)]),
        DraftStatement(text=quote, citations=[DraftCitation(ref=other.id, quote=quote)]),
    ])
    t0 = time.perf_counter()
    v = C.verify(d, {own.id, other.id}, st, own.contract_id)
    assert time.perf_counter() - t0 < 10
    assert len(v.draft.statements) == 1 and v.citations[own.id].contract_id == own.contract_id
    assert C.foreign_refs(v) == [other.id]
