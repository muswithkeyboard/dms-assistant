"""Метрики оценки (план §10.1) — заготовка волны 1. Чистые функции над словарями эталона (eval/gold/README.md)
и ответа (поля Answer/Citation из docs/CONTRACTS.md); без сети, без документов.

Что считается:
  * совпадение ссылки — тот же договор, тот же тип документа (если указан у обеих), та же программа (если указана
    у обеих) и пункт с допуском родитель/потомок («3.1.4» ↔ «3.1.4.6», план §3.2) или та же строка таблицы;
  * recall обязательных ссылок кейса; ссылки на чужой договор (ADR 0005);
  * точность метки SC 01 — матрица 4×4 (allowed / denied / conditional / insufficient) + «нет метки»;
  * ложный отказ: эталон не «нельзя», ответ «нельзя» (в т. ч. insufficient → denied: «не нашли» ≠ «нельзя»,
    `docs/rules.md` п. 3а); ложное обещание: эталон «нельзя» или «недостаточно данных», ответ «можно»;
  * ключевые факты — грубая проверка присутствия (числа + основы слов); модель-судья — eval/judge.py (выключена);
  * волна 2: ссылки ответа Answer (answer_citations), доля отброшенных verify, чужие договоры до фильтра (по следу
    движка), уточнение там, где ожидается, p50/p95, строка кейса (evaluate_case) с причинами ошибки по категориям
    разбор / поиск / модель / правило (failure_causes), свод всего / SC 01 / SC 02 / по договору (summarize).
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence

LABELS = ("allowed", "denied", "conditional", "insufficient")
NO_LABEL = "none"


# ------------------------------------------------------------------ нормализация

def _norm_text(s: str | None) -> str:
    s = unicodedata.normalize("NFC", s or "").replace("ё", "е").replace("Ё", "Е")
    s = s.translate(dict.fromkeys(map(ord, "‐‑‒–—−"), "-")).translate(dict.fromkeys(map(ord, "«»“”„"), '"'))
    return re.sub(r"\s+", " ", s).strip().casefold()


def norm_clause(c: str | None) -> tuple[str, ...]:
    """«п. 3.1.4.» → ("3", "1", "4"); «VII» → ("vii",); пусто → ()."""
    s = _norm_text(c)
    s = re.sub(r"^(п\.|пп\.|пункт|раздел)\s*", "", s).strip().rstrip(".")
    return tuple(x for x in s.split(".") if x)


def clause_related(a: str | None, b: str | None) -> bool:
    """Пункты совпадают с допуском родитель/потомок: один — префикс другого по компонентам (не по символам:
    «3.1» не родитель «3.10»)."""
    x, y = norm_clause(a), norm_clause(b)
    if not x or not y:
        return False
    n = min(len(x), len(y))
    return x[:n] == y[:n]


def _norm_ref(r: str | None) -> str:
    return _norm_text(r).replace(" ", "")


def _id_related(gold_id: str, pred_id: str) -> bool:
    """Адреса блоков совпадают точно или ответ ссылается на версию/часть того же блока через «#»
    («c01.contract#t6r4» ↔ «c01.contract#t6r4#v1»), но «…#t6r4» не совпадает с «…#t6r40» (как в check_acceptance)."""
    g, p = _norm_ref(gold_id), _norm_ref(pred_id)
    return g == p or p.startswith(g + "#") or g.startswith(p + "#")


def clause_covers(gold: str | None, pred: str | None) -> bool:
    """Пункт ответа засчитывается за пункт эталона: тот же или его потомок (эталон грубее: «2.6» ← «2.6.1»).
    Обратное не засчитывается: раздел «1» ответа не доказывает пункт «1.4» эталона (ревизия w2, дефект 8) —
    ссылка на целый раздел не показывает менеджеру нужный фрагмент."""
    x, y = norm_clause(gold), norm_clause(pred)
    return bool(x) and len(y) >= len(x) and y[:len(x)] == x


def _programs(v: str | None) -> set[str]:
    return {_norm_text(p) for p in re.split(r",\s*", v or "") if p.strip()}


def _path_parts(path: str | None) -> set[str]:
    """Разделы пути без номера пункта: «Программа «Москва» › «Бизнес» › 1.4» → {программа "москва", "бизнес"}."""
    parts = (p.strip() for p in _norm_text(path).split("›"))
    return {p for p in parts if p and not re.fullmatch(r"(п\.\s*)?[\divxlc.]+", p)}


def _path_covers(gold: str | None, pred: str | None) -> bool:
    """Все разделы пути эталона (программа, подпрограмма) есть в пути ссылки ответа."""
    return _path_parts(gold) <= _path_parts(pred)


def citation_matches(gold: Mapping, pred: Mapping) -> bool:
    """Ссылка ответа pred засчитывается за обязательную ссылку gold (Citation: contract_id, clause_number |
    table_ref; в эталоне — clause).

    Что указано в эталоне, то обязано совпасть и у ответа (ревизия w2, дефект 8): договор; документ (doc_id и/или
    doc_type); программа (если в эталоне перечень «А, Б» — программа ответа из этого перечня); путь (каждый раздел
    пути эталона — программа, подпрограмма — есть в пути ответа: у Ренессанса нумерация «1.4» заново в каждой
    подпрограмме). Ответ без программы/пути при заданных в эталоне — не засчитывается: путь и программу ссылки
    ответа дополняет runner по базе (`answer_citations(clause_meta=…)`).
    Адрес: строка таблицы / id блока — точное совпадение (с версией «#v1»); номер пункта — тот же или потомок
    (`clause_covers`), раздел-родитель за пункт не засчитывается."""
    if gold.get("contract_id") != pred.get("contract_id"):
        return False
    for key in ("doc_id", "doc_type"):
        if gold.get(key) and _norm_text(gold.get(key)) != _norm_text(pred.get(key)):
            return False
    if gold.get("program"):
        want, have = _programs(gold.get("program")), _programs(pred.get("program"))
        if not have or not have & want:
            return False
    if gold.get("path") and not (pred.get("path") and _path_covers(gold.get("path"), pred.get("path"))):
        return False
    g_ref, p_ref = gold.get("table_ref"), pred.get("table_ref")
    if g_ref and p_ref and _id_related(g_ref, p_ref):
        return True
    g_id, p_id = gold.get("clause_id"), pred.get("clause_id")
    if g_id and p_id and _id_related(g_id, p_id):
        return True
    p_clause = pred.get("clause_number", pred.get("clause"))
    return clause_covers(gold.get("clause"), p_clause)


def citation_recall(gold: Sequence[Mapping], pred: Sequence[Mapping]) -> dict:
    """Доля обязательных ссылок, покрытых хотя бы одной ссылкой ответа. Пустой эталон → recall None."""
    hit = [g for g in gold if any(citation_matches(g, p) for p in pred)]
    return {"required": len(gold), "found": len(hit), "recall": (len(hit) / len(gold)) if gold else None,
            "missed": [g for g in gold if g not in hit]}


def foreign_citations(pred: Iterable[Mapping], allowed_contracts: Iterable[str]) -> list[Mapping]:
    """Ссылки на договор вне контекста (ADR 0005: условия одного договора на другой не переносятся)."""
    allowed = set(allowed_contracts)
    return [p for p in pred if p.get("contract_id") not in allowed]


# ------------------------------------------------------------------ метки SC 01

def _lab(x: str | None) -> str:
    return x if x in LABELS else NO_LABEL


def label_confusion(pairs: Iterable[tuple[str | None, str | None]]) -> dict:
    """pairs — (эталон, ответ). Матрица gold → pred по LABELS + «none» (ответ без метки или эталон без метки);
    accuracy — по кейсам, где у эталона есть метка."""
    keys = (*LABELS, NO_LABEL)
    m = {g: dict.fromkeys(keys, 0) for g in keys}
    total = correct = 0
    for g, p in pairs:
        gl, pl = _lab(g), _lab(p)
        m[gl][pl] += 1
        if gl != NO_LABEL:
            total += 1
            correct += gl == pl
    return {"matrix": m, "labelled": total, "correct": correct, "accuracy": (correct / total) if total else None}


def is_false_refusal(gold: str | None, pred: str | None) -> bool:
    """Ответ «нельзя», когда по эталону можно, можно при условиях или данных недостаточно."""
    return pred == "denied" and gold in ("allowed", "conditional", "insufficient")


def is_false_promise(gold: str | None, pred: str | None) -> bool:
    """Ответ «можно» (без условий), когда по эталону нельзя или данных недостаточно."""
    return pred == "allowed" and gold in ("denied", "insufficient")


def refusal_promise_rates(pairs: Sequence[tuple[str | None, str | None]]) -> dict:
    labelled = [(g, p) for g, p in pairs if g in LABELS]
    fr = sum(is_false_refusal(g, p) for g, p in labelled)
    fp = sum(is_false_promise(g, p) for g, p in labelled)
    n = len(labelled)
    return {"labelled": n, "false_refusal": fr, "false_promise": fp,
            "false_refusal_rate": fr / n if n else None, "false_promise_rate": fp / n if n else None}


# ------------------------------------------------------------------ ключевые факты (грубо)

_NUM = re.compile(r"\d+(?:[ .,]\d+)*")


def _numbers(s: str) -> set[str]:
    return {re.sub(r"[ .,]", "", m.group()) for m in _NUM.finditer(s)}


def _stems(s: str) -> set[str]:
    return {w[:5] for w in re.findall(r"[a-zа-я]{4,}", _norm_text(s))}


def fact_present(fact: str, answer_text: str, min_word_share: float = 0.6) -> bool:
    """Факт считается названным, если в тексте ответа есть все его числа и ≥ min_word_share основ слов
    (первые 5 букв слов от 4 букв). Это заготовка: перефразы без общих слов не засчитываются."""
    t = _norm_text(answer_text)
    if not _numbers(fact) <= _numbers(t):
        return False
    st = _stems(fact)
    return not st or len(st & _stems(t)) / len(st) >= min_word_share


def key_facts_coverage(facts: Sequence[str], answer_text: str) -> dict:
    hit = [f for f in facts if fact_present(f, answer_text)]
    return {"total": len(facts), "found": len(hit), "share": len(hit) / len(facts) if facts else None,
            "missed": [f for f in facts if f not in hit]}


def answer_text(answer: Mapping | None) -> str:
    """Весь видимый менеджеру текст ответа (Answer, ТЗ п. 8) — для грубой проверки ключевых фактов."""
    if not answer:
        return ""
    parts: list[str] = [answer.get("summary") or "", answer.get("label_note") or ""]
    parts += [st.get("text") or "" for st in _all_statements(answer)]
    parts += [pc.get("summary") or "" for pc in answer.get("per_contract") or []]
    for key in ("limitations", "missing", "questions"):
        parts += list(answer.get(key) or [])
    for alt in answer.get("alternatives") or []:
        clinic = alt.get("clinic") or {}
        parts += [clinic.get("clinic_name") or "", clinic.get("address") or "", alt.get("reason") or ""]
    return "\n".join(p for p in parts if p)


# ------------------------------------------------------------------ ссылки ответа

DOC_TYPES = ("contract", "amendment", "program", "clinics", "memo", "rules", "catalog", "insurer_reply", "other")


def _all_statements(answer: Mapping) -> list[Mapping]:
    out = list(answer.get("statements") or []) + list(answer.get("comparisons") or [])
    for pc in answer.get("per_contract") or []:
        out += list(pc.get("statements") or [])
    return out


def doc_type_of(doc_id: str | None) -> str | None:
    """Тип документа из id «c01.contract» / «gk1.rules» (формат store: <договор|ГК>.<тип>)."""
    if not doc_id or "." not in doc_id:
        return None
    t = re.split(r"[.#]", doc_id.split(".", 1)[1], maxsplit=1)[0]
    return t if t in DOC_TYPES else None


def ref_contract(ref: str | None) -> str | None:
    """Договор, которому принадлежит адрес блока «c05.contract#p40»; документы ГК («gk1.rules») — None."""
    m = re.match(r"^(c\d+)\.", ref or "")
    return m.group(1) if m else None


def answer_citations(answer: Mapping | None, clause_meta=None) -> list[dict]:
    """Ссылки ответа в форме citation_matches: договор, тип документа (из doc_id), номер пункта, полный адрес
    строки таблицы (<doc_id>#<table_ref>, если в ответе он без префикса документа).

    clause_meta(citation) → {"path", "program"} | None — путь и программа пункта по базе (runner): в Citation их
    нет, а эталон с путём/программой без них не засчитывается."""
    out: list[dict] = []
    for st in _all_statements(answer or {}):
        for c in st.get("citations") or []:
            doc_id = c.get("doc_id") or ""
            ref = c.get("table_ref")
            if ref and doc_id and not re.match(r"^(c|gk)\d+\.", ref):   # «t6r4» / «t6r4#v1» без документа
                ref = f"{doc_id}#{ref}"
            item = {"contract_id": c.get("contract_id") or ref_contract(doc_id), "doc_id": doc_id,
                    "doc_type": doc_type_of(doc_id), "clause_number": c.get("clause_number"),
                    "table_ref": ref, "clause_id": ref, "verified": bool(c.get("verified"))}
            meta = clause_meta(c) if clause_meta is not None else None
            if meta:
                item.update({k: meta[k] for k in ("path", "program") if meta.get(k)})
            out.append(item)
    return out


def dropped_share(kept: int, dropped: int) -> float | None:
    """Доля ссылок, отброшенных citations.verify, от всех предложенных моделью."""
    n = kept + dropped
    return dropped / n if n else None


def foreign_refs(refs: Iterable[str], allowed_contracts: Iterable[str]) -> list[str]:
    """Адреса блоков, которые модель привела ДО фильтра, на договор вне контекста. Документы ГК — не чужие."""
    allowed = set(allowed_contracts)
    return [r for r in refs if ref_contract(r) and ref_contract(r) not in allowed]


# ------------------------------------------------------------------ уточнение, время

def clarification_expected(expected: Mapping) -> bool:
    """Эталон ждёт уточняющего вопроса: есть expected.questions или статус need_clarification (ТЗ п. 8.1)."""
    return bool(expected.get("questions")) or expected.get("confidence") == "need_clarification"


def clarification_asked(answer: Mapping | None) -> bool:
    return bool(answer) and (bool(answer.get("questions")) or answer.get("confidence") == "need_clarification")


def percentile(values: Iterable[float | None], q: float) -> float | None:
    """Перцентиль по ближайшему рангу: элемент с номером ceil(q·n) отсортированного ряда; пусто → None."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    k = max(1, math.ceil(q * len(xs) - 1e-9))
    return xs[min(k, len(xs)) - 1]


# ------------------------------------------------------------------ один кейс

SC_GROUP = {"sc01": "sc01", "sc02_info": "sc02", "sc02_clinic": "sc02", "sc02_add_clinic": "sc02"}


def case_contracts(case: Mapping) -> list[str]:
    """Договоры кейса: договор контекста; в контексте ГК — договоры из эталона (contracts, обязательные ссылки,
    per_contract). Если не нашлось ни одного — id ГК (метрики «по договору» тогда идут под ним)."""
    ctx, exp = case.get("context") or {}, case.get("expected") or {}
    if ctx.get("contract_id"):
        return [ctx["contract_id"]]
    found = set(case.get("contracts") or [])
    found |= {c.get("contract_id") for c in exp.get("required_citations") or [] if c.get("contract_id")}
    pc = exp.get("per_contract")
    if isinstance(pc, dict):
        found |= set(pc)
    elif isinstance(pc, list):
        found |= {x.get("contract_id") for x in pc if isinstance(x, Mapping) and x.get("contract_id")}
    return sorted(found) or [ctx.get("group_id") or "?"]


def _gold_address(g: Mapping) -> dict:
    """Адрес обязательной ссылки без цитат (для summary.json и test_report.md: ADR 0008, без текстов документов)."""
    return {k: g.get(k) for k in ("contract_id", "doc_type", "clause", "table_ref", "clause_id") if g.get(k)}


def evaluate_case(case: Mapping, record: Mapping, *, allowed_contracts: Iterable[str] | None = None,
                  automation_check=None, ref_resolver=None, clause_meta=None) -> dict:
    """Строка метрик одного кейса. record — результат прогона (eval/runner.py): answer (Answer как JSON) | None,
    trace (необязательный след движка: dropped, kept, draft_refs, run_refs, automation_states), ms, usage,
    cost_usd, error. Тексты в строку не попадают — только id, метки, числа, адреса пунктов.

    automation_check(expected_automation, answer, trace) → dict | None — сверка веток автомата (eval/automation.py).
    ref_resolver(gold_citation) → set[str] | None — id блоков базы для обязательной ссылки (пусто — блока нет в базе,
    None — проверить нельзя); нужен для причины ошибки «разбор / поиск / модель»."""
    exp = case.get("expected") or {}
    answer = record.get("answer")
    trace = record.get("trace") or {}
    contracts = case_contracts(case)
    allowed = set(allowed_contracts) if allowed_contracts is not None else set(contracts)
    row: dict = {"id": case["id"], "scenario": case.get("scenario"), "sc": SC_GROUP.get(case.get("scenario"), "?"),
                 "contracts": contracts, "source": case.get("source"), "trap": case.get("trap"),
                 "error": record.get("error"), "ms": record.get("ms"), "cost_usd": record.get("cost_usd"),
                 "tokens": record.get("usage") or {}, "mode": (answer or {}).get("mode")}
    # Метка SC 01 и статус уверенности
    gl, pl = exp.get("label"), (answer or {}).get("label")
    row["label"] = {"gold": gl, "pred": pl, "ok": (gl == pl) if gl in LABELS else None,
                    "false_refusal": is_false_refusal(gl, pl), "false_promise": is_false_promise(gl, pl)}
    gc, pcf = exp.get("confidence"), (answer or {}).get("confidence")
    row["confidence"] = {"gold": gc, "pred": pcf, "ok": (gc == pcf) if gc else None}
    # Ключевые факты (грубо; судья — record["judge"], если включён)
    kf = key_facts_coverage(exp.get("key_facts") or [], answer_text(answer))
    row["facts"] = {"total": kf["total"], "found": kf["found"], "share": kf["share"],
                    "missed_idx": [i for i, f in enumerate(exp.get("key_facts") or []) if f in kf["missed"]]}
    if record.get("judge"):
        row["facts"]["judge"] = record["judge"]
    # Обязательные ссылки (родитель/потомок), чужие договоры после и до фильтра, отброшенные verify
    gold_c = list(exp.get("required_citations") or [])
    pred_c = answer_citations(answer, clause_meta)
    rc = citation_recall(gold_c, pred_c)
    row["citations"] = {"required": rc["required"], "found": rc["found"], "recall": rc["recall"],
                        "missed": [_gold_address(g) for g in rc["missed"]], "given": len(pred_c)}
    kept = int(trace.get("kept", sum(p["verified"] for p in pred_c)))
    dropped = int(trace["dropped"]) if "dropped" in trace else None
    row["dropped"] = {"kept": kept, "dropped": dropped,
                      "share": dropped_share(kept, dropped) if dropped is not None else None}
    row["foreign"] = {"after": len(foreign_citations([p for p in pred_c if p["contract_id"]], allowed)),
                      "before": len(foreign_refs(trace["draft_refs"], allowed)) if "draft_refs" in trace else None}
    # Уточнение там, где ожидается (и лишнее уточнение там, где не ожидается)
    ce, ca = clarification_expected(exp), clarification_asked(answer)
    row["clarify"] = {"expected": ce, "asked": ca, "ok": (ce == ca) if answer is not None else False}
    row["automation"] = automation_check(exp.get("automation"), answer, trace) if automation_check else None
    row["causes"] = failure_causes(row, gold_c, rc["missed"], trace, ref_resolver)
    row["passed"] = not row["causes"]
    return row


def failure_causes(row: Mapping, gold_c: Sequence[Mapping], missed: Sequence[Mapping], trace: Mapping,
                   ref_resolver=None) -> list[str]:
    """Причины ошибки по категориям плана §10.1: разбор / поиск / модель / правило (+ «сбой» — исключение движка).
      * обязательной ссылки нет: блока нет в базе → разбор; блок есть, но инструменты его не вернули (run_refs)
        → поиск; инструменты вернули, а в ответе нет → модель; следа нет → модель;
      * неверная метка при полном наборе ссылок → правило (метку ставит код по config/verdict_rules.yaml);
      * чужой договор в ответе после verify → правило (фильтр обязан отбросить);
      * неверный статус, нет нужного уточнения, нет ключевых фактов, неверная ветка автомата — модель / правило.
    Порядок фиксирован, повторов нет."""
    causes: list[str] = []

    def add(c: str) -> None:
        if c not in causes:
            causes.append(c)

    if row.get("error"):
        add("сбой")
        return causes
    run_refs = trace.get("run_refs")
    for g in missed:
        ids = ref_resolver(g) if ref_resolver else None
        if ids is not None and not ids:
            add("разбор")
        elif ids and run_refs is not None and not any(_id_related(i, r) for i in ids for r in run_refs):
            add("поиск")
        else:
            add("модель")
    if row["label"]["ok"] is False:
        add("правило" if not missed else "модель")
    if row["foreign"]["after"]:
        add("правило")
    if row["confidence"]["ok"] is False or not row["clarify"]["ok"]:
        add("модель")
    f = row["facts"]
    if f["total"] and f["found"] < f["total"]:
        add("модель")
    a = row.get("automation")
    if a and a.get("ok") is False:
        add("правило")
    return causes


# ------------------------------------------------------------------ свод

def _mean(xs: Iterable[float | None]) -> float | None:
    v = [x for x in xs if x is not None]
    return sum(v) / len(v) if v else None


def aggregate(rows: Sequence[Mapping]) -> dict:
    """Свод метрик по набору строк evaluate_case. Ссылки и отброшенные — микро-усреднение (по всем ссылкам),
    факты — среднее долей по кейсам с фактами, время — p50/p95 по кейсам без сбоя."""
    pairs = [(r["label"]["gold"], r["label"]["pred"]) for r in rows]
    conf = [r["confidence"]["ok"] for r in rows if r["confidence"]["ok"] is not None]
    req = sum(r["citations"]["required"] for r in rows)
    found = sum(r["citations"]["found"] for r in rows)
    kept = sum(r["dropped"]["kept"] for r in rows if r["dropped"]["dropped"] is not None)
    dropped = sum(r["dropped"]["dropped"] for r in rows if r["dropped"]["dropped"] is not None)
    before = [r["foreign"]["before"] for r in rows if r["foreign"]["before"] is not None]
    clar = [r for r in rows if r["clarify"]["expected"]]
    extra = [r for r in rows if not r["clarify"]["expected"] and r["clarify"]["asked"]]
    auto = [r["automation"]["ok"] for r in rows if r.get("automation") and r["automation"].get("ok") is not None]
    ms = [r["ms"] for r in rows if not r.get("error")]
    costs = [r["cost_usd"] for r in rows if r.get("cost_usd") is not None]
    causes: dict[str, int] = {}
    for r in rows:
        for c in r["causes"]:
            causes[c] = causes.get(c, 0) + 1
    return {
        "cases": len(rows), "passed": sum(bool(r["passed"]) for r in rows),
        "errors": sum(bool(r.get("error")) for r in rows),
        "label": label_confusion(pairs), "refusal_promise": refusal_promise_rates(pairs),
        "confidence_accuracy": (sum(conf) / len(conf)) if conf else None,
        "key_facts_share": _mean(r["facts"]["share"] for r in rows),
        "citations": {"required": req, "found": found, "recall": found / req if req else None},
        "dropped": {"kept": kept, "dropped": dropped, "share": dropped_share(kept, dropped)},
        "foreign": {"after": sum(r["foreign"]["after"] for r in rows),
                    "before": sum(before) if before else None},
        "clarify": {"expected": len(clar), "asked_when_expected": sum(r["clarify"]["asked"] for r in clar),
                    "recall": (sum(r["clarify"]["asked"] for r in clar) / len(clar)) if clar else None,
                    "unexpected": len(extra)},
        "automation": {"checked": len(auto), "ok": sum(auto), "accuracy": sum(auto) / len(auto) if auto else None},
        "latency_ms": {"p50": percentile(ms, 0.5), "p95": percentile(ms, 0.95)},
        "cost_usd": sum(costs) if costs else None,
        "tokens": {k: sum(int((r.get("tokens") or {}).get(k, 0)) for r in rows)
                   for k in ("input_tokens", "output_tokens")},
        "causes": causes,
    }


def summarize(rows: Sequence[Mapping]) -> dict:
    """Итог прогона: всего, отдельно SC 01 / SC 02 и по каждому договору (план §10.1). Кейс по ГК входит в свод
    каждого своего договора."""
    by_sc: dict[str, list] = {}
    by_contract: dict[str, list] = {}
    for r in rows:
        by_sc.setdefault(r["sc"], []).append(r)
        for c in r["contracts"]:
            by_contract.setdefault(c, []).append(r)
    return {"overall": aggregate(rows),
            "by_scenario": {k: aggregate(v) for k, v in sorted(by_sc.items())},
            "by_contract": {k: aggregate(v) for k, v in sorted(by_contract.items())}}
