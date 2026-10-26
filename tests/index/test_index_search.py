"""Поиск: морфология, BM25, синонимы видов помощи, изоляция договоров, производительность на 5 000 пунктов."""
from __future__ import annotations

import time

from dms_core.index.bm25 import BM25
from dms_core.index.morph import lemma, lemmas, tokens
from dms_core.index.search import SYNONYM_WEIGHT, expand_query, get_index, search_clauses
from dms_core.store import KitStore
from fixtures.synth_kit import make_group_docs, make_parsed


def _store(extra: int = 0) -> KitStore:
    st = KitStore(":memory:")
    st.save(make_parsed("c01", extra=extra))
    st.save(make_parsed("c02", extra=extra))
    g, docs, cls = make_group_docs()
    st.save_group(g, docs, cls)
    return st


def test_morph_lemmas_and_tokens():
    assert lemmas("чистка зубов") == ["чистка", "зуб"]
    assert lemma("налёта") == "налет" and lemma("налета") == "налет"     # ё → е
    assert tokens("AIR-FLOW п. 2.6.1") == ["air", "flow", "п", "2.6.1"]
    assert "и" not in lemmas("лечение и удаление")
    assert lemma("mrt") == "mrt"


def test_bm25_ranks_and_filters():
    bm = BM25([["зуб", "чистка"], ["зуб"], ["стационар"]])
    top = bm.top({"чистка": 1.0, "зуб": 1.0}, 3)
    assert [i for i, _ in top] == [0, 1]
    assert bm.top({"зуб": 1.0}, 3, allowed={1}) == [(1, bm.scores({"зуб": 1.0})[1])]
    assert bm.top({"нет": 1.0}, 3) == []
    assert BM25([]).top({"x": 1.0}, 3) == []


def test_expand_query_care_synonyms():
    q = expand_query("чистка зубов")
    assert q["чистка"] == 1.0 and q["зуб"] == 1.0
    assert q["снятие"] == SYNONYM_WEIGHT and q["налет"] == SYNONYM_WEIGHT and q["air"] == SYNONYM_WEIGHT
    assert "налет" not in expand_query("стационар")
    assert expand_query("") == {}


def test_cleaning_finds_air_flow_clause_only_in_own_contract():
    st = _store()
    hits = search_clauses(st, "c01", "чистка зубов", k=3)
    assert hits and "AIR FLOW" in hits[0][0].text and hits[0][0].contract_id == "c01"
    for cid in ("c01", "c02"):
        for c, _ in search_clauses(st, cid, "чистка зубов снятие налёта", k=50):
            assert c.contract_id == cid                      # ни чужого договора, ни правил ГК
    assert all("Правила" not in c.text for c, _ in search_clauses(st, "c02", "правила страхования", k=50))


def test_search_requires_contract_and_filters():
    st = _store()
    try:
        search_clauses(st, "", "зуб")
    except ValueError:
        pass
    else:
        raise AssertionError("нужен ValueError без contract_id")
    hits = search_clauses(st, "c01", "вызов врача на дом", program="Стандарт", k=50)
    assert all(c.program in (None, "Стандарт") for c, _ in hits)
    assert search_clauses(st, "c01", "вызов врача на дом", program="Премиум", k=1)[0][0].program == "Премиум"
    hits = search_clauses(st, "c01", "договор срок", doc_types=["contract"], k=50)
    assert hits and all(c.doc_id == "c01.contract" for c, _ in hits)
    assert search_clauses(st, "c01", "зуб", doc_types=["memo"]) == []


def test_index_cache_rebuilt_after_reimport():
    st = _store()
    first = get_index(st, "c01")
    assert get_index(st, "c01") is first
    st.save(make_parsed("c01", extra=3))
    again = get_index(st, "c01")
    assert again is not first and len(again.clauses) == len(first.clauses) + 3


def test_perf_5000_clauses():
    """MUST ANSWER 2: построение индекса и запрос на 5 000 пунктов (печать — в отчёт этапа)."""
    st = KitStore(":memory:")
    st.save(make_parsed("c01", extra=5000 - 7))
    assert len(st.clauses("c01")) == 5000
    t0 = time.perf_counter()
    idx = get_index(st, "c01")
    t_build = time.perf_counter() - t0
    t0 = time.perf_counter()
    for _ in range(20):
        hits = search_clauses(st, "c01", "чистка зубов консультация врача", k=10)
    t_query = (time.perf_counter() - t0) / 20
    print(f"\n[perf] 5000 пунктов: индекс {t_build:.3f} с, запрос {t_query * 1000:.1f} мс, "
          f"терминов {len(idx.bm25.postings)}, база {st.size_bytes() / 1024:.0f} КиБ")
    assert hits and t_build < 10 and t_query < 0.5


def test_search_program_filter_with_shared_block():
    """Общий блок «Стандарт, Бизнес» находится при фильтре по любой из программ, но не по «Премиум»."""
    from dms_core.adapters.base import text_hash
    from dms_core.schemas import Clause

    st = KitStore(":memory:")
    for cid in ("c01", "c02"):
        p = make_parsed(cid)
        text = "Общие для вариантов: телемедицинская консультация врача круглосуточно."
        p.clauses.append(Clause(id=f"{cid}.program#t2r3c1l0", doc_id=f"{cid}.program", contract_id=cid,
                                path="Программа › Общие", program="Стандарт, Бизнес", text=text,
                                text_hash=text_hash(text)))
        st.save(p)
    for prog in ("Стандарт", "Бизнес"):
        hits = search_clauses(st, "c01", "телемедицинская консультация", program=prog, k=1)
        assert hits[0][0].id == "c01.program#t2r3c1l0"
    hits = search_clauses(st, "c01", "телемедицинская консультация", program="Премиум", k=50)
    assert all(c.program in (None, "Премиум") for c, _ in hits)
    assert all(c.contract_id == "c02" for c, _ in search_clauses(st, "c02", "телемедицинская", k=50))


def test_program_matches_shared_block():
    # Общий блок нескольких программ (СОГАЗ: «Стандарт, Бизнес») относится к каждой из них
    from dms_core.store.db import program_matches

    assert program_matches("Стандарт, Бизнес", "Бизнес")
    assert program_matches("Стандарт", "Стандарт")
    assert not program_matches("Стандарт, Бизнес", "ВИП")
