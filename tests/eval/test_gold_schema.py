"""Схема эталонов data/derived/gold/*.jsonl (eval/gold/README.md, ADR 0008). Файлов нет — SKIP.

Проверяется структура записи и изоляция договоров (ADR 0005): у кейса по одному договору все обязательные ссылки —
на этот договор. Сообщения об ошибках содержат только файл, строку и поле — без текста эталона (`docs/rules.md` п. 4;
содержимое holdout/own при настройке не смотрим, правило 6).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from helpers import skip

SCENARIOS = {"sc01", "sc02_info", "sc02_clinic", "sc02_add_clinic"}
LABELS = {"allowed", "denied", "conditional", "insufficient"}
CONFIDENCE = {"confirmed", "need_clarification", "contradictory", "insufficient"}
DOC_TYPES = {"contract", "program", "clinics", "memo", "rules", "catalog", "amendment", "insurer_reply", "other"}
BRANCHES = {None, "insurer_free", "insurer_paid"}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CONTRACT = re.compile(r"^c\d{2}$")
_GROUP = re.compile(r"^gk\d+$")


def _gold_dir() -> Path:
    from dms_core.config import derived_dir

    return derived_dir() / "gold"


def _str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v)


def check_record(rec: dict) -> list[str]:
    """Список нарушений схемы (имена полей, без значений). Пустой список — запись корректна."""
    err: list[str] = []
    if not isinstance(rec.get("id"), str) or not rec["id"].strip():
        err.append("id")
    if rec.get("source") not in {"organizers", "own"}:
        err.append("source")
    if rec.get("scenario") not in SCENARIOS:
        err.append("scenario")
    ctx = rec.get("context")
    if not isinstance(ctx, dict):
        return err + ["context"]
    if not (isinstance(ctx.get("group_id"), str) and _GROUP.match(ctx["group_id"])):
        err.append("context.group_id")
    cid = ctx.get("contract_id")
    if cid is not None and not (isinstance(cid, str) and _CONTRACT.match(cid)):
        err.append("context.contract_id")
    if ctx.get("program") is not None and not isinstance(ctx["program"], str):
        err.append("context.program")
    if ctx.get("date") is not None and not (isinstance(ctx["date"], str) and _DATE.match(ctx["date"])):
        err.append("context.date")
    if not isinstance(rec.get("question"), str) or not rec["question"].strip():
        err.append("question")
    e = rec.get("expected")
    if not isinstance(e, dict):
        return err + ["expected"]
    if e.get("label") is not None and e["label"] not in LABELS:
        err.append("expected.label")
    if rec.get("scenario") == "sc01" and e.get("label") is None:
        err.append("expected.label (sc01 без метки)")
    if e.get("label_note") is not None and not isinstance(e["label_note"], str):
        err.append("expected.label_note")
    if e.get("confidence") not in CONFIDENCE:
        err.append("expected.confidence")
    if not _str_list(e.get("key_facts")):
        err.append("expected.key_facts")
    for k in ("must_not", "questions"):
        v = e.get(k, [])
        if not isinstance(v, list) or (v and not _str_list(v)):
            err.append(f"expected.{k}")
    auto = e.get("automation")
    if auto is not None and (not isinstance(auto, dict) or auto.get("branch") not in BRANCHES
                             or not _str_list(auto.get("states", []))):
        err.append("expected.automation")
    cits = e.get("required_citations")
    if not isinstance(cits, list):
        return err + ["expected.required_citations"]
    for i, c in enumerate(cits):
        p = f"expected.required_citations[{i}]"
        if not isinstance(c, dict):
            err.append(p)
            continue
        if not (isinstance(c.get("contract_id"), str) and _CONTRACT.match(c["contract_id"])):
            err.append(f"{p}.contract_id")
        if c.get("doc_type") not in DOC_TYPES:
            err.append(f"{p}.doc_type")
        if not any(c.get(k) for k in ("clause", "table_ref", "clause_id", "path")):
            err.append(f"{p}: нет ни clause, ни table_ref, ни path")
        q = c.get("quote_hint")
        if q is not None and (not isinstance(q, str) or len(q.split()) > 15):
            err.append(f"{p}.quote_hint (до 15 слов)")
        # ADR 0005: кейс по договору ссылается только на свой договор.
        if cid is not None and c.get("contract_id") != cid:
            err.append(f"{p}.contract_id ≠ context.contract_id")
    if rec.get("source") == "organizers":
        if not isinstance(rec.get("reference_answer"), str) or not rec["reference_answer"].strip():
            err.append("reference_answer")
        if not cits:
            err.append("expected.required_citations пуст")
    return err


def _check_file(path: Path) -> list[str]:
    problems, ids = [], set()
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            problems.append(f"{path.name}:{n}: не JSON")
            continue
        problems += [f"{path.name}:{n}: {x}" for x in check_record(rec)]
        if rec.get("id") in ids:
            problems.append(f"{path.name}:{n}: повтор id")
        ids.add(rec.get("id"))
    return problems


def _files(pattern: str) -> list[Path]:
    files = sorted(_gold_dir().glob(pattern))
    if not files:
        skip(f"нет эталонов {pattern} в {_gold_dir()} (ADR 0008: вне git)")
    return files


# ------------------------------------------------------------------ сам проверяльщик (без данных)

def _valid() -> dict:
    return {"id": "own-gk3-01", "source": "own", "scenario": "sc01",
            "context": {"group_id": "gk3", "contract_id": "c10", "program": "Стандарт", "date": None},
            "question": "Положен ли приём?",
            "expected": {"label": "allowed", "label_note": None, "confidence": "confirmed",
                         "key_facts": ["приём положен"], "must_not": [], "questions": [], "automation": None,
                         "required_citations": [{"contract_id": "c10", "doc_type": "program", "clause": "3.1",
                                                 "table_ref": None, "quote_hint": "три слова здесь"}]},
            "trap": None, "notes": None}


def test_checker_accepts_valid_record():
    assert check_record(_valid()) == []


def test_checker_rejects_foreign_contract_citation():
    rec = _valid()
    rec["expected"]["required_citations"][0]["contract_id"] = "c09"
    assert any("≠ context.contract_id" in x for x in check_record(rec))


def test_checker_rejects_bad_enums_and_missing_label():
    rec = _valid()
    rec["scenario"], rec["expected"]["confidence"] = "sc03", "maybe"
    rec["expected"]["required_citations"][0]["doc_type"] = "pdf"
    errs = check_record(rec)
    assert {"scenario", "expected.confidence", "expected.required_citations[0].doc_type"} <= set(errs)
    rec = _valid()
    rec["expected"]["label"] = None
    assert "expected.label (sc01 без метки)" in check_record(rec)


def test_checker_rejects_citation_without_location_and_long_quote():
    rec = _valid()
    rec["expected"]["required_citations"][0].update(clause=None, quote_hint=" ".join(["слово"] * 16))
    errs = check_record(rec)
    assert any("нет ни clause" in x for x in errs) and any("до 15 слов" in x for x in errs)


def test_checker_group_context_allows_several_contracts():
    rec = _valid()
    rec["scenario"], rec["expected"]["label"] = "sc02_info", None
    rec["context"]["contract_id"] = None
    rec["expected"]["required_citations"].append(dict(rec["expected"]["required_citations"][0], contract_id="c11"))
    assert check_record(rec) == []


# ------------------------------------------------------------------ файлы эталонов (вне git)

def test_k10_k19_schema_and_cases():
    path = _gold_dir() / "k10_k19.jsonl"
    if not path.exists():
        skip(f"нет {path} (scripts/import_gold.py)")
    assert _check_file(path) == []
    recs = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    by_id = {r["id"]: r for r in recs}
    assert sorted(by_id, key=lambda x: int(x[1:])) == [f"K{i}" for i in range(10, 20)]
    assert all(r["source"] == "organizers" for r in recs)
    # Метки SC 01 по эталону (план §3.2) и даты расчёта (план A8).
    labels = {k: (r["expected"]["label"], r["expected"]["label_note"]) for k, r in by_id.items()
              if r["scenario"] == "sc01"}
    assert labels == {"K11": ("allowed", None), "K12": ("denied", None), "K13": ("denied", "есть альтернатива"),
                      "K14": ("conditional", None), "K17": ("conditional", "нужно подтверждение СК"),
                      "K19": ("allowed", "списание правомерно")}
    assert by_id["K10"]["context"]["date"] == "2026-10-19" and by_id["K16"]["context"]["date"] == "2026-10-19"
    assert by_id["K17"]["expected"]["confidence"] == "contradictory"
    k18 = by_id["K18"]
    assert k18["context"]["contract_id"] is None
    assert {c["contract_id"] for c in k18["expected"]["required_citations"]} == {"c09", "c10", "c11", "c12", "c13"}


def test_acceptance_k10_k19_schema():
    path = _gold_dir() / "acceptance_k10_k19.json"
    if not path.exists():
        skip(f"нет {path} (scripts/import_gold.py)")
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert sorted(doc["cases"], key=lambda x: int(x[1:])) == [f"K{i}" for i in range(10, 20)]
    for cid, items in doc["cases"].items():
        assert items, cid
        assert any(i["required"] for i in items), cid
        for i in items:
            assert _CONTRACT.match(i["contract_id"]) and i["doc_type"] in DOC_TYPES, cid
            assert i["ref"] and i["ref"].startswith(f"{i['contract_id']}.{i['doc_type']}#"), cid
            assert i["verified"] is True, cid
            assert 1 <= len(i["quote_hint"].split()) <= 15, cid


def test_own_cases_schema():
    problems = [p for f in _files("own_*.jsonl") for p in _check_file(f)]
    assert problems == []


def test_holdout_schema():
    problems = [p for f in _files("holdout.jsonl") for p in _check_file(f)]
    assert problems == []
