"""Отказ клиники в процедуре (этап w14-refusal): есть ли у ответа вывод по существу — на 13 договорах.

Формулировки — свои (сленг, опечатки, разные процедуры: ампутация, операция, госпитализация, капельница, роды,
гипс, реанимация), не из наборов ревизии и не из эталонов (`docs/rules.md` п. 6). Договор берётся с первой программой
карточки (вопрос «какая программа?» не мешает замеру). По каждой паре «формулировка × договор» печатаются:
  вывод   — краткий вывод не пустой и не «не найдено / нужно уточнение» (config/texts_ru/ui.yaml summary.*);
  ссылки  — число проверенных ссылок (citations.verify) во всех утверждениях ответа;
  метка   — метка SC 01.
Итог — по формулировкам (доля договоров с выводом, среднее ссылок, метки) и общий. Текстов документов в выводе нет.

  uv run python scripts/bench/refusal_probe.py [--json до.json] [--compare до.json] [--contracts c01,c06]
Запуск из app/; данные — DMS_DATA_DIR (своя копия kit.sqlite).
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("DMS_ENGINE_MODE", "guided")

# Свои формулировки отказа в процедуре: глаголы, существительные, сленг, опечатки.
PROBES: tuple[str, ...] = (
    "клиника не дала отрезать ногу - оправдано?",
    "не дали отрезать ногу, правомерно?",
    "клиника не дала атрезать ногу",                       # опечатка
    "отказали в ампутации ноги — правильно?",
    "отказали в операции на колене",
    "клиника отказалась делать операцию на колене, это законно?",
    "не положили в больницу, обоснованно?",
    "в стационар не положили, имеют право?",
    "отказали в госпитализации",
    "не стали делать капельницу",
    "отказались ставить капельницу",
    "не приняли роды",
    "роддом отказал в родах по полису",
    "отказали в гипсе",
    "не стали накладывать гипс на руку",
    "не загипсовали руку, отказ правомерен?",
    "не стали вырезать аппендицит, отказали",
    "отказали удалить аппендикс",
    "клиника не хочет оперировать грыжу",
    "отказ в удалении желчного пузыря",
    "отказали в резекции желудка",
    "не взяли в реанимацию",
    "отказали в реанимации, правомерно?",
    "не прооперировали мениск, сказали платно",
    "отказ в хирургическом вмешательстве на позвоночнике",
    "не дали сделать операцию на глаза",
    "отказали в иссечении липомы",
    "отказ в удалении родинки",
    "не стали удалять кисту яичника",
    "клиника отказала в капельнице на дому",
    "отказали вскрыть абсцесс",
    "отказали зашить рану",
    "отказали в наложении швов",
    "оперировать катаракту отказались",
    "не стали оперировать перелом, требуют деньги",
    "не госпитализировали с аппендицитом",
    "отказали в кесаревом сечении",
    "отказали в лонгете на ногу",
)


def _no_answer_texts() -> set[str]:
    from dms_core.config import load_yaml

    s = (load_yaml("texts_ru/ui.yaml") or {}).get("summary") or {}
    return {str(s.get("empty") or ""), str(s.get("neutral") or ""), "Нужно уточнение.", ""}


def probe(q: str, cid: str, program: str, store: Any, empty: set[str]) -> dict[str, Any]:
    from dms_core.engine import answer
    from dms_core.schemas import ChatContext

    a = answer(q, ChatContext(contract_id=cid, program=program), llm=None, store=store)
    sts = [*a.statements, *(s for pc in a.per_contract for s in pc.statements)]
    return {"conclusion": a.summary.strip() not in empty,
            "refs": sum(1 for s in sts for c in s.citations if c.verified),
            "label": a.label.value if a.label else "-"}


def main(argv: list[str]) -> int:
    from dms_core.store import KitStore, default_path

    args = dict(zip(argv[::2], argv[1::2], strict=False))
    store = KitStore(default_path())
    want = set(filter(None, (args.get("--contracts") or "").split(",")))
    contracts = [(c.id, c.programs[0].name) for c in store.contracts(None)
                 if c.programs and (not want or c.id in want)]
    empty = _no_answer_texts()
    out: dict[str, dict[str, Any]] = {}
    for i, q in enumerate(PROBES):
        res = {cid: probe(q, cid, prog, store, empty) for cid, prog in contracts}
        out[str(i)] = res
    before = json.loads(Path(args["--compare"]).read_text(encoding="utf-8")) if "--compare" in args else None
    n = len(contracts)
    print(f"договоров: {n}; формулировок: {len(PROBES)}")
    print("№\tвывод\tссылок/дог.\tметки" + ("\tвывод до\tссылок до" if before else ""))
    tot_c = tot_r = tot_bc = tot_br = 0
    labels: Counter[str] = Counter()
    for k, res in out.items():
        c = sum(r["conclusion"] for r in res.values())
        refs = sum(r["refs"] for r in res.values())
        lab = Counter(r["label"] for r in res.values())
        labels.update(lab)
        tot_c, tot_r = tot_c + c, tot_r + refs
        row = [k, f"{c}/{n}", f"{refs / max(n, 1):.1f}", " ".join(f"{a}:{b}" for a, b in sorted(lab.items()))]
        if before and k in before:
            bc = sum(r["conclusion"] for r in before[k].values())
            br = sum(r["refs"] for r in before[k].values())
            tot_bc, tot_br = tot_bc + bc, tot_br + br
            row += [f"{bc}/{len(before[k])}", f"{br / max(len(before[k]), 1):.1f}"]
        print("\t".join(row))
    total = len(PROBES) * n
    print(f"итого: вывод {tot_c}/{total}, ссылок в среднем {tot_r / max(total, 1):.1f}; метки: "
          + ", ".join(f"{a} {b}" for a, b in sorted(labels.items())))
    if before:
        print(f"до: вывод {tot_bc}/{total}, ссылок в среднем {tot_br / max(total, 1):.1f}")
    if "--json" in args:
        Path(args["--json"]).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
