"""read_docx на своих маленьких docx (без комплекта): нумерация Word, скрытый текст, сноски, иконки в ячейках."""
from __future__ import annotations

import tempfile
from pathlib import Path

from docx_build import PNG_A, PNG_B, abstract, blip, build, fnref, lvl, num, p, r, tbl, vml

from dms_core.io.docx_reader import DocxError, iter_all_paras, read_docx, to_text

# многоуровневый список «1. / 1.1. / 1.1.1.» (abstractNum 1), римский заголовок с isLgl (abstractNum 2),
# буквенный русский список «а)» (abstractNum 3), маркеры (abstractNum 4)
NUMBERING = (
    abstract(1, lvl(0, "%1."), lvl(1, "%1.%2."), lvl(2, "%1.%2.%3."))
    + abstract(2, lvl(0, "%1.", fmt="upperRoman"), lvl(1, "%1.%2.", is_lgl=True))
    + abstract(3, lvl(0, "%1)", fmt="russianLower"))
    + abstract(4, lvl(0, "", fmt="bullet"))
    + abstract(5, lvl(0, "%1."), lvl(1, "%1.%2."), lvl(2, "%1.%2.%3."))
    + num(1, 1) + num(2, 1)                                     # два экземпляра одного списка — общий счётчик
    + num(3, 1, '<w:lvlOverride w:ilvl="0"><w:startOverride w:val="7"/></w:lvlOverride>')
    + num(4, 2) + num(5, 3) + num(6, 4) + num(7, 5)
)


def _labels(path: Path) -> list[tuple[str | None, str]]:
    return [(x.number, x.text) for x in iter_all_paras(read_docx(path))]


def test_multilevel_list_and_continuation_between_num_instances():
    body = (p("Предмет", (1, 0)) + p("Пункт", (1, 1)) + p("Подпункт", (1, 2)) + p("Подпункт два", (1, 2))
            + p("Пункт два", (1, 1)) + p("Раздел два", (2, 0)) + p("Пункт раздела два", (2, 1)))
    got = _labels(build(body, NUMBERING))
    assert [n for n, _ in got] == ["1.", "1.1.", "1.1.1.", "1.1.2.", "1.2.", "2.", "2.1."]


def test_start_override_is_separate_list_instance():
    body = p("А", (1, 0)) + p("Б", (3, 0)) + p("В", (3, 0)) + p("Г", (1, 0))
    assert [n for n, _ in _labels(build(body, NUMBERING))] == ["1.", "7.", "8.", "2."]


def test_is_lgl_roman_and_letter_formats_and_bullets():
    body = (p("Общие", (4, 0)) + p("Пункт", (4, 1)) + p("Второй", (4, 0)) + p("Пункт", (4, 1))
            + p("буква", (5, 0)) + p("буква", (5, 0)) + p("маркер", (6, 0)))
    got = [n for n, _ in _labels(build(body, NUMBERING))]
    assert got == ["I.", "1.1.", "II.", "2.1.", "а)", "б)", "•"]


def test_missing_upper_level_counts_as_passed():
    # «1.1.1» без предшествующего «1.1»: следующий абзац уровня 1 — «1.2», уровня 0 — «2.» (как Word и LO, ГК 1)
    body = p("Глава", (7, 0)) + p("глубже", (7, 2)) + p("пункт", (7, 1)) + p("глава", (7, 0))
    assert [n for n, _ in _labels(build(body, NUMBERING))] == ["1.", "1.1.1.", "1.2.", "2."]


def test_hidden_text_and_hidden_paragraph():
    # скрытый прогон выпадает из текста; скрытый пустой абзац не выводится, но двигает счётчик (Зетта, ГК 1)
    body = (p(runs=r("Видимо ") + r("СКРЫТО", hidden=True) + r("дальше"), num=(1, 0))
            + p("", (1, 0), hidden_mark=True) + p(runs=r("скрыт весь", hidden=True), num=(1, 0), hidden_mark=True)
            + p("Следующий", (1, 0)))
    doc = read_docx(build(body, NUMBERING))
    assert [(x.number, x.text) for x in doc.paragraphs] == [("1.", "Видимо дальше"), ("4.", "Следующий")]
    assert "СКРЫТО" not in to_text(doc)
    # чтение явно сообщает о пропусках: (позиция в iter_all_paras, перед которой стоял абзац, метка)
    assert doc.skipped_hidden == [(1, "2."), (1, "3.")]


def test_skipped_hidden_positions_count_table_cells():
    body = (p("До", (1, 0)) + tbl([p("в ячейке", (1, 0)) + p("", (1, 0), hidden_mark=True), p("соседняя")])
            + p("", (1, 0), hidden_mark=True) + p("После", (1, 0)))
    doc = read_docx(build(body, NUMBERING))
    labels = [x.number for x in iter_all_paras(doc)]
    assert labels == ["1.", "2.", None, "5."]
    assert doc.skipped_hidden == [(2, "3."), (3, "4.")]


def test_numbering_continues_through_table_and_cells_keep_labels():
    body = (p("До таблицы", (1, 0))
            + tbl([p("в ячейке", (1, 0)) + p("ещё абзац"), p("соседняя")])
            + p("После таблицы", (1, 0)))
    doc = read_docx(build(body, NUMBERING))
    assert [x.number for x in doc.paragraphs] == ["1.", "3."]
    cell = doc.tables[0].rows[0][0]
    assert cell.text == "2. в ячейке\nещё абзац"
    assert [x.in_table for x in cell.paras] == [True, True]
    assert doc.blocks == [("p", 0), ("t", 0), ("p", 1)]
    assert doc.tables[0].para_before == 0


def test_footnotes_in_body_and_in_cells():
    body = (p(runs=r("Помощь на дому") + fnref(1) + r(" в пределах 30 км") + fnref(2), num=(1, 0))
            + tbl([p(runs=r("Ячейка") + fnref(3)), p("без сноски")]))
    notes = {1: "Оказывается при возможности", 2: "Вызов до 12:00", 3: "Сноска в таблице"}
    doc = read_docx(build(body, NUMBERING, footnotes=notes))
    para = doc.paragraphs[0]
    assert para.text == "Помощь на дому[^1] в пределах 30 км[^2]"
    assert para.footnotes == ["Оказывается при возможности", "Вызов до 12:00"]
    cell = doc.tables[0].rows[0][0]
    assert cell.text == "Ячейка[^3]" and cell.footnotes == ["Сноска в таблице"]
    assert doc.tables[0].rows[0][1].footnotes == []


def test_icons_in_cells_vml_and_drawingml_without_fallback_duplicates():
    body = tbl([p("Клиника"), p(runs=vml("rIdA")), p("адрес")],
               [p("Другая"), p(runs=blip("rIdB")), p("адрес")],
               [p("Без иконки"), p(""), p("адрес")])
    doc = read_docx(build(body, images={"rIdA": PNG_A, "rIdB": PNG_B}))
    rows = doc.tables[0].rows
    a, b = rows[0][1].icons, rows[1][1].icons
    assert len(a) == 1 and len(b) == 1 and a != b                      # Fallback-дубль не считается
    assert a[0].startswith("[icon:") and len(a[0]) == len("[icon:12345678]")
    assert rows[2][1].icons == []


def test_numbering_via_paragraph_style():
    styles = ('<w:style w:type="paragraph" w:styleId="H1"><w:pPr><w:numPr><w:numId w:val="7"/></w:numPr></w:pPr>'
              '</w:style><w:style w:type="paragraph" w:styleId="H1b"><w:basedOn w:val="H1"/></w:style>')
    body = p("Заголовок", style="H1") + p("Ещё", style="H1b")
    doc = read_docx(build(body, NUMBERING, styles=styles))
    assert [x.number for x in doc.paragraphs] == ["1.", "2."]


def test_nested_table_paragraphs_counted_once():
    inner = tbl([p("внутри", (1, 0))])
    body = tbl([p("снаружи", (1, 0)) + inner]) + p("после", (1, 0))
    doc = read_docx(build(body, NUMBERING))
    assert doc.tables[0].rows[0][0].text == "1. снаружи\n2. внутри"
    assert len(doc.tables) == 1 and len(doc.tables[0].rows) == 1
    assert doc.paragraphs[0].number == "3."


def test_bad_files_raise_russian_error():
    d = Path(tempfile.mkdtemp())
    (d / "x.docx").write_bytes(b"not a zip")
    for f in (d / "x.docx",):
        try:
            read_docx(f)
        except DocxError as e:
            assert e.code == "bad_docx" and "docx" in e.message
        else:
            raise AssertionError("ожидалась DocxError")
