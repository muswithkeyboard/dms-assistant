"""summary.json → docs/test_report.md (план §10.1, ADR 0008).

Отчёт лежит в git, поэтому в нём только ID кейсов, ожидаемые и полученные метки/статусы, числа и адреса пунктов
(«c01 · contract · п. 2.8») — без вопросов, ответов и цитат документов (ADR 0008: не длиннее 15 слов; здесь — ни одной).
По holdout — только свод без строк (правило 6), и только с явным allow_holdout.
"""
from __future__ import annotations

from collections.abc import Mapping

from .metrics import LABELS, NO_LABEL

LABEL_RU = {"allowed": "можно", "denied": "нельзя", "conditional": "при условиях", "insufficient": "недост.",
            None: "—", NO_LABEL: "—"}
CONF_RU = {"confirmed": "подтв.", "need_clarification": "уточнить", "contradictory": "противоречие",
           "insufficient": "недост.", None: "—"}
SC_RU = {"sc01": "SC 01", "sc02": "SC 02"}
MAX_QUOTE_WORDS = 15


class ReportError(RuntimeError):
    pass


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f} %"


def _num(x) -> str:
    return "—" if x is None else str(x)


def _ms(x) -> str:
    return "—" if x is None else f"{x / 1000:.1f} с"


def _usd(x) -> str:
    return "—" if x is None else f"{x:.4f}"


def _addr(g: Mapping) -> str:
    """Адрес пропущенной ссылки без цитат: договор · тип · пункт / строка."""
    parts = [g.get("contract_id") or "?", g.get("doc_type") or "?"]
    if g.get("clause"):
        parts.append(f"п. {g['clause']}")
    ref = g.get("table_ref") or g.get("clause_id")
    if ref and "#" in str(ref):
        parts.append(str(ref).split("#", 1)[1])
    elif not g.get("clause") and ref:
        parts.append("строка таблицы")
    return " · ".join(parts)


def _agg_rows(title: str, groups: Mapping[str, Mapping]) -> list[str]:
    out = [f"### {title}", "",
           ("| Срез | Кейсов | Пройдено | Сбоев | Метка | Ложный отказ | Ложное обещание | Статус | Факты | "
            "Ссылки (recall) | Отброшено | Чужой договор (после / до) | Уточнение | Автомат | p50 | p95 | USD |"),
           "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for name, a in groups.items():
        rp, lab = a["refusal_promise"], a["label"]
        cl, au = a["clarify"], a["automation"]
        out.append(
            f"| {SC_RU.get(name, name)} | {a['cases']} | {a['passed']} | {a['errors']} | "
            f"{_pct(lab['accuracy'])} ({lab['correct']}/{lab['labelled']}) | {rp['false_refusal']} | "
            f"{rp['false_promise']} | {_pct(a['confidence_accuracy'])} | {_pct(a['key_facts_share'])} | "
            f"{_pct(a['citations']['recall'])} ({a['citations']['found']}/{a['citations']['required']}) | "
            f"{_pct(a['dropped']['share'])} | {a['foreign']['after']} / {_num(a['foreign']['before'])} | "
            f"{_pct(cl['recall'])} ({cl['asked_when_expected']}/{cl['expected']}; лишних {cl['unexpected']}) | "
            f"{_pct(au['accuracy'])} ({au['ok']}/{au['checked']}) | {_ms(a['latency_ms']['p50'])} | "
            f"{_ms(a['latency_ms']['p95'])} | {_usd(a['cost_usd'])} |")
    return out + [""]


def _matrix(a: Mapping) -> list[str]:
    keys = (*LABELS, NO_LABEL)
    m = a["label"]["matrix"]
    out = ["### Метка SC 01: эталон (строки) → ответ (столбцы)", "",
           "| эталон \\ ответ | " + " | ".join(LABEL_RU[k] for k in keys) + " |",
           "|---|" + "--:|" * len(keys)]
    for g in keys:
        out.append(f"| {LABEL_RU[g]} | " + " | ".join(str(m[g][p]) for p in keys) + " |")
    return out + [""]


def render(summary: Mapping, *, allow_holdout: bool = False) -> str:
    if summary.get("holdout") and not allow_holdout:
        raise ReportError("это прогон holdout: отчёт строит только автор с флагом --holdout (правило 6)")
    met = summary["metrics"]
    lines = ["# Отчёт о тестах — прогон эталонов", "",
             ("Собран `scripts/make_test_report.py` из `summary.json` (`scripts/eval.py`, план §10.1). Только ID "
              "кейсов, метки, числа и адреса пунктов; вопросы, ответы и цитаты документов — в `data/derived/eval/` "
              "(вне git, ADR 0008)."), "",
             (f"- Прогон: `{summary.get('run_id')}`, {summary.get('created_at')}; holdout: "
              f"{'да' if summary.get('holdout') else 'нет'}"),
             (f"- Модель: {summary.get('provider') or 'нет (extractive)'} / {summary.get('model') or '—'}; "
              f"движок {summary.get('engine_version') or '—'}; промпты: ")
             + (", ".join(f"{k} {v}" for k, v in (summary.get("prompts") or {}).items()) or "—"),
             "- Эталоны: " + (", ".join(f"{g['name']} ({g['sha256_16']})" for g in summary.get("gold_files") or [])
                              or "—") + f"; кейсов {summary.get('cases')}",
             "",
             ("Кейс пройден, если нет сбоя, метка SC 01 и статус уверенности совпали, найдены все обязательные "
              "ссылки (допуск родитель/потомок), нет ссылок на чужой договор, уточнение задано ровно там, где "
              "ожидается, ключевые факты названы (грубо: числа + основы слов) и ветка автомата верна. «—» — метрику "
              "посчитать нельзя (нет данных или следа движка), это не ноль."), "",
             "## Свод", ""]
    lines += _agg_rows("Всего и по сценариям", {"всего": met["overall"], **met["by_scenario"]})
    lines += _agg_rows("По договорам (кейс по ГК входит в каждый свой договор)", met["by_contract"])
    lines += _matrix(met["overall"])
    causes = met["overall"]["causes"]
    lines += ["### Причины ошибок", "", "| Категория | Кейсов |", "|---|--:|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(causes.items(), key=lambda kv: -kv[1])] or ["| — | 0 |"]
    lines += ["", ("Категории: **разбор** — нужного блока нет в базе; **поиск** — блок есть, инструменты его не "
                   "вернули; **модель** — вернули, но в ответе нет / неверный статус, факты, уточнение; **правило** — "
                   "метка или ветка неверны при полном наборе ссылок, чужой договор прошёл фильтр; **сбой** — "
                   "исключение."), ""]
    if summary.get("holdout"):
        lines += ["Построчная таблица по holdout не выводится (правило 6).", ""]
        return "\n".join(lines)
    lines += ["## Кейсы", "",
              ("| ID | Сценарий | Договор | Метка: ожидание → итог | Статус: ожидание → итог | Ссылки | Факты | "
               "Уточнение | Автомат | Время | Итог | Причина | Пропущенные ссылки |"),
              "|---|---|---|---|---|--:|--:|---|---|--:|---|---|---|"]
    for r in summary.get("rows") or []:
        lab, conf, cit, f, cl = r["label"], r["confidence"], r["citations"], r["facts"], r["clarify"]
        au = r.get("automation")
        auto = "—" if not au else (("✓ " if au["ok"] else "✗ ") + "→".join(au["got"] or ["—"])
                                   + f" (ждали {'→'.join(au['expected'])})")
        clar = ("ждали, " if cl["expected"] else "") + ("задано" if cl["asked"] else "нет")
        status = "сбой" if r.get("error") else ("✓ пройден" if r["passed"] else "✗ ошибка")
        missed = "; ".join(_addr(g) for g in cit["missed"]) or "—"
        lines.append(
            f"| {r['id']} | {r['scenario']} | {', '.join(r['contracts'])} | "
            f"{LABEL_RU.get(lab['gold'], lab['gold'])} → {LABEL_RU.get(lab['pred'], lab['pred'])} | "
            f"{CONF_RU.get(conf['gold'], conf['gold'])} → {CONF_RU.get(conf['pred'], conf['pred'])} | "
            f"{cit['found']}/{cit['required']} | {f['found']}/{f['total']} | {clar} | {auto} | {_ms(r.get('ms'))} | "
            f"{status} | {', '.join(r['causes']) or '—'} | {missed} |")
    errs = [r for r in summary.get("rows") or [] if r.get("error")]
    if errs:
        lines += ["", "## Сбои", ""] + [f"- {r['id']}: {_short(r['error'])}" for r in errs]
    lines += ["", ("Примеры веток автомата и черновиков писем — после этапа автомата добавления клиники "
                   "(ADR 0004): в свод попадают, когда движок кладёт путь автомата в trace.automation_states."), ""]
    return "\n".join(lines)


def _short(s: str) -> str:
    """Сообщение сбоя — не длиннее 15 слов (ADR 0008)."""
    words = str(s).replace("|", "/").split()
    return " ".join(words[:MAX_QUOTE_WORDS]) + (" …" if len(words) > MAX_QUOTE_WORDS else "")
