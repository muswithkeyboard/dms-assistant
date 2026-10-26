#!/usr/bin/env python3
"""Прогон эталонов через движок → data/derived/eval/<run_id>/<case>.json + summary.json (план §10.1).

Эталоны — data/derived/gold/k10_k19.jsonl и own_gk*.jsonl (ADR 0008). holdout.jsonl — только с --holdout
(запускает автор; при настройке промптов не открывается, правило 6). Модель — --provider/--model/--base-url
через dms_core.llm.get_llm; без --provider движок получает llm=None (режим extractive).

Запуск из app/:
  uv run python scripts/eval.py                              # все настроечные кейсы, без модели
  uv run python scripts/eval.py --provider openai_compat --model qwen3:8b --base-url http://127.0.0.1:11434/v1 -j 2
  uv run python scripts/eval.py --ids K10,K12 --no-resume
  uv run python scripts/eval.py --fake                       # фейковый движок (проверка конвейера без модели)
В журнал печатаются только ID кейсов, статус и время.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from eval import runner


def _get_llm(provider: str | None, model: str | None, base_url: str | None):
    if not provider:
        return None
    try:
        from dms_core.llm import get_llm  # type: ignore[attr-defined]
    except ImportError:
        raise SystemExit("нет dms_core.llm.get_llm (этап llm волны 2) — запустите без --provider") from None
    llm = get_llm(provider=provider, model=model, base_url=base_url)
    if llm is None:
        raise SystemExit(f"провайдер {provider} недоступен (нет ключа в .env?)")
    return llm


def _engine(name: str | None, fake: bool):
    if fake:
        from eval.fake_engine import answer

        return answer
    mod, _, fn = (name or "dms_core.engine:answer").partition(":")
    return getattr(importlib.import_module(mod), fn or "answer")


def main(argv: list[str] | None = None) -> int:
    from dms_core.config import derived_dir

    ap = argparse.ArgumentParser(description="Прогон эталонов и метрики (план §10.1)")
    ap.add_argument("--provider"), ap.add_argument("--model"), ap.add_argument("--base-url")
    ap.add_argument("--judge-provider", help="модель-судья ключевых фактов (по умолчанию выключена)")
    ap.add_argument("--judge-model"), ap.add_argument("--judge-base-url")
    ap.add_argument("--ids", help="только эти кейсы, через запятую")
    ap.add_argument("--gold-dir", type=Path, default=None, help="по умолчанию data/derived/gold")
    ap.add_argument("--holdout", action="store_true", help="прогон holdout.jsonl (только автор)")
    ap.add_argument("--run-id"), ap.add_argument("--out", type=Path, default=None, help="по умолчанию data/derived/eval")
    ap.add_argument("-j", "--workers", type=int, default=1)
    ap.add_argument("--no-resume", action="store_true", help="не брать результаты из кэша")
    ap.add_argument("--date", help="дата для кейсов без context.date (ГГГГ-ММ-ДД); по умолчанию — сегодня")
    ap.add_argument("--engine", help="модуль:функция движка, по умолчанию dms_core.engine:answer")
    ap.add_argument("--fake", action="store_true", help="фейковый движок eval/fake_engine.py")
    ap.add_argument("--no-store", action="store_true", help="не открывать data/derived/kit.sqlite")
    a = ap.parse_args(argv)

    gold_dir = a.gold_dir or derived_dir() / "gold"
    paths = runner.gold_files(gold_dir, holdout=a.holdout)
    if not paths:
        print(f"нет эталонов в {gold_dir}", file=sys.stderr)
        return 2
    cases = runner.load_cases(paths, holdout=a.holdout, ids=a.ids.split(",") if a.ids else None)
    if not cases:
        print("нет кейсов для прогона", file=sys.stderr)
        return 2
    llm = _get_llm(a.provider, a.model, a.base_url)
    judge = None
    if a.judge_provider:
        from eval.judge import make_judge

        judge = make_judge(_get_llm(a.judge_provider, a.judge_model, a.judge_base_url))
    store = None
    if not a.no_store:
        from dms_core.store import KitStore, default_path

        if default_path().is_file():
            store = KitStore(default_path())
    date_fallback = dt.date.fromisoformat(a.date) if a.date else dt.date.today()
    run_id = a.run_id or runner.default_run_id(a.provider or ("fake" if a.fake else None), a.model,
                                               holdout=a.holdout)
    if a.holdout and not run_id.startswith("holdout-"):
        run_id = "holdout-" + run_id
    out_dir = (a.out or derived_dir() / "eval") / run_id
    summary = runner.run(cases, _engine(a.engine, a.fake), out_dir, llm=llm, store=store, workers=a.workers,
                         resume=not a.no_resume, date_fallback=date_fallback, holdout=a.holdout,
                         run_meta={"base_url": a.base_url, "fake_engine": a.fake}, gold_paths=paths, judge=judge,
                         log=lambda m: print(m, flush=True))
    o = summary["metrics"]["overall"]
    print(f"Итог: кейсов {o['cases']}, пройдено {o['passed']}, сбоев {o['errors']}, "
          f"метка {o['label']['correct']}/{o['label']['labelled']}, ссылки {o['citations']['found']}/"
          f"{o['citations']['required']} → {out_dir / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
