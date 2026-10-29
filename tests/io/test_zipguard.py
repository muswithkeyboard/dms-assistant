"""Защита от zip-бомб (w16-sec, п. 1): общая проверка io/zipguard для sniff_format, docx_reader, xlsx_reader и
таблиц docx (table_mapping). Свои маленькие архивы, без сети и без комплекта."""
from __future__ import annotations

import io
import struct
import tempfile
import zipfile
from pathlib import Path

from dms_core.io import zipguard
from dms_core.io.errors import InputError

CT = "<Types/>"


def _zip(entries: dict[str, bytes], main: str = "word/document.xml") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CT)
        for k, v in entries.items():
            z.writestr(k, v)
        if main not in entries:
            z.writestr(main, "<x/>")
    return buf.getvalue()


def _file(data: bytes, suffix: str) -> Path:
    p = Path(tempfile.mkdtemp(prefix="dms_zip_")) / f"a{suffix}"
    p.write_bytes(data)
    return p


def _raises(fn, code: str = "too_large") -> InputError:
    try:
        fn()
    except InputError as e:
        assert e.code == code, e.code
        assert any("а" <= ch <= "я" for ch in e.message.lower()), e.message      # сообщение по-русски
        return e
    raise AssertionError(f"ожидалась InputError {code}")


# 26 МБ пробелов сжимаются в ~26 КБ: и часть больше предела, и степень сжатия ~1000
BIG_XML = b"<w:document>" + b" " * (26 * 2**20) + b"</w:document>"
# 2 МБ нулей: часть в пределах размера, но сжата в сотни раз
RATIO_XML = b"\0" * (2 * 2**20)


def test_check_zip_limits_part_total_ratio():
    with zipfile.ZipFile(io.BytesIO(_zip({"word/document.xml": BIG_XML}))) as z:
        assert "МБ" in _raises(lambda: zipguard.check_zip(z, "DOCX")).message
    with zipfile.ZipFile(io.BytesIO(_zip({"word/document.xml": RATIO_XML}))) as z:
        assert "сжата" in _raises(lambda: zipguard.check_zip(z, "DOCX")).message
    # сумма: три части по 21 МБ случайных байтов был бы тяжёлый тест — уменьшаем предел суммы
    parts = {f"word/p{i}.xml": bytes(range(256)) * 4096 for i in range(3)}      # по 1 МБ, сжатие слабое
    with zipfile.ZipFile(io.BytesIO(_zip(parts))) as z:
        old = zipguard.MAX_TOTAL_UNPACKED
        zipguard.MAX_TOTAL_UNPACKED = 2 * 2**20
        try:
            assert "распакованном" in _raises(lambda: zipguard.check_zip(z, "DOCX")).message
        finally:
            zipguard.MAX_TOTAL_UNPACKED = old
        assert len(zipguard.check_zip(z, "DOCX")) == 5                    # в обычных пределах — проходит


def test_read_part_never_returns_more_than_declared():
    """Объявленный размер подделан (100 байт вместо 5 МБ): чтение не отдаёт больше объявленного и падает
    на проверке, а не распаковывает всё."""
    data = bytearray(_zip({"word/document.xml": b"a" * (5 * 2**20)}))
    name = b"word/document.xml"
    for sig, off in ((b"PK\x03\x04", 22), (b"PK\x01\x02", 24)):
        i = 0
        while (i := data.find(sig, i)) >= 0:
            n = struct.unpack_from("<H", data, i + (26 if sig == b"PK\x03\x04" else 28))[0]
            start = i + (30 if sig == b"PK\x03\x04" else 46)
            if bytes(data[start:start + n]) == name:
                struct.pack_into("<I", data, i + off, 100)
            i += 4
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as z:
        assert z.getinfo("word/document.xml").file_size == 100
        try:
            out = zipguard.read_part(z, "word/document.xml")
            assert len(out) <= 100
        except (zipfile.BadZipFile, InputError):
            pass                                          # CRC не сошёлся — тоже отказ, без распаковки 5 МБ
    # поток обрывается на пределе
    with zipfile.ZipFile(io.BytesIO(_zip({"word/document.xml": b"b" * 300_000}))) as z:
        _raises(lambda: zipguard.read_part(z, "word/document.xml", limit=100_000))
        assert len(zipguard.read_part(z, "word/document.xml")) == 300_000


def test_docx_reader_rejects_bomb():
    from dms_core.io.docx_reader import DocxError, read_docx

    for xml in (BIG_XML, RATIO_XML):
        e = _raises(lambda x=xml: read_docx(_file(_zip({"word/document.xml": x}), ".docx")))
        assert isinstance(e, DocxError)


def test_table_mapping_docx_grid_rejects_bomb():
    from dms_core.table_mapping import _docx_grid

    _raises(lambda: _docx_grid(_file(_zip({"word/document.xml": BIG_XML}), ".docx")))


def test_xlsx_reader_rejects_bomb():
    from dms_core.io.xlsx_reader import read_xlsx

    p = _file(_zip({"xl/worksheets/sheet1.xml": RATIO_XML}, main="xl/workbook.xml"), ".xlsx")
    assert "сжата" in _raises(lambda: read_xlsx(p)).message


def test_sniff_format_bomb_is_413():
    from dms_api import service

    for xml in (BIG_XML, RATIO_XML):
        try:
            service.sniff_format("Программа.docx", _zip({"word/document.xml": xml}))
            raise AssertionError("ожидалась ошибка размера")
        except service.ServiceError as e:
            assert (e.status, e.code, e.field) == (413, "payload_too_large", "file"), e.body()
    assert service.sniff_format("a.docx", _zip({})) == "docx"


def test_kit_files_pass_guard():
    """Документы комплекта (если есть) проходят новые пределы: самая большая часть ~23 МБ, каталог ЛПУ ~54 МБ."""
    from helpers import kit_or_skip

    kit = kit_or_skip()
    files = [*kit.rglob("*.docx"), *kit.rglob("*.xlsx")]
    assert files
    for f in files:
        with zipfile.ZipFile(f) as z:
            zipguard.check_zip(z, f.suffix.upper())
