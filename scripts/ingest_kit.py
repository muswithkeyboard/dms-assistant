#!/usr/bin/env python3
"""Импорт комплекта в data/derived/kit.sqlite и отчёт data/derived/ingest_report.json (docs/CONTRACTS.md §5).

kit.scan() → для каждой ГК общие документы (adapters.parse_group_docs → KitStore.save_group; правила и каталог —
не условия договора) → для каждого договора adapters.parse_contract → KitStore.save → статусы документов
received → extracted → checked → indexed → ready (ТЗ п. 7.1) с проверкой полноты по типу документа и сверкой
карточки (_mark_statuses). Договор без адаптера — пропуск; сбой разбора — статус error и текст в отчёте.
В лог и отчёт тексты документов не пишутся (`docs/rules.md` п. 4) — только идентификаторы, числа и предупреждения.

После комплекта — договоры клиентов из реестра сервиса (data/app/app.sqlite, ADR 0011): пересборка базы их не теряет.

Запуск из app/:  uv run python scripts/ingest_kit.py [--db PATH] [--report PATH] [--root PATH] [--app-db PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from dms_core import adapters, kit, onboarding  # noqa: F401 — adapters подменяется в тестах
from dms_core.config import derived_dir, kit_dir
from dms_core.store import KitStore

# Общий конвейер разбора договора — dms_core/onboarding.py (им же пользуется подключение клиента, ADR 0011).
# Старые имена оставлены для тестов и прежних вызовов.
PREMIUM_TOLERANCE = onboarding.PREMIUM_TOLERANCE
ERROR_MARKERS = onboarding.ERROR_MARKERS
COMPLETENESS = onboarding.COMPLETENESS
adapter_errors = onboarding.adapter_errors
_premium_check = onboarding.premium_check
_completeness = onboarding.completeness
_card_problems = onboarding.card_problems
_mark_statuses = onboarding.mark_statuses
_mark_group_doc = onboarding.mark_group_doc
_doc_entry = onboarding.doc_entry


def _status_summary(report: dict[str, Any]) -> dict[str, Any]:
    """Сводка статусов по всем документам договоров и ГК: сколько ready / checked / extracted / error."""
    out: dict[str, Any] = {"contracts": {}, "groups": {}}
    for key in ("contracts", "groups"):
        for e in report[key].values():
            for d in e.get("documents", []):
                out[key][d["status"]] = out[key].get(d["status"], 0) + 1
    return out


def ingest(root: Path, store: KitStore, registry: Any = None, clients_root: Path | None = None,
           previous: KitStore | None = None) -> dict[str, Any]:
    """Комплект → база. registry (реестр клиентов, AppStore) задан — после комплекта заново разбираются договоры
    клиентов (onboarding.reparse_all): пересборка базы их не теряет (ADR 0011 п. 5). previous — прежняя база (сборка
    в новый файл): договор клиента, файлы которого изменились после обработки, переносится из неё без изменений."""
    t0 = time.time()
    report: dict[str, Any] = {"root": str(root), "contracts": {}, "groups": {}, "warnings": []}
    groups = kit.scan(root)
    if not groups:
        report["warnings"].append(f"комплект не найден: {root}")
    for g in groups:
        report["groups"][g.id] = onboarding.ingest_group_docs(g, root, store) if g.files else \
            {"files": [], "documents": []}
        for kc in g.contracts:
            report["contracts"][kc.id] = onboarding.ingest_contract(kc, root, store)
    if registry is not None:
        report["clients"] = onboarding.reparse_all(store, registry, root=clients_root, previous=previous)
    report["statuses"] = _status_summary(report)
    stats = store.dedup_stats()
    report["dedup"] = {"clauses": stats.clauses, "unique_texts": stats.unique_texts, "chars_total": stats.chars_total,
                       "chars_saved": stats.chars_saved, "saved_ratio": round(stats.saved_ratio, 4)}
    report["db_bytes"] = store.size_bytes()
    report["seconds"] = round(time.time() - t0, 2)
    return report


def _registry(path: Path | None, default: bool) -> Any:
    """Реестр клиентов сервиса: --app-db или (сборка базы по умолчанию, без --db) data/app/app.sqlite, если он есть.
    Сборка в чужой файл (--db) без --app-db реестр сервиса не трогает."""
    from dms_core.config import data_dir

    p = path or (data_dir() / "app" / "app.sqlite" if default else None)
    if p is None or not p.is_file():
        return None
    from dms_api.store import AppStore

    return AppStore(p)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Импорт комплекта в SQLite и отчёт ingest_report.json")
    ap.add_argument("--root", type=Path, default=None, help="папка «Документы» комплекта")
    ap.add_argument("--db", type=Path, default=None, help="путь к kit.sqlite")
    ap.add_argument("--report", type=Path, default=None, help="путь к ingest_report.json")
    ap.add_argument("--app-db", type=Path, default=None, help="реестр клиентов (app.sqlite) для договоров клиентов")
    args = ap.parse_args(argv)
    root = args.root or kit_dir()
    report_path = args.report or derived_dir() / "ingest_report.json"
    # Сборка во временный файл и атомарная замена: читатели (сервис, тесты) никогда не видят недописанную базу.
    target = args.db or derived_dir() / "kit.sqlite"
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    for p in (tmp, tmp.with_name(tmp.name + "-wal"), tmp.with_name(tmp.name + "-shm")):
        p.unlink(missing_ok=True)
    registry = _registry(args.app_db, default=args.db is None)
    # прежняя база — источник договоров клиентов, которые нельзя разобрать заново той же версией (reparse_all)
    previous = KitStore(target) if registry is not None and target.is_file() else None
    try:
        with KitStore(tmp) as store:
            report = ingest(root, store, registry, previous=previous)
    finally:
        if previous is not None:
            previous.close()
        if registry is not None:
            registry.close()
    # журнал WAL старой базы не должен достаться новому файлу
    for suffix in ("-wal", "-shm"):
        target.with_name(target.name + suffix).unlink(missing_ok=True)
    os.replace(tmp, target)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for e in report["contracts"].values() if e["status"] == "ok")
    gdocs = sum(len(g.get("documents", [])) for g in report["groups"].values())
    print(f"статусы документов договоров: {report['statuses']['contracts']}; документов ГК: {gdocs} "
          f"{report['statuses']['groups']}")
    print(f"договоров: {len(report['contracts'])}, импортировано: {ok}; "
          f"дедуп сэкономил {report['dedup']['chars_saved']} знаков; отчёт: {report_path}")
    return 0 if all(e["status"] != "error" for e in report["contracts"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
