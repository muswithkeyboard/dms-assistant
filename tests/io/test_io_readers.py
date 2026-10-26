"""xlsx_reader, pdf_reader, txt_reader на своих маленьких файлах (без комплекта) и сверка lo_check.compare."""
from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path

from dms_core.io.errors import InputError
from dms_core.io.lo_check import compare
from dms_core.io.pdf_reader import read_pdf
from dms_core.io.txt_reader import read_txt
from dms_core.io.xlsx_reader import read_xlsx


def _tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="dms_io_"))


def _raises(fn, code: str) -> InputError:
    try:
        fn()
    except InputError as e:
        assert e.code == code, e.code
        return e
    raise AssertionError(f"ожидалась InputError {code}")


# ------------------------------------------------------------------ xlsx

def _xlsx(path: Path) -> Path:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Список МО"
    ws.append(["Клиника", "Условия", "Код"])
    ws.append(["Клиника 1", "Помощь на дому:\nв пределах МКАД\nдо 12:00", 101])
    ws.append([None, None, None])
    ws.append(["Клиника 2", None, 2.5])
    wb.create_sheet("Пустой")
    wb.save(path)
    return path


def test_xlsx_multiline_cells_kept_as_is():
    data = read_xlsx(_xlsx(_tmp() / "a.xlsx"))
    assert list(data) == ["Список МО", "Пустой"]
    rows = data["Список МО"]
    assert rows[1] == ["Клиника 1", "Помощь на дому:\nв пределах МКАД\nдо 12:00", "101"]
    assert rows[2] == []                         # пустая строка внутри листа сохраняется
    assert rows[3] == ["Клиника 2", None, "2.5"]
    assert data["Пустой"] == []


def test_xlsx_zip_bomb_and_bad_file():
    path = _xlsx(_tmp() / "a.xlsx")
    e = _raises(lambda: read_xlsx(path, max_unpacked=1000), "too_large")
    assert "МБ" in e.message
    bad = _tmp() / "b.xlsx"
    bad.write_bytes(b"not a zip")
    _raises(lambda: read_xlsx(bad), "bad_file")
    many = _tmp() / "c.xlsx"
    with zipfile.ZipFile(many, "w") as z:
        for i in range(10_001):
            z.writestr(f"x{i}", "")
    _raises(lambda: read_xlsx(many), "too_large")


# ------------------------------------------------------------------ pdf

def _pdf(path: Path, text: str | None) -> Path:
    """Минимальный PDF с одной страницей (текст Helvetica или пустая страница) и корректной таблицей xref."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>"),
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(bytes(out))
    return path


def test_pdf_text_by_pages():
    pages = read_pdf(_pdf(_tmp() / "a.pdf", "Rules of insurance"))
    assert len(pages) == 1 and "Rules of insurance" in pages[0]


def test_pdf_without_text_layer_and_not_pdf():
    e = _raises(lambda: read_pdf(_pdf(_tmp() / "s.pdf", None)), "no_text_layer")
    assert "текстового слоя" in e.message
    bad = _tmp() / "x.pdf"
    bad.write_bytes(b"hello")
    _raises(lambda: read_pdf(bad), "bad_pdf")
    broken = _tmp() / "y.pdf"
    broken.write_bytes(b"%PDF-1.4\n garbage")
    _raises(lambda: read_pdf(broken), "bad_pdf")


# ------------------------------------------------------------------ txt

def test_txt_utf8_bom_and_cp1251():
    d = _tmp()
    (d / "u.txt").write_bytes("﻿Письмо\r\nстрока".encode())
    (d / "w.txt").write_bytes("Письмо\rв cp1251".encode("cp1251"))
    assert read_txt(d / "u.txt") == "Письмо\nстрока"
    assert read_txt(d / "w.txt") == "Письмо\nв cp1251"
    _raises(lambda: read_txt(d / "u.txt", max_bytes=3), "too_large")
    _raises(lambda: read_txt(d / "нет.txt"), "bad_file")


# ------------------------------------------------------------------ сверка с LibreOffice (логика, без soffice)

LO = """﻿ДОГОВОР
    1. ПРЕДМЕТ ДОГОВОРА
        1.1. Страховщик обязуется оплатить16 помощь
    I. ОБЩИЕ ПОЛОЖЕНИЯ
№	Клиника	Адрес
1	Клиника один	Москва
        2.1.
        2.2. Пункт в ячейке	соседняя ячейка
"""


def test_compare_counts_equal_and_mismatched_labels():
    ours = [("", "ДОГОВОР"), ("1.", "ПРЕДМЕТ ДОГОВОРА"), ("1.1.", "Страховщик обязуется оплатить[^16] помощь"),
            ("I.", "ОБЩИЕ ПОЛОЖЕНИЯ"), ("", "№"), ("", "Клиника"), ("", "Адрес"), ("", "1"),
            ("", "Клиника один"), ("", "Москва"), ("2.2.", "Пункт в ячейке"), ("", "соседняя ячейка")]
    # «2.1.» у LO без пары — скрытый пустой абзац; чтение сообщает о нём (позиция 10 — перед «2.2.»)
    res = compare(ours, LO.splitlines(), hidden=[(10, "2.1.")])
    assert (res.equal, res.numbered, res.rate) == (4, 4, 1.0)
    assert (res.hidden_skipped, res.lo_extra, res.status) == (1, 0, "checked")
    # без сообщения о скрытом абзаце лишняя метка LO — расхождение, а не исключение
    res = compare(ours, LO.splitlines())
    assert (res.equal, res.numbered, res.lo_extra, res.hidden_skipped) == (4, 5, 1, 0)
    ours_h = {"hidden": [(10, "2.1.")]}
    wrong = [(("1.2." if lbl == "1.1." else lbl), t) for lbl, t in ours]
    res = compare(wrong, LO.splitlines(), **ours_h)
    assert res.equal == 3 and res.numbered == 4
    assert res.mismatches[0]["ours"] == "1.2." and res.mismatches[0]["lo"] == "1.1."
    # у нас метки нет, у LO есть — тоже расхождение
    res = compare([(("" if lbl == "I." else lbl), t) for lbl, t in ours], LO.splitlines(), **ours_h)
    assert res.equal == 3 and res.numbered == 4


def test_compare_mutation_lost_numbered_paragraph_lowers_rate():
    """Мутация: убрать из нашего разбора нумерованный абзац — доля обязана упасть (метка LO без пары не
    исключается, если чтение не сообщило о скрытом абзаце на этом месте)."""
    ours = [("", "ДОГОВОР"), ("1.", "ПРЕДМЕТ ДОГОВОРА"), ("1.1.", "Страховщик обязуется оплатить[^16] помощь"),
            ("I.", "ОБЩИЕ ПОЛОЖЕНИЯ"), ("", "№"), ("", "Клиника"), ("", "Адрес"), ("", "1"),
            ("", "Клиника один"), ("", "Москва"), ("2.2.", "Пункт в ячейке"), ("", "соседняя ячейка")]
    base = compare(ours, LO.splitlines(), hidden=[(10, "2.1.")])
    assert base.rate == 1.0
    for drop in (1, 2, 10):                       # «1.», «1.1.», «2.2.» (в ячейке таблицы)
        mutated = ours[:drop] + ours[drop + 1:]
        pos = 10 if drop > 10 else 9              # позиция скрытого абзаца сдвигается вместе с абзацами
        res = compare(mutated, LO.splitlines(), hidden=[(pos, "2.1.")])
        assert res.rate is not None and res.rate < 1.0, (drop, res)
        assert res.lo_extra == 1 and res.hidden_skipped == 1
    # скрытый абзац, заявленный не той меткой, не «прикрывает» лишнюю метку LO
    res = compare(ours, LO.splitlines(), hidden=[(10, "7.7.")])
    assert res.rate is not None and res.rate < 1.0


def test_compare_no_numbering_is_not_counted():
    res = compare([("", "Договор"), ("", "Текст без номеров")], ["Договор", "Текст без номеров"])
    assert res.status == "no_numbering" and res.rate is None and res.numbered == 0
