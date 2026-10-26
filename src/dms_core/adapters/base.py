"""Общее для адаптеров форматов СК (план §6.2, docs/CONTRACTS.md §3).

Адаптер получает опись договора (kit.KitContract) и превращает его документы в структуру: карточку договора,
пункты с номерами, строки клиник и матрицу «программа × вид помощи». Каждый адаптер — отдельный модуль
(zetta.py, renaissance.py, sogaz.py, generic.py) с функциями `matches(kc, root) -> bool` и
`parse(kc, root) -> ParsedContract`; выбор — в adapters/__init__.py по признакам документа, не по имени папки.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..config import load_yaml
from ..constants import DocStatus, DocType
from ..kit import KitContract, KitFile
from ..schemas import CareMatrix, Clause, ClinicRow, Contract, Document


@dataclass
class ParsedContract:
    contract: Contract
    documents: list[Document] = field(default_factory=list)
    clauses: list[Clause] = field(default_factory=list)
    clinics: list[ClinicRow] = field(default_factory=list)
    care: list[CareMatrix] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Ошибки сверки (не сошлись премии, нет перечня клиник…): документ не получает «ready» (ТЗ п. 7.1).
    # Явный канал вместо распознавания по тексту предупреждений; текст ошибки — с doc_id, если он известен.
    errors: list[str] = field(default_factory=list)


def doc_id(owner_id: str, doc_type: str, n: int = 0) -> str:
    """c09.program, c09.program.2 (второй документ того же типа), gk1.rules."""
    return f"{owner_id}.{doc_type}" + (f".{n + 1}" if n else "")


def make_document(kf: KitFile, owner_id: str, *, contract_id: str | None, group_id: str | None,
                  n: int = 0) -> Document:
    """Document по файлу описи; уровень приоритета — из config/precedence.yaml."""
    levels = load_yaml("precedence.yaml")["levels"]
    return Document(
        id=doc_id(owner_id, kf.doc_type, n), contract_id=contract_id, group_id=group_id, title=kf.title,
        doc_type=DocType(kf.doc_type), precedence_level=levels.get(kf.doc_type), source_path=kf.path,
        original_format=kf.fmt, sha256=kf.sha256, uploaded_by="kit-import",
        uploaded_at=datetime.now(UTC), status=DocStatus.extracted,
    )


def text_hash(text: str) -> str:
    """Хэш нормализованного текста (пробелы схлопнуты) — для дедупа одинаковых пунктов (store/dedup.py)."""
    return hashlib.sha1(re.sub(r"\s+", " ", text).strip().casefold().encode("utf-8")).hexdigest()[:16]


def files_of(kc: KitContract, doc_type: str) -> list[KitFile]:
    return [f for f in kc.files if f.doc_type == doc_type]


def abs_path(root: Path, kf: KitFile) -> Path:
    """Путь к файлу на диске. Имена в описи — в NFC, а на macOS файлы могут лежать в NFD: ищем оба варианта."""
    import unicodedata

    p = root / kf.path
    if p.exists():
        return p
    alt = root / unicodedata.normalize("NFD", kf.path)
    return alt if alt.exists() else p
