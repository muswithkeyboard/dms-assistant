"""Точность выбора сценария на вопросах эталонов (К10–К19 + свои кейсы, БЕЗ holdout): правила, kNN и сочетания.
Запуск: uv run python scripts/bench/route_eval.py [--fp32]"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
if "--fp32" in sys.argv:                     # сравнение с неквантованной моделью: временная папка без int8
    import shutil
    import tempfile

    from dms_core.index.embed import encoder_dir
    tmp = Path(tempfile.mkdtemp())
    for f in ("model.onnx", "tokenizer.json", "meta.json"):
        shutil.copy(encoder_dir() / f, tmp / f)
    os.environ["DMS_ENCODER_DIR"] = str(tmp)

from dms_core.config import derived_dir
from dms_core.constants import Scenario
from dms_core.scenarios.knn_route import knn_scenario
from dms_core.scenarios.route import rule_scenario

cases = []
for f in sorted((derived_dir() / "gold").glob("*.jsonl")):
    if "holdout" in f.name:
        continue
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            cases.append((r["question"], r["scenario"]))
knn_scenario("прогрев")
t = time.time()
rows = []
for q, gold in cases:
    r = rule_scenario(q)
    k = knn_scenario(q)
    rows.append((gold, r, k))
ms = (time.time() - t) / len(cases) * 1000


def acc(pick):
    return sum(pick(r, k) == Scenario(g) for g, r, k in rows) / len(rows)


variants = {
    "правила": lambda r, k: r,
    "kNN": lambda r, k: k.scenario if k else r,
    "правила, kNN если правила не нашли": lambda r, k: (k.scenario if k else r) if r == Scenario.general else r,
    "kNN при уверенности (margin ≥ 0,5), иначе правила": lambda r, k: k.scenario if k and k.margin >= 0.5 else r,
    "то же, но маркеры отказа (правила → sc01) важнее kNN":
        lambda r, k: r if r == Scenario.sc01 else (k.scenario if k and k.margin >= 0.5 else r),
}
print(f"вопросов {len(rows)}; kNN {ms:.1f} мс на вопрос")
for name, f in variants.items():
    print(f"  {name}: {acc(f):.2f}")
