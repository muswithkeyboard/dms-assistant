"""eval/metrics.py на своих маленьких данных (без документов организаторов)."""
from __future__ import annotations

import sys

from helpers import ROOT

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval import metrics as M


def test_clause_parent_child_tolerance():
    assert M.clause_related("3.1.4", "3.1.4.6") and M.clause_related("3.1.4.6", "3.1.4")
    assert M.clause_related("п. 2.6.1.", "2.6.1")
    assert M.clause_related("2", "2.6.1")                     # эталон «п. 2» грубее точного 2.6.1
    assert not M.clause_related("3.1", "3.10")                 # по компонентам, не по символам
    assert not M.clause_related("3.1.4", "3.1.5")
    assert not M.clause_related(None, "3.1") and not M.clause_related("", "")
    assert M.clause_related("VII", "VII") and not M.clause_related("VI", "VII")


def test_citation_match_requires_same_contract_doc_and_program():
    gold = {"contract_id": "c09", "doc_type": "program", "program": "Стандарт, Бизнес", "clause": "3.2"}
    assert M.citation_matches(gold, {"contract_id": "c09", "doc_type": "program", "program": "Стандарт",
                                     "clause_number": "3.2.1"})
    # программа задана в эталоне, у ссылки ответа её нет — не засчитывается (ревизия w2, дефект 8)
    assert not M.citation_matches(gold, {"contract_id": "c09", "doc_type": "program", "clause_number": "3.2.1"})
    # тот же пункт чужого договора ГК (тексты совпадают на 99 %) — не засчитывается (ADR 0005)
    assert not M.citation_matches(gold, {"contract_id": "c10", "doc_type": "program", "clause_number": "3.2"})
    assert not M.citation_matches(gold, {"contract_id": "c09", "doc_type": "contract", "clause_number": "3.2"})
    assert not M.citation_matches(gold, {"contract_id": "c09", "doc_type": "program", "program": "ВИП",
                                         "clause_number": "3.2"})


def test_citation_match_by_table_row_and_path():
    row = {"contract_id": "c10", "doc_type": "clinics", "clause": None,
           "table_ref": "c10.clinics#Список медицинских организаций!569"}
    assert M.citation_matches(row, {"contract_id": "c10", "doc_type": "clinics",
                                    "table_ref": "c10.clinics#Список  медицинских организаций!569"})
    assert not M.citation_matches(row, {"contract_id": "c10", "doc_type": "clinics",
                                        "table_ref": "c10.clinics#Список медицинских организаций!17"})
    biz = {"contract_id": "c05", "doc_type": "program", "clause": "1.4",
           "path": "Программа «Москва» › Программа стоматологического обслуживания «Бизнес» › 1.4"}
    assert M.citation_matches(biz, {"contract_id": "c05", "doc_type": "program", "clause_number": "1.4",
                                    "path": "Программа «Москва» › Программа стоматологического обслуживания «Бизнес»"})
    # «1.4» из «Антиклещ» той же программы — другой путь
    assert not M.citation_matches(biz, {"contract_id": "c05", "doc_type": "program", "clause_number": "1.4",
                                        "path": "Программа «Москва» › Антиклещ › 1.4"})


def test_section_parent_not_credited_for_clause():
    """Раздел «1» ответа не засчитывается за пункт «1.4» эталона; потомок «1.4.2» — засчитывается."""
    g = {"contract_id": "c09", "doc_type": "program", "clause": "1.4"}
    p = {"contract_id": "c09", "doc_type": "program", "doc_id": "c09.program"}
    assert not M.citation_matches(g, {**p, "clause_number": "1"})
    assert M.citation_matches(g, {**p, "clause_number": "1.4"}) and M.citation_matches(g, {**p, "clause_number": "1.4.2"})
    assert not M.clause_covers("1.4", "1") and M.clause_covers("2", "2.6.1") and not M.clause_covers("3.1", "3.11")


def test_path_and_document_required_when_in_gold():
    g = {"contract_id": "c09", "doc_type": "program", "clause": "1.4", "path": "Программа «Бизнес» › 1.4"}
    p = {"contract_id": "c09", "doc_type": "program", "clause_number": "1.4", "doc_id": "c09.program"}
    assert not M.citation_matches(g, p)                                   # без пути — не проверить подпрограмму
    assert M.citation_matches(g, {**p, "path": "Программа «Бизнес» › Стоматология › 1.4"})
    assert not M.citation_matches(g, {**p, "path": "Программа «Антиклещ» › 1.4"})
    assert not M.citation_matches({**g, "path": None, "doc_id": "c09.memo"}, p)


def test_answer_citations_take_path_from_clause_meta():
    ans = {"statements": [{"citations": [{"doc_id": "c09.program", "contract_id": "c09", "clause_number": "1.4",
                                          "quote": "q", "verified": True}]}]}
    meta = M.answer_citations(ans, lambda c: {"path": "Программа «Бизнес» › 1.4", "program": "Бизнес"})
    assert meta[0]["path"].endswith("1.4") and meta[0]["program"] == "Бизнес"
    assert "path" not in M.answer_citations(ans)[0]


def test_citation_recall_and_foreign():
    gold = [{"contract_id": "c05", "doc_type": "contract", "clause": "3.4.5"},
            {"contract_id": "c05", "doc_type": "contract", "clause": "7.1"}]
    pred = [{"contract_id": "c05", "doc_type": "contract", "clause_number": "3.4.5"},
            {"contract_id": "c06", "doc_type": "contract", "clause_number": "7.1"}]
    r = M.citation_recall(gold, pred)
    assert (r["required"], r["found"], r["recall"]) == (2, 1, 0.5)
    assert r["missed"] == [gold[1]]
    assert M.citation_recall([], pred)["recall"] is None
    assert M.foreign_citations(pred, ["c05"]) == [pred[1]]


def test_label_confusion_matrix_4x4():
    pairs = [("allowed", "allowed"), ("denied", "denied"), ("conditional", "allowed"),
             ("insufficient", "denied"), ("denied", None), (None, "allowed")]
    c = M.label_confusion(pairs)
    m = c["matrix"]
    assert set(m) == {*M.LABELS, M.NO_LABEL} and all(set(row) == set(m) for row in m.values())
    assert m["allowed"]["allowed"] == 1 and m["conditional"]["allowed"] == 1
    assert m["insufficient"]["denied"] == 1 and m["denied"][M.NO_LABEL] == 1 and m[M.NO_LABEL]["allowed"] == 1
    assert (c["labelled"], c["correct"]) == (5, 2) and abs(c["accuracy"] - 0.4) < 1e-9
    assert sum(sum(r.values()) for r in m.values()) == len(pairs)
    assert M.label_confusion([])["accuracy"] is None


def test_false_refusal_and_false_promise():
    # «не нашли» (insufficient) → «нельзя» — ложный отказ (`docs/rules.md` п. 3а)
    assert M.is_false_refusal("insufficient", "denied") and M.is_false_refusal("conditional", "denied")
    assert not M.is_false_refusal("denied", "denied") and not M.is_false_refusal("allowed", "conditional")
    assert M.is_false_promise("denied", "allowed") and M.is_false_promise("insufficient", "allowed")
    assert not M.is_false_promise("conditional", "allowed") and not M.is_false_promise("denied", "conditional")
    r = M.refusal_promise_rates([("allowed", "denied"), ("denied", "allowed"), ("denied", "denied"),
                                 (None, "denied")])
    assert (r["labelled"], r["false_refusal"], r["false_promise"]) == (3, 1, 1)
    assert abs(r["false_refusal_rate"] - 1 / 3) < 1e-9


def test_key_facts_coverage_needs_numbers():
    facts = ["4-й взнос — 56 760,00 руб.", "Оплатить до 31.10.2026"]
    ok = "Следующий — 4-й взнос: 56 760,00 руб., оплатить до 31.10.2026."
    assert M.key_facts_coverage(facts, ok)["found"] == 2
    bad = "Следующий — 4-й взнос: 56 760,00 руб., оплатить до 31.07.2026."
    cov = M.key_facts_coverage(facts, bad)
    assert cov["found"] == 1 and cov["missed"] == [facts[1]]
    assert M.key_facts_coverage([], ok)["share"] is None
