"""Чтение xlsx (docs/CONTRACTS.md §2): openpyxl read-only, значения ячеек как строки, многострочные ячейки — как
есть (с переводами строк: в ГК 3 условие «30 км от МКАД» спрятано в многострочной ячейке, docs/facts/kit/gk3.md).

Защита от zip-бомбы перенесена из Justmedit (reference/justmedit-code/table.py, _check_xlsx): до разбора архив
открывается, частей не больше MAX_XLSX_PARTS, сумма объявленных распакованных размеров не больше max_unpacked
(zipfile не выдаёт больше объявленного размера части, поэтому сумма ограничивает и фактическую распаковку).
"""
from __future__ import annotations

import datetime as dt
import zipfile
from pathlib import Path

from dms_core.io.errors import InputError

# Каталог ЛПУ ГК 1 — 2181 часть (по листу на регион), ~54 МБ распакованного; предел с запасом.
MAX_XLSX_PARTS = 10_000


def _check_xlsx(path: Path, max_unpacked: int | None) -> None:
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, ValueError) as e:
        raise InputError("bad_file", "Файл XLSX повреждён или это не XLSX.") from e
    if len(infos) > MAX_XLSX_PARTS:
        raise InputError("too_large", f"В архиве XLSX больше {MAX_XLSX_PARTS} частей.")
    if max_unpacked is not None:
        total = sum(i.file_size for i in infos)
        if total > max_unpacked:
            raise InputError("too_large", f"XLSX в распакованном виде больше {max_unpacked // 2**20} МБ: "
                                          "разбейте файл на части.")


def _cell(v: object) -> str | None:
    """Значение ячейки → строка; пустая ячейка → None. Даты — ISO, целые числа — без «.0»."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "ИСТИНА" if v else "ЛОЖЬ"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, dt.datetime):
        return v.date().isoformat() if v.time() == dt.time() else v.isoformat(sep=" ")
    if isinstance(v, dt.date):
        return v.isoformat()
    s = str(v)
    return s if s != "" else None


def read_xlsx(path: Path | str, max_unpacked: int = 200 * 2**20) -> dict[str, list[list[str | None]]]:
    """Лист → строки (списки значений ячеек). Хвостовые пустые ячейки строки и хвостовые пустые строки листа
    отбрасываются; пустые строки внутри листа сохраняются (номер строки = индекс + 1)."""
    path = Path(path)
    _check_xlsx(path, max_unpacked)
    import openpyxl  # импорт здесь: модуль тяжёлый

    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as e:
        raise InputError("bad_file", "Файл XLSX не открывается: повреждён или защищён паролем.") from e
    out: dict[str, list[list[str | None]]] = {}
    try:
        for ws in wb.worksheets:
            rows: list[list[str | None]] = []
            for raw in ws.iter_rows(values_only=True):
                row = [_cell(v) for v in raw]
                while row and row[-1] is None:
                    row.pop()
                rows.append(row)
            while rows and not rows[-1]:
                rows.pop()
            out[ws.title] = rows
    finally:
        wb.close()
    return out
