"""Запасной адаптер (план §6.2): документ неизвестной СК → пункты с номерами и таблицы как текст, без клиник.

Ничего не угадывает: карточка договора содержит только юрлицо и группу, перечня клиник и матрицы нет, в warnings —
«формат СК не распознан». Так «нет в документах» по клиникам не выводится (absent требует разобранного перечня,
`docs/rules.md` п. 3а), а ответ уходит в «данных недостаточно».

Форматы (этап w8-fix): docx — абзацы с автонумерацией Word (io/docx_reader); txt и pdf — абзацы текста, номер пункта
берётся из начала строки («2.6.1. …»); xlsx — каждая непустая строка листа — один «пункт» (ячейки через « | »).

Таблицы (этап w10-fix, дополнение CONTRACTS §11): docx-таблицы читаются в порядке тела документа, чтобы проверка
покрытия видела условия, исключения и услуги, набранные в таблицах (таблицы услуг «специалист | да», текст
программы в ячейке). Строка таблицы — пункт `<doc>#t<N>r<R>` (id = ссылка строки table_mapping.row_ref) с путём
«<документ> › табл. N, стр. M» (guided._is_table_row: раздел — по ячейкам «да / нет»), текст — непустые ячейки
через « | » без повторов (объединённые ячейки), номер — если первая ячейка — номер пункта. Строка из одной
непустой ячейки с несколькими абзацами — «текстовая рамка» (текст программы, набранный в таблице): каждый абзац —
пункт `<doc>#t<N>r<R>p<K>` как абзац тела, с путём «<документ> › таблица N › <заголовок раздела> › <номер>»
(без «табл.», чтобы разделы «входит / исключения» определялись по заголовкам, как у абзацев; нумерация,
начавшаяся заново, — новый раздел под последним заголовком). xlsx — строка листа `<doc>#<лист>!<строка>` с путём
«<документ> › табл. <номер листа>, стр. M». Программа у этих пунктов — None (общий пункт договора); строки
таблиц, размеченных оператором, получают программу разметки или убираются, если стали строками перечня или
матрицы (onboarding.apply_mappings).
Файл, который не читается, получает статус error и предупреждение — остальные файлы договора разбираются
(ошибка одного файла не роняет договор, ревизия w8 п. 5).
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from pathlib import Path

from ..constants import DocStatus
from ..io.errors import InputError
from ..kit import KitContract
from ..schemas import Clause, Contract, Document
from .base import ParsedContract, abs_path, make_document, text_hash

# Номер пункта в начале строки: «1.», «2.6.1.», «3.1» — части до трёх цифр (дата «01.01.2026» — не номер:
# после «01.01» нет пробела), за номером — пробел и текст.
_NUM = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3}){0,5})\.?\s+(?=\S)")
# Конец предложения: следующая строка — новый абзац (иначе — перенос строки внутри абзаца, как в PDF).
_END = re.compile(r"[.:;!?»)]\s*$")
ROW_SEP = " | "

UNREADABLE = "Файл не читается: {msg}"
PDF_NO_TEXT = "PDF не разбирается: загрузите DOCX."


def matches(kc: KitContract, root: Path) -> bool:
    return True


def split_number(line: str) -> tuple[str | None, str]:
    """(номер пункта из начала строки | None, текст без номера)."""
    m = _NUM.match(line)
    if not m:
        return None, line.strip()
    return m.group(1), line[m.end():].strip()


def paragraphs(lines: Iterable[str]) -> Iterator[tuple[str | None, str]]:
    """Строки текста → абзацы (номер, текст). Новый абзац — после пустой строки, со строки с номером пункта или
    после строки, закончившей предложение; иначе строка — продолжение абзаца (перенос в PDF и txt)."""
    num: str | None = None
    buf: list[str] = []
    for raw in lines:
        line = raw.strip()
        n, rest = split_number(line) if line else (None, "")
        if buf and (not line or n is not None or _END.search(buf[-1])):
            yield num, " ".join(buf)
            num, buf = None, []
        if not line:
            continue
        if not buf:
            num = n
            buf.append(rest if n is not None else line)
        else:
            buf.append(line)
    if buf:
        yield num, " ".join(buf)


# Пункт запасного адаптера: (номер, текст, сноски, страница PDF, суффикс id | None, путь | None). Суффикс и путь —
# у пунктов из таблиц (id «<doc>#t<N>r<R>…», свой путь), у абзацев — None (id «<doc>#p<i>», путь по номеру).
Item = tuple[str | None, str, list[str], int | None, str | None, str | None]

# Строка-номер в первой ячейке таблицы: «1», «2.6.1.» (без текста)
_NUM_CELL = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3}){0,5})\.?\s*$")
# Заголовок раздела текстовой рамки: без номера, кончается «:» (≤ 150 знаков) или короткая строка без точки в конце
_HEAD_MAX, _HEAD_SHORT = 150, 90


def row_cells(cells: Iterable[str]) -> list[str]:
    """Непустые ячейки строки без повторов подряд: объединённая по горизонтали или вертикали ячейка у некоторых
    редакторов повторяет текст в каждой клетке — в пункте он один раз."""
    out: list[str] = []
    for c in cells:
        c = re.sub(r"\s+", " ", c or "").strip()
        if c and (not out or out[-1] != c):
            out.append(c)
    return out


def row_number(first: str) -> str | None:
    """Номер пункта строки таблицы: первая ячейка — сам номер («2.1», «3.») или начинается с номера («1.1. …»)."""
    m = _NUM_CELL.match(first)
    if m:
        return m.group(1)
    return split_number(first)[0]


def _short(s: str, n: int = 80) -> str:
    s = re.sub(r"\s+", " ", s).strip().rstrip(":")
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _is_heading(line: str) -> bool:
    """Заголовок раздела в тексте рамки: без номера, не пункт списка; «…:» до 150 знаков или короткая строка без
    запятых и точки в конце («Исключения из программы»)."""
    s = line.strip()
    if not s or s[0] in "-–—•*" or s[0].isdigit():
        return False
    if s.endswith(":"):
        return len(s) <= _HEAD_MAX
    return len(s) <= _HEAD_SHORT and "," not in s and not s.endswith((".", ";"))


def text_frame(lines: list[tuple[str | None, str]], base: str, suffix: str) -> Iterator[Item]:
    """Абзацы текстовой рамки (одна ячейка — много абзацев) → пункты как абзацы тела. Путь «<base> › <раздел> ›
    <номер>»: раздел — последний заголовок перед тем, как нумерация началась заново («1.» / «1.1.» после больших
    номеров), чтобы «1.2» исключений не получил родителем «1.» раздела «входит»; абзац без номера — под последним
    заголовком."""
    heads: list[str] = []
    ctx: list[str] = []
    prev_top: int | None = None
    prev_num: str | None = None
    for k, (number, text) in enumerate(lines):
        if number is None:
            number, text = split_number(text)
        if number:
            top = int(number.split(".")[0])
            child = bool(prev_num) and number.startswith(f"{prev_num}.")
            if prev_top is None or (number in ("1", "1.1") and not child and top <= prev_top):
                ctx = [_short(heads[-1])] if heads else []
            prev_top = top
            path = " › ".join([base, *ctx, number])
        else:
            if _is_heading(text):
                heads.append(text)
            path = " › ".join([base, _short(heads[-1])] if heads else [base])
        prev_num = number
        yield number, text, [], None, f"{suffix}p{k}", path


def docx_items(path: Path, title: str) -> Iterator[Item]:
    """Абзацы и строки таблиц docx в порядке тела документа (правила — в описании модуля)."""
    from ..io.docx_reader import read_docx

    doc = read_docx(path)
    for kind, i in doc.blocks:
        if kind == "p":
            p = doc.paragraphs[i]
            # номер пункта — автонумерация Word (numbering.xml), иначе — набранный текстом в начале абзаца
            number, text = (p.number, p.text) if p.number else split_number(p.text)
            yield number, text, list(p.footnotes), None, f"#p{i}", None
            continue
        for ri, row in enumerate(doc.tables[i].rows):
            filled = [c for c in row if c.text.strip()]
            cells = row_cells(c.text for c in filled)
            if not cells:
                continue                                  # пустая строка таблицы — не пункт
            suffix = f"#t{i}r{ri}"
            if len(cells) == 1:
                # одна ячейка (или одна объединённая): много абзацев — текстовая рамка
                lines = [(p.number, p.text) for p in filled[0].paras if p.text.strip()]
                if len(lines) <= 1:
                    lines = [(None, x) for x in filled[0].text.split("\n") if x.strip()]
                if len(lines) > 1:
                    yield from text_frame(lines, f"{title} › таблица {i + 1}", suffix)
                    continue
            notes = [n for c in filled for n in c.footnotes]
            yield (row_number(cells[0]), ROW_SEP.join(cells), notes, None, suffix,
                   f"{title} › табл. {i + 1}, стр. {ri + 1}")


def _txt(path: Path, title: str) -> Iterator[Item]:
    from ..io.txt_reader import read_txt

    for n, t in paragraphs(read_txt(path).split("\n")):
        yield n, t, [], None, None, None


def _pdf(path: Path, title: str) -> Iterator[Item]:
    from ..io.pdf_reader import read_pdf

    for page_no, page in enumerate(read_pdf(path), start=1):
        for n, t in paragraphs(page.split("\n")):
            yield n, t, [], page_no, None, None


def _xlsx(path: Path, title: str) -> Iterator[Item]:
    """Лист как таблица: строка листа → пункт «<документ> › табл. <номер листа>, стр. M», ячейки через « | »
    (пустые пропускаются); id — как ссылка строки листа (table_mapping.row_ref: «<doc>#<лист>!<строка Excel>»)."""
    from ..io.xlsx_reader import read_xlsx

    for si, (sheet, rows) in enumerate(read_xlsx(path).items()):
        for ri, row in enumerate(rows):
            cells = row_cells(row)
            if cells:
                yield (row_number(cells[0]), ROW_SEP.join(cells), [], None, f"#{sheet}!{ri + 1}",
                       f"{title} › табл. {si + 1}, стр. {ri + 1}")


READERS = {"docx": docx_items, "txt": _txt, "pdf": _pdf, "xlsx": _xlsx}


def _fail(doc: Document, msg: str) -> None:
    doc.status = DocStatus.error
    doc.warnings.append(msg)


def parse(kc: KitContract, root: Path) -> ParsedContract:
    out = ParsedContract(contract=Contract(id=kc.id, group_id=kc.group_id, legal_entity=kc.legal_entity),
                         warnings=["формат СК не распознан: разобраны только пункты, перечня клиник нет"])
    seen: dict[str, int] = {}
    for kf in kc.files:
        n = seen.get(kf.doc_type, 0)
        seen[kf.doc_type] = n + 1
        doc = make_document(kf, kc.id, contract_id=kc.id, group_id=kc.group_id, n=n)
        out.documents.append(doc)
        reader = READERS.get(kf.fmt)
        if reader is None:
            _fail(doc, f"формат {kf.fmt} не разобран запасным адаптером")
            continue
        clauses: list[Clause] = []
        try:
            for i, (number, text, footnotes, page, suffix, path) in enumerate(reader(abs_path(root, kf), doc.title)):
                if not text.strip():
                    continue
                clauses.append(Clause(
                    id=f"{doc.id}{suffix}" if suffix else f"{doc.id}#p{i}", doc_id=doc.id, contract_id=kc.id,
                    number=number, path=path or (f"{doc.title} › {number}" if number else doc.title), text=text,
                    footnotes=footnotes, page=page, text_hash=text_hash(text)))
        except InputError as e:
            # PDF без текстового слоя или нечитаемый PDF — ясная подсказка «загрузите DOCX»
            _fail(doc, PDF_NO_TEXT if kf.fmt == "pdf" else UNREADABLE.format(msg=e.message))
            continue
        except Exception:  # noqa: BLE001 — битый файл договора виден у этого файла, остальные разбираются
            _fail(doc, UNREADABLE.format(msg="файл повреждён или это не " + kf.fmt.upper()))
            continue
        out.clauses += clauses
    return out
