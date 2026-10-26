#!/usr/bin/env python3
"""summary.json прогона scripts/eval.py → docs/test_report.md (план §10.1, ADR 0008: только ID, метки, числа).

Запуск из app/:
  uv run python scripts/make_test_report.py                       # последний не-holdout прогон в data/derived/eval
  uv run python scripts/make_test_report.py data/derived/eval/<run_id>/summary.json [--out docs/test_report.md]
Прогон holdout — только с --holdout (автор), и тогда в отчёте лишь свод без построчной таблицы.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from eval.report import ReportError, render


def _latest(base: Path) -> Path | None:
    found = [p for p in base.glob("*/summary.json") if not p.parent.name.startswith("holdout-")]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def main(argv: list[str] | None = None) -> int:
    from dms_core.config import derived_dir

    ap = argparse.ArgumentParser(description="docs/test_report.md из summary.json")
    ap.add_argument("summary", nargs="?", type=Path)
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "test_report.md")
    ap.add_argument("--holdout", action="store_true", help="разрешить прогон holdout (только свод)")
    a = ap.parse_args(argv)
    path = a.summary or _latest(derived_dir() / "eval")
    if not path or not path.is_file():
        print("нет summary.json: сначала uv run python scripts/eval.py", file=sys.stderr)
        return 2
    try:
        text = render(json.loads(path.read_text(encoding="utf-8")), allow_holdout=a.holdout)
    except ReportError as e:
        print(e, file=sys.stderr)
        return 2
    a.out.write_text(text, encoding="utf-8")
    print(f"{a.out} ← {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
