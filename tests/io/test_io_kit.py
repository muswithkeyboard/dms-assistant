"""Чтение файлов комплекта: иконки Ренессанса, сноски СОГАЗ, скрытый текст Зетты, xlsx и PDF (иначе SKIP)."""
from __future__ import annotations

import collections
import zipfile

from dms_core.io.docx_reader import iter_all_paras, read_docx
from dms_core.io.pdf_reader import read_pdf
from dms_core.io.xlsx_reader import read_xlsx
from helpers import kit_or_skip


def test_renins_programs_have_access_icons():
    # ГК 2: VML-иконка «по записи через центр» во 2-й ячейке строк клиник (docs/facts/kit/gk2.md)
    for f in sorted(kit_or_skip().glob("ГК 2/*/Приложение 1*.docx")):
        doc = read_docx(f)
        c = collections.Counter(i for t in doc.tables for row in t.rows for cell in row for i in cell.icons)
        top, n = c.most_common(1)[0]
        assert n > 1000, (f.parent.name, n)
        # самая частая иконка стоит во второй ячейке строки из 4 ячеек
        second = sum(1 for t in doc.tables for row in t.rows if len(row) == 4 and top in row[1].icons)
        assert second > 0.9 * n


def test_sogaz_footnotes_glued_to_their_paragraphs():
    for f in sorted(kit_or_skip().glob("ГК 3/*/Приложение 2*.docx")):
        doc = read_docx(f)
        refs = zipfile.ZipFile(f).read("word/document.xml").decode().count("w:footnoteReference")
        glued = sum(len(p.footnotes) for p in iter_all_paras(doc))
        assert glued == refs, (f.parent.name, glued, refs)        # и в теле, и в ячейках
        cells = sum(len(c.footnotes) for t in doc.tables for row in t.rows for c in row)
        assert cells > 0
        p32 = [p for p in doc.paragraphs if p.number == "3.2." and "30 км от МКАД" in p.text]
        assert p32 and any("возможности оказания" in n for n in p32[0].footnotes)        # сноска 17
        home = [p for p in doc.paragraphs if any("до 12:00" in n for n in p.footnotes)]  # сноска 38
        assert home and "Помощь на дому оказывается" in home[0].text
        i = home[0].index
        assert any(p.number == "6.1.3." for p in doc.paragraphs[max(0, i - 5):i])      # под п. 6.1.3


def test_zetta_hidden_paragraphs_dropped():
    f = next(kit_or_skip().glob("ГК 1/Компания 1/*.docx"))
    doc = read_docx(f)
    assert all(p.text.strip() or p.number for p in doc.paragraphs)
    numbers = [p.number for p in doc.paragraphs]
    assert "4.1.1." in numbers and "4.1.2." in numbers and "1.1.2." in numbers


def test_kit_xlsx_and_pdf():
    kit = kit_or_skip()
    data = read_xlsx(next(kit.glob("ГК 3/Компания 9/Приложение 4*.xlsx")))
    assert any(c and "\n" in c for rows in data.values() for row in rows for c in row)
    pages = read_pdf(next(kit.glob("ГК 1/Компания 2/*.pdf")))
    assert len(pages) > 10 and sum(len(p) for p in pages) > 100_000
