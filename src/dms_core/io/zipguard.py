"""Защита от zip-бомб в docx/xlsx (ревизия w16-sec): одна проверка пакета для всех, кто открывает OOXML —
sniff_format (dms_api.service), чтение docx (docx_reader), xlsx (xlsx_reader) и таблиц docx (table_mapping).

Что проверяется ДО распаковки, по оглавлению архива (ZipInfo):
  * частей не больше MAX_ZIP_PARTS (каталог ЛПУ ГК 1 — 2181 часть, по листу на регион);
  * объявленный распакованный размер одной части не больше MAX_PART_UNPACKED (самая большая часть комплекта —
    word/document.xml программы ГК 2, ~23 МБ);
  * сумма объявленных размеров не больше MAX_TOTAL_UNPACKED (каталог ЛПУ ГК 1 — ~54 МБ);
  * степень сжатия части больше RATIO_MIN_PART: распаковано/сжато не больше MAX_RATIO (в комплекте максимум ~88 —
    вложенный oleObject; XML — 13–19).
Объявленный размер можно подделать, поэтому сами части читаются потоком (read_part): чтение обрывается, как только
распаковано больше предела. Кроме того, zipfile сам не выдаёт больше объявленного размера части (и проверяет CRC),
поэтому для openpyxl, который читает пакет сам, достаточно проверки оглавления.
Ошибка — InputError("too_large" | "bad_file") с сообщением по-русски (io/errors.py).
"""
from __future__ import annotations

import zipfile

from dms_core.io.errors import InputError

MAX_ZIP_PARTS = 10_000
MAX_PART_UNPACKED = 25 * 2**20
MAX_TOTAL_UNPACKED = 60 * 2**20
MAX_RATIO = 100
RATIO_MIN_PART = 2**20
_CHUNK = 1 << 16


def _mb(n: int) -> int:
    return n // 2**20


def check_zip(z: zipfile.ZipFile, kind: str = "файл") -> list[zipfile.ZipInfo]:
    """Оглавление пакета в пределах (см. шапку модуля) → список частей; иначе InputError по-русски.
    kind — как назвать файл в сообщении («DOCX», «XLSX», «файл»)."""
    infos = z.infolist()
    if len(infos) > MAX_ZIP_PARTS:
        raise InputError("too_large", f"В архиве {kind} больше {MAX_ZIP_PARTS} частей: разбейте файл на части.")
    total = 0
    for i in infos:
        if i.file_size > MAX_PART_UNPACKED:
            raise InputError("too_large", f"Часть {kind} в распакованном виде больше {_mb(MAX_PART_UNPACKED)} МБ: "
                                          "файл слишком большой или повреждён.")
        if i.file_size > RATIO_MIN_PART and i.file_size > MAX_RATIO * max(i.compress_size, 1):
            raise InputError("too_large", f"Часть {kind} сжата подозрительно сильно (больше чем в {MAX_RATIO} раз): "
                                          "похоже на повреждённый или специально собранный файл.")
        total += i.file_size
    if total > MAX_TOTAL_UNPACKED:
        raise InputError("too_large", f"{kind} в распакованном виде больше {_mb(MAX_TOTAL_UNPACKED)} МБ: "
                                      "разбейте файл на части.")
    return infos


def read_part(z: zipfile.ZipFile, name: str | zipfile.ZipInfo, limit: int = MAX_PART_UNPACKED,
              kind: str = "файл") -> bytes:
    """Часть архива потоком с обрывом при превышении limit (объявленному размеру не верим). KeyError — части нет."""
    info = name if isinstance(name, zipfile.ZipInfo) else z.getinfo(name)
    if info.file_size > limit:
        raise InputError("too_large", f"Часть {kind} в распакованном виде больше {_mb(limit)} МБ: "
                                      "файл слишком большой или повреждён.")
    buf = bytearray()
    with z.open(info) as f:
        while chunk := f.read(_CHUNK):
            buf += chunk
            if len(buf) > limit:
                raise InputError("too_large", f"Часть {kind} в распакованном виде больше {_mb(limit)} МБ: "
                                              "файл слишком большой или повреждён.")
    return bytes(buf)


class PartReader:
    """Чтение частей одного пакета с общим бюджетом MAX_TOTAL_UNPACKED на все прочитанные части."""

    def __init__(self, z: zipfile.ZipFile, kind: str = "файл", budget: int = MAX_TOTAL_UNPACKED):
        check_zip(z, kind)
        self.z, self.kind, self.left = z, kind, budget

    def read(self, name: str) -> bytes:
        """Часть name (KeyError — нет такой части); сверх бюджета — InputError."""
        data = read_part(self.z, name, min(MAX_PART_UNPACKED, self.left), self.kind)
        self.left -= len(data)
        return data
