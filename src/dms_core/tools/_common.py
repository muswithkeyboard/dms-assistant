"""Общее для инструментов агента (план §6.3): хранилище по умолчанию и источник у каждого результата.

Правило инструментов: `contract_id` — первый аргумент; результат — JSON-совместимые данные, где у каждого факта есть
`source` (doc_id + номер/путь пункта или source_ref строки таблицы). Модель цитирует только полученное отсюда.
"""
from __future__ import annotations

import threading
from typing import Any

from ..schemas import CareMatrix, Clause, ClinicRow
from ..store.db import KitStore, _require_contract_id

_DEFAULT: KitStore | None = None
_DEFAULT_LOCK = threading.Lock()


def default_store() -> KitStore:
    """data/derived/kit.sqlite — один объект на процесс. Базы нет — пустое хранилище в памяти: чтение по умолчанию
    не создаёт файл (иначе пустая база в data/derived выдаёт себя за разобранный комплект; создаёт базу только импорт).
    Хранилище в памяти не запоминается навсегда (ревью 29.10.2026 п. 1): появилась база (импорт скриптом) — следующий
    вызов открывает её; импорт из интерфейса переводит тот же объект в файл сам (KitStore.replace_file)."""
    from ..store.db import default_path

    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None or (str(_DEFAULT.path) == ":memory:" and default_path().exists()):
            _DEFAULT = KitStore() if default_path().exists() else KitStore(":memory:")
        return _DEFAULT


def resolve(contract_id: str, store: KitStore | None) -> tuple[str, KitStore]:
    """Проверка contract_id (ADR 0005) и выбор хранилища."""
    return _require_contract_id(contract_id), (store or default_store())


def clause_out(c: Clause, **extra: Any) -> dict[str, Any]:
    return {
        "clause_id": c.id, "number": c.number, "path": c.path, "program": c.program, "text": c.text,
        "footnotes": c.footnotes,
        "source": {"doc_id": c.doc_id, "contract_id": c.contract_id, "number": c.number, "path": c.path,
                   "clause_id": c.id},
        **extra,
    }


def clinic_out(r: ClinicRow, **extra: Any) -> dict[str, Any]:
    data = r.model_dump(mode="json", exclude={"id", "doc_id", "source_ref"})
    data["source"] = {"doc_id": r.doc_id, "contract_id": r.contract_id, "source_ref": r.source_ref, "row_id": r.id}
    data.update(extra)
    return data


def care_out(m: CareMatrix) -> dict[str, Any]:
    return {"program": m.program, "care_type": m.care_type, "present": m.present,
            "source": {"contract_id": m.contract_id, "source_refs": list(m.source_refs)}}


def contract_source(store: KitStore, contract_id: str) -> dict[str, Any]:
    """Источник карточки договора — документ типа «договор» (карточку строит адаптер из него)."""
    docs = [d for d in store.documents(contract_id) if d.doc_type.value == "contract"]
    return {"doc_id": docs[0].id if docs else None, "contract_id": contract_id, "card": True}
