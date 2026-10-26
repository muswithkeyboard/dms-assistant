"""Общие документы ГК (план §1 п. 4, docs/CONTRACTS.md §3): правила страхования СК и каталог страховщика.

Это НЕ условия договора (`docs/rules.md` п. 0а): документы и пункты сохраняются с contract_id=None и group_id ГК
(KitStore.save_group) и в инструменты договора не попадают — они нужны, чтобы оператор видел «что загружено».
  * rules (PDF, ГК 1 — правила НС Зетты): io/pdf_reader → пункты по нумерации «1.», «1.1.», «2.6.1.» в начале
    строки; текст до первого номера и строки без номера продолжают текущий пункт (в том числе через границу
    страницы); page — страница, где пункт начинается. Пункты правил в ответах о договоре не используются.
  * catalog («ЛПУ_Регионы Стандарт.xlsx»): файл читается (io/xlsx_reader) только чтобы подтвердить, что он
    цел; пунктов и строк клиник нет — каталог СК не перечень клиник договора (config/precedence.yaml:
    not_contract_terms). Документ получает предупреждение CATALOG_WARNING.
Сбой чтения файла → документ со статусом error и текстом ошибки в warnings (ТЗ п. 7.1), импорт не падает.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..constants import DocStatus
from ..io.errors import InputError
from ..io.pdf_reader import read_pdf
from ..io.xlsx_reader import read_xlsx
from ..kit import KitFile, KitGroup
from ..schemas import Clause, Document
from .base import abs_path, make_document, text_hash

CATALOG_WARNING = "общий каталог СК, не часть договора: в ответах о договоре не используется"
RULES_WARNING = "общие правила СК, не условия договора: в ответах о договоре не используются"

# номер пункта в начале строки: «1. ОБЩИЕ», «1.1.Настоящие», «2.6.1. …»; первая часть ≤ 2 цифр — чтобы год
# («2023. …») или сумма не стали номером; после номера — не цифра (иначе это дробь «1.5.10»)
_NUM = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,3}){0,4})\.\s*(?=[^\d\s.])")
_PAGE_NO = re.compile(r"^\s*(?:стр\.?\s*)?\d{1,3}\s*(?:из\s*\d{1,3})?\s*$", re.IGNORECASE)


def split_rules(pages: list[str], doc_id: str, title: str) -> list[Clause]:
    """Пункты правил из текста PDF по страницам (номер — из начала строки, текст — до следующего номера)."""
    out: list[Clause] = []
    cur: dict | None = None

    def flush() -> None:
        if cur is None:
            return
        text = " ".join(cur["lines"]).strip()
        if not text:
            return
        n = len(out)
        number = cur["number"]
        out.append(Clause(id=f"{doc_id}#{number or 'p' + str(cur['page'])}@{n}", doc_id=doc_id, contract_id=None,
                          number=number, path=" › ".join(x for x in (title, number) if x), text=text,
                          page=cur["page"], text_hash=text_hash(text)))

    for pno, page in enumerate(pages, start=1):
        for line in page.splitlines():
            if not line.strip() or _PAGE_NO.match(line):
                continue                          # пустая строка или колонтитул с номером страницы
            m = _NUM.match(line)
            if m:
                flush()
                cur = {"number": m.group(1), "page": pno, "lines": [line[m.end():].strip()]}
            elif cur is None:
                cur = {"number": None, "page": pno, "lines": [line.strip()]}
            else:
                cur["lines"].append(line.strip())
    flush()
    return out


def _error_doc(doc: Document, e: Exception) -> Document:
    msg = e.message if isinstance(e, InputError) else f"{type(e).__name__}"
    doc.status = DocStatus.error
    doc.warnings.append(f"ошибка чтения: {msg}")
    return doc


def _parse_file(kf: KitFile, group: KitGroup, root: Path, n: int) -> tuple[Document, list[Clause]]:
    doc = make_document(kf, group.id, contract_id=None, group_id=group.id, n=n)
    path = abs_path(root, kf)
    if kf.doc_type == "rules":
        doc.warnings.append(RULES_WARNING)
        if kf.fmt != "pdf":
            doc.status = DocStatus.error
            doc.warnings.append(f"правила в формате {kf.fmt} не разбираются (ожидается PDF)")
            return doc, []
        try:
            pages = read_pdf(path)
        except Exception as e:  # noqa: BLE001 — сбой одного файла ГК виден в отчёте, импорт продолжается
            return _error_doc(doc, e), []
        return doc, split_rules(pages, doc.id, kf.title)
    if kf.doc_type == "catalog":
        doc.warnings.append(CATALOG_WARNING)
        try:
            sheets = read_xlsx(path) if kf.fmt == "xlsx" else None
        except Exception as e:  # noqa: BLE001
            return _error_doc(doc, e), []
        if sheets is None:
            doc.status = DocStatus.error
            doc.warnings.append(f"каталог в формате {kf.fmt} не читается (ожидается xlsx)")
        elif not any(sheets.values()):
            doc.status = DocStatus.error
            doc.warnings.append("каталог пуст: нет ни одной строки")
        return doc, []
    doc.warnings.append(f"общий документ ГК типа {kf.doc_type} не разбирается")
    return doc, []


def parse_group_docs(group: KitGroup, root: Path) -> tuple[list[Document], list[Clause]]:
    """Документы и пункты общих файлов ГК (contract_id=None, group_id=group.id) — для KitStore.save_group."""
    docs: list[Document] = []
    clauses: list[Clause] = []
    seen: dict[str, int] = {}
    for kf in group.files:
        n = seen.get(kf.doc_type, 0)
        seen[kf.doc_type] = n + 1
        doc, cls = _parse_file(kf, group, root, n)
        docs.append(doc)
        clauses.extend(cls)
    return docs, clauses
