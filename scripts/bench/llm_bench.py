"""Запуск: python3 scripts/bench/llm_bench.py ../data/derived/gold qwen3:1.7b [--cpu] [--n N]
Замер маленьких моделей Ollama для разбора вопроса (сценарий + слоты) под сервер 2–4 ГБ:
2 потока CPU, контекст 2048, без «размышлений», строгий JSON по схеме (format). Кейсы — без holdout."""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

gold, model = Path(sys.argv[1]), sys.argv[2]
CPU = "--cpu" in sys.argv                     # без видеокарты (num_gpu: 0) — как на сервере
N = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else None
cases = []
for f in sorted(gold.glob("*.jsonl")):
    if "holdout" in f.name:
        continue
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line); cases.append(r)
SCEN = ["sc01", "sc02_info", "sc02_clinic", "sc02_add_clinic"]
schema = {"type": "object", "properties": {
    "scenario": {"type": "string", "enum": SCEN},
    "service": {"type": ["string", "null"]}, "clinic": {"type": ["string", "null"]},
    "program": {"type": ["string", "null"]}, "city": {"type": ["string", "null"]},
    "date": {"type": ["string", "null"]}}, "required": ["scenario", "service", "clinic", "program", "city", "date"]}
SYSTEM = ("Разбери вопрос менеджера ДМС. scenario: sc01 — отказ клиники или «положено ли / входит ли услуга»; "
          "sc02_info — условия договора, взносы, франшиза, сроки, сравнение программ; sc02_clinic — подбор клиники; "
          "sc02_add_clinic — добавить клинику в программу. Поля service, clinic, program, city, date — только если "
          "они прямо названы в вопросе, иначе null. Ничего не придумывай.")

def call(q):
    body = {"model": model, "stream": False, "think": False, "format": schema,
            "options": {"temperature": 0, "num_thread": 2, "num_ctx": 2048, **({"num_gpu": 0} if CPU else {})},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": q}]}
    req = urllib.request.Request("http://127.0.0.1:11434/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=600))

cases = cases[:N] if N else cases
call("тест")                                   # загрузка модели
ps = subprocess.run(["ollama", "ps"], check=False, capture_output=True, text=True).stdout
ok = valid = grounded_bad = 0
lat, toks = [], []
for c in cases:
    t = time.time(); r = call(c["question"]); lat.append(time.time() - t)
    toks.append(r.get("eval_count", 0) / max(r.get("eval_duration", 1) / 1e9, 1e-9))
    try:
        d = json.loads(r["message"]["content"])
    except json.JSONDecodeError:
        continue                               # невалидный JSON — считается в «валидный JSON N/M»
    valid += 1
    ok += d.get("scenario") == c["scenario"]
    q = c["question"].casefold()
    for k in ("clinic", "program", "city"):
        v = d.get(k)
        if v and v.casefold()[:6] not in q:     # значение, которого нет в вопросе, — выдумка
            grounded_bad += 1
lat.sort()
print(f"{model}: кейсов {len(cases)}; валидный JSON {valid}/{len(cases)}; сценарий верно {ok}/{len(cases)} "
      f"({ok/len(cases):.2f}); слотов не из вопроса {grounded_bad}; задержка p50 {lat[len(lat)//2]:.1f} с, "
      f"p95 {lat[int(len(lat)*.95)]:.1f} с; генерация ~{sum(toks)/len(toks):.1f} ток/с (2 потока)")
print(ps.strip().splitlines()[-1] if ps.strip() else "ollama ps: —")
