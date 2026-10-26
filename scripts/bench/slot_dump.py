"""Слоты правил (route.rule_slots) по вопросам наборов ревизии — для построчного сравнения двух версий (этап w7-fix).

Печатается id и слоты clinic / street / city / service / care_type — без текстов вопросов. Сравнение:
  uv run python scripts/bench/slot_dump.py набор1.jsonl … > до.tsv;  (после правки) > после.tsv;  diff до.tsv после.tsv
Запуск из app/.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("DMS_ENGINE_MODE", "guided")

from dms_core.schemas import ChatContext

KEYS = ("clinic", "street", "city", "service", "care_type")


def main(argv: list[str]) -> int:
    from dms_core.scenarios.route import rule_slots
    from dms_core.store import KitStore, default_path

    store = KitStore(default_path())
    for path in argv:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if "question" in r:          # эталоны: поля question / context
                c = r.get("context") or {}
                r = {"id": r["id"], "q": r["question"], "g": c.get("group_id"), "c": c.get("contract_id"),
                     "p": c.get("program")}
            ctx = ChatContext(group_id=r.get("g"), contract_id=r.get("c"), program=r.get("p"))
            s = rule_slots(r["q"], ctx, store)
            nc = "no_clinic" if r.get("no_clinic") else ""
            print("\t".join([r["id"], nc, *(s.get(k, "") for k in KEYS)]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
