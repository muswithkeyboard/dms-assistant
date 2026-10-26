"""Замер rubert-tiny2: память, задержка, маршрутизация kNN по вопросам эталонов (без holdout).
Запуск: uv run --no-project --with sentence-transformers python encoder_bench.py <gold_dir> <app_src>"""
import json
import os
import resource
import sys
import time
from pathlib import Path

gold, src = Path(sys.argv[1]), sys.argv[2]
torch_threads = int(os.environ.get("THREADS", "1"))
cases = []
for f in sorted(gold.glob("*.jsonl")):
    if "holdout" in f.name:
        continue
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            cases.append((r["id"], r["question"], r["scenario"]))
print("кейсов:", len(cases))

def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)   # macOS: байты

import torch

torch.set_num_threads(torch_threads)
base = rss_mb()
t = time.time()
from sentence_transformers import SentenceTransformer

m = SentenceTransformer("cointegrated/rubert-tiny2", device="cpu")
print(f"загрузка {time.time()-t:.1f} с; пиковая память процесса {rss_mb():.0f} МБ (до загрузки {base:.0f})")
qs = [q for _, q, _ in cases]
m.encode(qs[:2])
t = time.time()
for q in qs:
    m.encode([q], normalize_embeddings=True)
per = (time.time() - t) / len(qs) * 1000
print(f"задержка на вопрос: {per:.1f} мс ({torch_threads} поток)")
E = m.encode(qs, normalize_embeddings=True)
# kNN leave-one-out по сценарию
import numpy as np

S = E @ E.T
np.fill_diagonal(S, -1)
pred = [cases[int(S[i].argmax())][2] for i in range(len(cases))]
acc = sum(p == c[2] for p, c in zip(pred, cases)) / len(cases)
print(f"kNN (1 сосед) по сценарию, leave-one-out: {acc:.2f}")
# правила проекта
sys.path.insert(0, src)
from dms_core.scenarios.route import rule_scenario

rules = [rule_scenario(q).value for q in qs]
racc = sum(p == c[2] for p, c in zip(rules, cases)) / len(cases)
print(f"правила route.rule_scenario: {racc:.2f}")
print(f"итоговая пиковая память {rss_mb():.0f} МБ")
