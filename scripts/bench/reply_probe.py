"""Проверка ответа в чате (ADR 0014, этап w15-reply) на выборке вопросов разных типов — без модели (guided).

Выборка: К10–К19 (data/derived/gold/k10_k19.jsonl; holdout не открывается), по N вопросов ревизий 7 и 8
(data/derived/review/review7.jsonl, review8.jsonl — по одному на класс, по кругу; фиксированный порядок) и свободные
вопросы ниже (уточнение, ГК, справка, разные сценарии). Для вопросов ревизии 7 с выбором клиники выбор менеджера
моделируется как в review_eval.py --pick.

Проверки по каждому ответу:
  есть reply (заполнил движок/compose; иначе собирается chat_reply — как после правки engine, см. отчёт этапа);
  lead — не больше 2 фраз и 300 знаков; ни в lead, ни в points/ask/caution нет id строк и кодов договоров;
  у каждого point есть refs, и каждый ref — ключ проверенной ссылки ответа (compose.cite_ref), а quote —
  подстрока проверенной цитаты.
Печатаются только счётчики; ответы (с текстами документов) пишутся в <out>/reply_probe.jsonl — папка вне git.

Запуск из app/: DMS_DATA_DIR=<своя копия> uv run python scripts/bench/reply_probe.py <out_dir> [--n 10]
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "bench"))
os.environ.setdefault("DMS_ENGINE_MODE", "guided")

from dms_core.citations import normalize
from dms_core.compose import chat_reply, cite_ref, sentences
from dms_core.schemas import Answer, ChatContext

# id строк/ссылок и коды договоров в тексте ответа — запрещены (CONTRACTS §12)
ID_RX = re.compile(r"\b[a-z]\d{2}\.\w+#|\bc\d{2}\b|\bt\d+r\d+|#v\d|\bsupports\b|\bguided\b|матриц", re.IGNORECASE)

FREE = [
    ("free-clarify-gk", {"group_id": "gk3"}, "Сколько стоит программа?"),
    ("free-gk-refusal", {"group_id": "gk3", "program": "Бизнес"},
     "клиника отказала в МРТ коленного сустава, сказали что не входит — правомерно?"),
    ("free-info-pay", {"contract_id": "c02"}, "когда следующий платёж по договору?"),
    ("free-info-dental", {"contract_id": "c06", "program": "Москва"}, "входит ли стоматология?"),
    ("free-clinic", {"contract_id": "c01", "program": "Челябинск+Магнитогорск+Учалы Стандарт"},
     "в какие клиники можно к кардиологу в Челябинске?"),
    ("free-c06-leg", {"contract_id": "c06", "program": "Москва"}, "клиника не дала отрезать ногу - оправдано?"),
    ("free-noprogram", {"contract_id": "c01"}, "можно ли к стоматологу?"),
]


def _ctx(d: dict[str, Any]) -> ChatContext:
    return ChatContext(**{k: v for k, v in d.items() if k in ("group_id", "contract_id", "program", "date")})


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _by_class(rows: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """По одному вопросу каждого класса по кругу (порядок набора), пока не наберётся n — без подбора под ответ."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(str(r.get("cls")), []).append(r)
    out: list[dict[str, Any]] = []
    depth = 0
    while len(out) < n and any(depth < len(g) for g in groups.values()):
        out += [g[depth] for g in groups.values() if depth < len(g)][: n - len(out)]
        depth += 1
    return out


def _sets_dir() -> Path:
    """Наборы вопросов читаются (только чтение) из общих data/derived, даже если DMS_DATA_DIR — своя копия базы."""
    return Path(os.environ.get("DMS_SETS_DIR") or ROOT / "data" / "derived")


def sample(n: int) -> list[tuple[str, ChatContext, str, dict[str, Any]]]:
    items: list[tuple[str, ChatContext, str, dict[str, Any]]] = []
    gold = _sets_dir() / "gold" / "k10_k19.jsonl"
    if gold.exists():
        items += [(r["id"], _ctx(r["context"]), r["question"], r) for r in _jsonl(gold)]
    for name in ("review7", "review8"):
        path = _sets_dir() / "review" / f"{name}.jsonl"
        if path.exists():
            for i, r in enumerate(_by_class(_jsonl(path), n)):
                ctx = ChatContext(group_id=r.get("g"), contract_id=r.get("c"), program=r.get("p"))
                items.append((r.get("id") or f"{name}-{r.get('cls')}-{i}", ctx, r["q"], r))
    items += [(i, _ctx(c), q, {}) for i, c, q in FREE]
    return items


def check(a: Answer) -> list[str]:
    """Нарушения формы ответа в чате (пусто — ответ в порядке)."""
    r = a.reply
    if r is None:
        return ["no_reply"]
    bad: list[str] = []
    if len(sentences(r.lead)) > 2:
        bad.append("lead_sentences")
    if len(r.lead) > 300:
        bad.append("lead_long")
    texts = [r.lead, r.caution or "", *r.ask, *(p.text for p in r.points)]
    if any(ID_RX.search(t) for t in texts):
        bad.append("service_id")
    sts = [*a.statements, *(s for pc in a.per_contract for s in pc.statements)]
    live = [c for s in sts for c in s.citations if c.verified]
    refs = {cite_ref(c) for c in live}
    quotes = [normalize(c.quote) for c in live]
    for p in r.points:
        if not p.refs or not set(p.refs) <= refs:
            bad.append("point_ref")
        if p.quote and not any(normalize(p.quote) in q for q in quotes):
            bad.append("point_quote")
    return bad


def main(argv: list[str]) -> int:
    from dms_core.engine import answer as run
    from dms_core.tools._common import default_store

    out_dir = Path(argv[0]) if argv and not argv[0].startswith("--") else None
    if out_dir is None:
        print("нужна папка вывода (вне git, например mktemp -d)")
        return 2
    n = int(argv[argv.index("--n") + 1]) if "--n" in argv else 10
    store = default_store()
    rows: list[dict[str, Any]] = []
    stats = {"total": 0, "engine_reply": 0, "ok": 0, "points": 0}
    viol: dict[str, int] = {}
    for qid, ctx, q, rec in sample(n):
        a = run(q, ctx, store=store)
        if rec.get("clinic_row_id") or rec.get("clinic_pick"):
            from review_eval import simulated_picks

            picks = simulated_picks(rec, a.clinic_choices)
            if picks:
                a = run(q, ctx, store=store, clinic_picks={c: p for c, (p, _w) in picks.items()})
        stats["total"] += 1
        stats["engine_reply"] += a.reply is not None
        # пока движок не заполняет reply во всех путях — собрать здесь (те же строки, что предложены для engine)
        a.reply = chat_reply(a, question=q, store=store)
        bad = check(a)
        stats["ok"] += not bad
        stats["points"] += len(a.reply.points)
        for b in bad:
            viol[b] = viol.get(b, 0) + 1
        rows.append({"id": qid, "q": q, "scenario": a.scenario.value, "mode": a.mode.value,
                     "label": a.label.value if a.label else None, "bad": bad,
                     "reply": a.reply.model_dump(mode="json")})
        print(f"{qid:28} {a.scenario.value:16} {(a.label.value if a.label else '-'):13} "
              f"points={len(a.reply.points)} ask={len(a.reply.ask)} {'OK' if not bad else ','.join(bad)}")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "reply_probe.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                                              encoding="utf-8")
    print(json.dumps({**stats, "violations": viol}, ensure_ascii=False))
    return 0 if not viol else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
