"""Адаптер «Ренессанс» (ГК 2, договоры c05–c08): договор + «Приложение 1. Программа ДМС» (план §6.2).

Устройство документов — docs/facts/kit/gk2.md; прототипы — reference/kit_analysis/gk2/ (clinics.py, progmap.py,
iconmap.py, blocks.py). Коротко:
  * договор: номера пунктов набраны текстом («2.4.\\t…»), подпункты «1)…9)» относятся к последнему номерному
    пункту; таблица п. 2.3 — программы, премии, страховые суммы, численность; п. 2.7 — график взносов;
  * программа: почти всё — таблицы. Таблица клиник: строка с названием программы → строки-заголовки вида помощи
    («Поликлиника | Врач на дом») → строки «юрлицо | иконка | адрес | код»; юрлицо пишется один раз и протягивается
    вниз; «^^^^^ Внимание предыдущая клиника: …» — примечание к строке выше; в конце — легенда иконок
    («Обслуживание в этом лечебном учреждении осуществляется:»). Следом — однострочная таблица с текстом условий
    программы (~60 тыс. знаков, 15 вариантов на ГК);
  * таблица ≠ программа: «Калининград» (дог. 5) и «Пермь» (дог. 6, 8) начинаются внутри таблицы с текстом
    предыдущей программы. Поэтому строки обходятся сквозным потоком, а программа узнаётся по совпадению строки
    с названием из таблицы п. 2.3 договора (прототип progmap.py: 34/34, 27/27, 25/25, 37/37);
  * «Расширения к холдингу» — абзацы тела после последней программы: отдельный блок, не программа.
Автонумерации Word в документах ГК 2 нет (gk2.md §4), поэтому номер берётся из текста абзаца.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import zipfile
from pathlib import Path

from lxml import etree

from ..constants import Insurer
from ..io.docx_reader import Cell, DocxDoc, read_docx
from ..kit import KitContract
from ..schemas import CareMatrix, Clause, ClinicRow, Contract, Franchise, Payment, Program
from .base import ParsedContract, abs_path, files_of, make_document, text_hash

# Признак СК в преамбуле договора (выбор адаптера — по содержимому, не по папке)
_INSURER_RE = re.compile(r"Ренессанс\s+Страхование", re.IGNORECASE)

# Номер пункта, набранный текстом: «2.4.\t», «3.4.1.1. », «1. » (но не «8 (800) …» — нужна точка или подуровень)
_NUM_RE = re.compile(r"^\s*(\d+(?:\.\d+)+)\.?(?=\s)|^\s*(\d+)\.(?=\s)")
# Подпункт «1)» — к последнему номерному пункту договора
_SUB_RE = re.compile(r"^\s*(\d+)\)")

_PROGRAM_TABLE_HEAD = "название программы"
_LEGEND_HEAD = "обслуживание в этом лечебном учреждении"
_NOTE_PREFIX = "^^^^^"
_TEXT_BLOCK_MIN = 300          # одноячеечная строка длиннее — текст условий программы (прототип clinics.py)
_SECTION_MAX = 60              # одноячеечная строка короче — заголовок вида помощи
_EXTENSIONS_HEAD = "расширения к холдингу"
# Заголовок подпрограммы в тексте условий: «Программа … «Антиклещ»», «Программа стоматологического обслуживания
# «Бизнес»», «Программа ментальной поддержки Просебя» (но не «Программа включает:» / «Программа не предусматривает…»)
_SUBPROGRAM_RE = re.compile(r"^Программа (?!не |включает|осуществляется|предусматривает)")

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября",
     "декабря"])}

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
V = "{urn:schemas-microsoft-com:vml}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


# ------------------------------------------------------------------ выбор адаптера

def matches(kc: KitContract, root: Path) -> bool:
    """Договор Ренессанса: в преамбуле (первые абзацы) страховщик — «Группа Ренессанс Страхование»."""
    for kf in files_of(kc, "contract"):
        if kf.fmt != "docx":
            continue
        try:
            doc = read_docx(abs_path(root, kf))
        except Exception:      # noqa: BLE001, S112 — битый файл не наш признак, решит generic
            continue
        head = " ".join(p.text for p in doc.paragraphs[:12])
        if _INSURER_RE.search(head):
            return True
    return False


# ------------------------------------------------------------------ мелкие разборщики

def _num(s: str | None) -> float | None:
    """«2 369 265,00» → 2369265.0; пусто → None."""
    if not s:
        return None
    t = re.sub(r"[\s\xa0]", "", s).replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return None


def _date_ru(s: str) -> dt.date | None:
    """«25 апреля 2026» или «25.04.2026» → date."""
    m = re.search(r"(\d{1,2})\s+([а-я]+)\s+(\d{4})", s)
    if m and m.group(2) in _MONTHS:
        return dt.date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return None


def text_number(text: str) -> str | None:
    """Номер пункта, набранный текстом в начале абзаца: «2.13.1. …» → «2.13.1», «1. …» → «1»."""
    m = _NUM_RE.match(text)
    if not m:
        return None
    return m.group(1) or m.group(2)


def _city(address: str | None) -> str | None:
    """Город из адреса «Регион, Город, улица…» или «Москва, улица…»."""
    if not address:
        return None
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if not parts or parts[0].startswith("Все адреса"):
        return None
    region = re.search(r"(област|край|республик|автономн|округ)", parts[0], re.IGNORECASE)
    if region and len(parts) > 1:
        return parts[1]
    return parts[0]


_LEGAL_RE = re.compile(r"\b(ООО|АО|ПАО|ЗАО|ОАО|ЧУЗ|ЧУ|НУЗ|ФГБУ|ФГБУЗ|ГБУЗ|ГАУЗ|КГБУЗ|ГУЗ|БУЗ|АНО|ИП|ФГАУ|ФБУН|"
                       r"Общество с ограниченной|Акционерное общество)\b")


def _legal_name(org: str) -> str | None:
    """Юрлицо из «Бренд (ООО "…")» или само название, если оно уже юрлицо."""
    m = re.match(r"^.*?\((.*)\)\s*$", org)
    if m and _LEGAL_RE.search(m.group(1)):
        return m.group(1).strip()
    return org if _LEGAL_RE.search(org) else None


def _franchise(org: str, code: str) -> tuple[float | None, str | None]:
    """Франшиза клиники: процент — только если он написан в документе («(ФРАНШИЗА 10%)» в названии).
    Код с «_фр» без процента — пометка «размер не указан» (знания «как обычно» не подставляем, `docs/rules.md` п. 2)."""
    m = re.search(r"франшиз\w*\s*(\d+(?:[.,]\d+)?)\s*%", org, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(",", ".")), None
    if re.search(r"_фр\b|_фр$", code):
        return None, "клиника с франшизой (код «_фр»), размер в перечне не указан"
    return None, None


# ------------------------------------------------------------------ иконки (VML) — запасной путь

def _vml_icons(path: Path) -> dict[tuple[int, int], list[list[str]]]:
    """(таблица, строка) → маркеры «[icon:<sha1 8>]» по ячейкам.

    Иконки Ренессанса — VML (v:imagedata), а docx_reader волны 0 видит только DrawingML (a:blip). Пока этап w1-io
    не выдаёт маркеры, берём их здесь по логике прототипа reference/kit_analysis/gk2/iconmap.py. Обход строк —
    тот же, что в docx_reader (таблицы тела по порядку, `tbl.iter(tr)`), поэтому индексы совпадают, а маркер —
    тот же sha1 байтов картинки.
    """
    out: dict[tuple[int, int], list[list[str]]] = {}
    with zipfile.ZipFile(path) as z:
        try:
            rels = etree.fromstring(z.read("word/_rels/document.xml.rels"))
        except KeyError:
            return out
        img: dict[str, str] = {}
        for rel in rels:
            target = rel.get("Target") or ""
            if "image" not in (rel.get("Type") or ""):
                continue
            p = target.lstrip("/") if target.startswith(("word/", "/word/")) else "word/" + target
            try:
                img[rel.get("Id")] = f"[icon:{hashlib.sha1(z.read(p)).hexdigest()[:8]}]"
            except KeyError:
                continue
        body = etree.fromstring(z.read("word/document.xml")).find(f"{W}body")
    ti = -1
    for el in body:
        if el.tag != f"{W}tbl":
            continue
        ti += 1
        for ri, tr in enumerate(el.iter(f"{W}tr")):
            cells = []
            for tc in tr.findall(f"{W}tc"):
                ids = [i.get(f"{R}id") for i in tc.iter(f"{V}imagedata")]
                ids += [b.get(f"{R}embed") for b in tc.iter(f"{A}blip")]
                cells.append([img[i] for i in ids if i in img])
            if any(cells):
                out[(ti, ri)] = cells
    return out


# ------------------------------------------------------------------ договор

def _parse_contract_doc(doc: DocxDoc, doc_id: str, contract_id: str, title: str) -> tuple[list[Clause], dict]:
    """Пункты договора и сырые факты карточки (программы п. 2.3, премия п. 2.4, численность п. 1.1, сроки, график)."""
    clauses: list[Clause] = []
    facts: dict = {"programs": [], "payments": [], "franchise_ids": []}
    last_num: str | None = None
    tables_after: dict[int, list[int]] = {}
    for t in doc.tables:
        tables_after.setdefault(t.para_before, []).append(t.index)

    def add(cid: str, number: str | None, path: str, text: str, program: str | None = None,
            footnotes: list[str] | None = None) -> Clause:
        c = Clause(id=cid, doc_id=doc_id, contract_id=contract_id, number=number, path=path, program=program,
                   text=text, footnotes=footnotes or [], text_hash=text_hash(text))
        clauses.append(c)
        return c

    def table_rows(ti: int) -> None:
        """Строки таблиц договора — тоже источники ссылок (таблица п. 2.3 — премии по программам)."""
        t = doc.tables[ti]
        head = [c.text.strip() for c in t.rows[0]] if t.rows else []
        is_prog = bool(head) and head[0].casefold().startswith(_PROGRAM_TABLE_HEAD)
        for ri, row in enumerate(t.rows):
            cells = [c.text.replace("\n", " ").strip() for c in row]
            if not any(cells):
                continue
            prog = cells[0] if is_prog and ri > 0 else None
            base = f"{title} › {last_num}" if last_num else title
            add(f"{doc_id}#t{ti}r{ri}", None, f"{base} › {prog}" if prog else base, " | ".join(cells), prog)
            if prog:
                facts["programs"].append(Program(
                    name=prog, premium_per_person=_num(cells[1]) if len(cells) > 1 else None,
                    persons=int(_num(cells[4]) or 0) if len(cells) > 4 else None,
                    notes="; ".join(x for x in (
                        f"страховая сумма {cells[2]} руб." if len(cells) > 2 and cells[2] else "",
                        f"«Медикаментозное обеспечение» {cells[3]} руб." if len(cells) > 3 and cells[3]
                        else "«Медикаментозное обеспечение»: сумма в п. 2.3 не указана",
                    ) if x) or None))

    for ti in tables_after.get(-1, []):
        table_rows(ti)
    for p in doc.paragraphs:
        text = p.text.strip()
        num = text_number(text)
        sub = _SUB_RE.match(text)
        if num:
            last_num = num
            number, path = num, f"{title} › {num}"
        elif sub and last_num:
            # подпункт «7)» — «п. 3.4.5, подп. 7» (gk2.md §4)
            number, path = f"{last_num}, подп. {sub.group(1)}", f"{title} › {last_num} › {sub.group(1)})"
        else:
            number, path = None, f"{title} › {last_num}" if last_num else title
        c = add(f"{doc_id}#p{p.index}", number, path, text, footnotes=p.footnotes)

        # факты карточки — из текста пунктов
        if p.index <= 1 and (m := re.search(r"№\s*(\S+)", text)) and "number" not in facts:
            facts["number"] = m.group(1)
        if "date" not in facts and re.fullmatch(r"\d{1,2}\s+[а-я]+\s+\d{4}\s*г\.?", text):
            facts["date"] = _date_ru(text)
        if num == "1.1" and (m := re.search(r"численность[^.]*?составляет\s+(\d+)", text)):
            facts["headcount"] = int(m.group(1))
        if num == "2.4" and (m := re.search(r"составляет\s+([\d\s\xa0]+,\d{2})", text)):
            facts["premium_total"] = _num(m.group(1))
        if num == "4.1":
            ds = re.findall(r"с\s+(\d{1,2}\s+[а-я]+\s+\d{4})\s*г\.?\s+по\s+(\d{1,2}\s+[а-я]+\s+\d{4})", text)
            if ds:
                facts["period_from"], facts["period_to"] = _date_ru(ds[0][0]), _date_ru(ds[0][1])
        if last_num == "2.7" and not num and (m := re.search(
                r"с\s+(\d\d\.\d\d\.\d{4})\s*г?\.?\s+по\s+(\d\d\.\d\d\.\d{4})\s*г?\.?\s+в размере\s+([\d\s\xa0]+,\d{2})"
                r".*?до\s+(\d\d\.\d\d\.\d{4})", text)):
            facts["payments"].append(Payment(n=len(facts["payments"]) + 1, due=_date_ru(m.group(4)),
                                             period=f"{m.group(1)}–{m.group(2)}", amount=_num(m.group(3))))
        if number and (number.startswith("2.13") or number == "6.5"):
            facts["franchise_ids"].append(c.id)
        for ti in tables_after.get(p.index, []):
            table_rows(ti)
    return clauses, facts


# ------------------------------------------------------------------ программа

def _section_heading(line: str) -> bool:
    """Заголовок раздела текста программы: без номера, не пункт списка, короткий; кончается «:» или начинается
    с «Программа» (подпрограммы «Антиклещ», «Бизнес», «АПТЕКА»…). Нумерация в каждом разделе начинается заново."""
    s = line.strip()
    if not s or len(s) > 300 or text_number(s) or s[0] in "-–—•*" or s[0].isdigit():
        return False
    return (s.endswith(":") and len(s) <= 150) or s.startswith(("Программа ", "Исключения", "Страховщик не оплачивает"))


def _short(s: str, n: int = 80) -> str:
    s = re.sub(r"\s+", " ", s).strip().rstrip(":")
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _care_types(section: str) -> list[str]:
    """«Поликлиника | Врач на дом» → ["Поликлиника", "Врач на дом"] (составной заголовок — несколько видов)."""
    return [x.strip() for x in section.split("|") if x.strip()]


def _parse_program_doc(doc: DocxDoc, doc_id: str, contract_id: str, title: str, programs: list[str],
                       icons_fallback: dict[tuple[int, int], list[list[str]]] | None,
                       warnings: list[str]) -> tuple[list[Clause], list[ClinicRow], dict[str, dict[str, list[str]]],
                                                     dict[str, str], dict[str, list[tuple[str, str]]]]:
    """Сквозной обход тела программы. Возвращает пункты текста, строки клиник, виды помощи по программам
    (программа → вид помощи → ссылки на строки-заголовки), программы → ссылка на их таблицу и заголовки разделов
    текста условий по программам (программа → [(id пункта, заголовок)])."""
    clauses: list[Clause] = []
    clinics: list[ClinicRow] = []
    care: dict[str, dict[str, list[str]]] = {}
    prog_ref: dict[str, str] = {}
    names = set(programs)
    has_text: set[str] = set()
    text_heads: dict[str, list[tuple[str, str]]] = {}   # программа → (id пункта, заголовок раздела текста условий)

    # поток строк таблиц: программа, вид помощи, юрлицо протягиваются через границы таблиц (Калининград/Пермь)
    prog: str | None = None        # программа текущего перечня клиник
    text_prog: str | None = None   # программа, чей перечень был последним (ей принадлежит следующий текст)
    section: str | None = None
    org: str | None = None
    legend = False
    legend_map: dict[str, str] = {}
    pending_icons: list[tuple[ClinicRow, list[str]]] = []   # иконки строк до легенды своей таблицы

    def icons_of(ti: int, ri: int, cells: list[Cell]) -> list[list[str]]:
        own = [c.icons for c in cells]
        if any(own) or icons_fallback is None:
            return own
        return icons_fallback.get((ti, ri), [[] for _ in cells])

    def close_list() -> None:
        """Конец перечня клиник: смысл иконок — по легенде этого перечня."""
        nonlocal pending_icons
        for row, marks in pending_icons:
            meanings = [legend_map[m] for m in marks if m in legend_map]
            if meanings:
                row.by_referral = True
                row.access_note = "; ".join(dict.fromkeys(meanings + ([row.access_note] if row.access_note else [])))
            elif marks:
                warnings.append(f"{row.source_ref}: иконка {marks} без расшифровки в легенде")
        pending_icons = []

    def add_text_block(ti: int, ri: int, text: str, program: str | None) -> None:
        """Текст условий программы: абзац ячейки = пункт; путь «Программа ДМС › <программа> › <раздел> › 1.4»."""
        heads: list[str] = []             # заголовки разделов по порядку
        ctx: list[str] | None = None      # путь, под которым идёт текущая нумерация
        prev_top: int | None = None
        prev_num: str | None = None       # номер предыдущей строки (None — строка без номера)
        base = [title, program] if program else [title]

        def section_ctx() -> list[str]:
            """Раздел = последний заголовок; подпрограмма («Программа … «Бизнес»») — если она среди трёх последних
            заголовков (её нумерация и «Исключения из программы» идут сразу за ней)."""
            if not heads:
                return []
            out = []
            sub = next((h for h in reversed(heads[-3:]) if _SUBPROGRAM_RE.match(h)), None)
            if sub and sub != heads[-1]:
                out.append(_short(sub.split(" (")[0]))
            out.append(_short(heads[-1].split(" (")[0] if _SUBPROGRAM_RE.match(heads[-1]) else heads[-1]))
            return out

        outer: tuple[list[str], int | None] | None = None   # путь и счётчик снаружи вложенного списка
        prev_line = ""
        for li, line in enumerate(x for x in text.split("\n") if x.strip()):
            num = text_number(line)
            if num:
                top = int(num.split(".")[0])
                if outer is not None and "." in num:
                    # вложенный список («1.», «2.» под «4.1.») кончился — снова «4.2.» внешнего уровня
                    ctx, prev_top = outer
                    outer = None
                child = bool(prev_num) and num.startswith(f"{prev_num}.")   # «1.» → «1.1.» — та же нумерация
                if ctx is None or (num in ("1", "1.1") and not child and (prev_top is None or top <= prev_top)):
                    if prev_num and prev_num != num and prev_line.rstrip().endswith(":") and ctx is not None:
                        # вложенный список под номерным пунктом «4.1. … не оплачивает:» → «… › 4.1 › 1»
                        outer = (ctx, prev_top)
                        ctx = ctx + [prev_num]
                    else:
                        # новый раздел: нумерация в каждом разделе своя → путь через последний заголовок
                        ctx, outer = section_ctx(), None
                prev_top = top
                path = " › ".join(base + ctx + [num])
            else:
                if _section_heading(line):
                    # заголовок посреди списка («Стационарное лечение предусматривает:» между 1.7 и 1.8) нумерацию
                    # не прерывает; новый раздел начинается, только когда номера начинаются заново
                    heads.append(line.strip())
                    if program:
                        text_heads.setdefault(program, []).append((f"{doc_id}#t{ti}r{ri}p{li}", line.strip()))
                path = " › ".join(base + section_ctx())
            prev_num, prev_line = num, line
            clauses.append(Clause(id=f"{doc_id}#t{ti}r{ri}p{li}", doc_id=doc_id, contract_id=contract_id,
                                  number=num, path=path, program=program, text=line.strip(),
                                  text_hash=text_hash(line)))

    tables_after: dict[int, list[int]] = {}
    for t in doc.tables:
        tables_after.setdefault(t.para_before, []).append(t.index)

    def walk_table(ti: int) -> None:
        nonlocal prog, text_prog, section, org, legend
        t = doc.tables[ti]
        for ri, cells in enumerate(t.rows):
            texts = [c.text.strip() for c in cells]
            ref = f"t{ti}r{ri}"
            if len(cells) == 1:
                c = texts[0]
                if len(c) > _TEXT_BLOCK_MIN:
                    # текст условий: принадлежит программе последнего перечня (первый блок после него)
                    close_list()
                    owner = text_prog if text_prog and text_prog not in has_text else None
                    if owner is None:
                        warnings.append(f"{ref}: текст условий без программы (не привязан к перечню)")
                    else:
                        has_text.add(owner)
                    add_text_block(ti, ri, c, owner)
                    prog, section, org, legend = None, None, None, False
                    continue
                if not c:
                    continue
                if c.startswith(_NOTE_PREFIX):
                    # «^^^^^ Внимание предыдущая клиника: …» → примечание к строке выше
                    note = c.lstrip("^ ").strip()
                    if clinics and prog:
                        last = clinics[-1]
                        last.access_note = f"{last.access_note}; {note}" if last.access_note else note
                    continue
                if c.casefold().startswith(_LEGEND_HEAD):
                    legend = True
                    continue
                if c in names:
                    # новая программа — по совпадению с названием из п. 2.3 (а не по границе таблицы)
                    close_list()
                    prog, text_prog, section, org, legend = c, c, None, None, False
                    legend_map.clear()
                    prog_ref.setdefault(c, f"{doc_id}#{ref}")
                    care.setdefault(c, {})
                    continue
                if prog and len(c) < _SECTION_MAX and not legend:
                    section = c
                    org = None
                    for ct in _care_types(c):
                        care[prog].setdefault(ct, []).append(f"{doc_id}#{ref}")
                continue
            if prog is None:
                continue
            marks = icons_of(ti, ri, cells)
            if legend:
                # легенда: иконка в ячейке k → смысл в ячейке k+1
                for k, ms in enumerate(marks):
                    if ms and k + 1 < len(texts) and texts[k + 1]:
                        for m in ms:
                            legend_map.setdefault(m, re.sub(r"\s+", " ", texts[k + 1]))
                continue
            if texts[0]:
                org = texts[0]
            if org is None:
                warnings.append(f"{ref}: строка клиники без названия")
                continue
            address = texts[-2] if len(texts) >= 3 else ""
            code = texts[-1]
            pct, fr_note = _franchise(org, code)
            row = ClinicRow(
                id=f"{doc_id}#{ref}", contract_id=contract_id, doc_id=doc_id, program=prog,
                care_type=section or "", clinic_name=org, legal_name=_legal_name(org), address=address or None,
                city=_city(address), code=code or None, franchise_pct=pct, access_note=fr_note, source_ref=f"{doc_id}#{ref}")
            clinics.append(row)
            row_marks = [m for ms in marks[1:2] for m in ms] if len(marks) > 1 else []
            if row_marks:
                pending_icons.append((row, row_marks))

    # абзацы тела: шапка до таблиц, «Расширения к холдингу» — после
    block = "шапка"
    for ti in tables_after.get(-1, []):
        walk_table(ti)
    for p in doc.paragraphs:
        text = p.text.strip()
        if text.casefold().startswith(_EXTENSIONS_HEAD):
            close_list()
            block = "ext"
        if block == "ext":
            path = f"{title} › Расширения к холдингу"
        else:
            path = title
        clauses.append(Clause(id=f"{doc_id}#p{p.index}", doc_id=doc_id, contract_id=contract_id,
                              number=text_number(text), path=path, program=None, text=text,
                              footnotes=p.footnotes, text_hash=text_hash(text)))
        for ti in tables_after.get(p.index, []):
            walk_table(ti)
    close_list()

    missing = [x for x in programs if x not in prog_ref]
    if missing:
        warnings.append(f"{doc_id}: нет перечня клиник для программ {missing}")
    no_text = [x for x in programs if x not in has_text]
    if no_text:
        warnings.append(f"{doc_id}: нет текста условий для программ {no_text}")
    return clauses, clinics, care, prog_ref, text_heads


# Вид помощи из заголовка перечня клиник → основы слов, по которым он узнаётся в заголовке раздела текста условий
# той же программы. Это разметка документов Ренессанса (gk2.md: заголовки перечней «Поликлиника», «Врач на дом»,
# «Стоматология», «Госпитализация», «Скорая помощь»; разделы текста — «Амбулаторно – поликлиническая помощь:»,
# «Вызов врача на дом…:», «Программа стоматологического обслуживания…», «Экстренная и плановая стационарная
# помощь:», «Круглосуточная скорая медицинская помощь:»), а не вывод: «можно/нельзя» решают инструменты по
# config/care_types.yaml. Неизвестный вид помощи (нет в таблице) — в тексте не ищется, и present=False ему
# не ставится (см. _care_matrix).
_CARE_STEMS: dict[str, re.Pattern[str]] = {
    "поликлиника": re.compile(r"поликлини|амбулаторн", re.IGNORECASE),
    "врач на дом": re.compile(r"на дом|на дому|вызов врача", re.IGNORECASE),
    "стоматология": re.compile(r"стоматолог", re.IGNORECASE),
    "госпитализация": re.compile(r"стационар|госпитализ", re.IGNORECASE),
    "скорая помощь": re.compile(r"скор(ая|ой) (медицинск|помощ)", re.IGNORECASE),
}
# Разделы-исключения («что не входит»): упоминание вида помощи в них не означает, что он есть в программе
_EXCLUSION_HEAD_RE = re.compile(r"^(Исключени|Страховщик не оплачивает|Программа не предусматривает)", re.IGNORECASE)


def _text_refs(care_type: str, heads: list[tuple[str, str]]) -> list[str] | None:
    """Ссылки на заголовки разделов текста условий, где назван вид помощи; [] — не назван; None — вид помощи
    неизвестен (нет основ в _CARE_STEMS): по тексту проверить нельзя, отсутствие не доказано."""
    stem = _CARE_STEMS.get(care_type.casefold())
    if stem is None:
        return None
    return [cid for cid, h in heads if not _EXCLUSION_HEAD_RE.match(h) and stem.search(h)]


def _care_matrix(contract_id: str, programs: list[str], care: dict[str, dict[str, list[str]]],
                 prog_ref: dict[str, str], heads: dict[str, list[tuple[str, str]]],
                 warnings: list[str]) -> list[CareMatrix]:
    """Матрица «программа × вид помощи»; виды помощи — все заголовки перечней клиник договора.

    Правило (`docs/rules.md` п. 3а, ревизия волны 1): present=False («нет в документах», основа вывода «нельзя» —
    К12 «Барнаул») — только если вида помощи нет НИ в перечне клиник программы, НИ в заголовках разделов её текста
    условий. Вид помощи есть в тексте, а клиник нет → present=True со ссылками на пункты текста (что клиник нет,
    инструмент увидит в clinics_query). Сомнение решается против «нет»: основы слов в _CARE_STEMS широкие
    («стационар» в «Стационарное лечение предусматривает:» подпрограммы «Антиклещ» → present=True), а если текст
    условий программы не разобран или вид помощи не распознаётся, ячейка не строится вовсе — инструмент получит
    «не нашли» (недостаточно данных), а не ложное «нельзя».
    """
    universe: list[str] = []
    for p in programs:
        for ct in care.get(p, {}):
            if ct not in universe:
                universe.append(ct)
    out = []
    for p in programs:
        if p not in prog_ref:
            continue       # перечня нет — «не нашли» ≠ «нет» (`docs/rules.md` п. 3а): матрицу не строим
        for ct in universe:
            refs = care[p].get(ct)
            if refs:
                out.append(CareMatrix(contract_id=contract_id, program=p, care_type=ct, present=True,
                                      source_refs=refs))
                continue
            text = _text_refs(ct, heads.get(p, [])) if p in heads else None
            if text:
                out.append(CareMatrix(contract_id=contract_id, program=p, care_type=ct, present=True,
                                      source_refs=text))
            elif text is None:
                # отсутствие не доказано: текста условий нет или вид помощи по тексту не распознаётся
                warnings.append(f"{contract_id}: «{p}» × «{ct}»: нет в перечне, текст не проверен — "
                                "present=False не ставится")
            else:
                # нет ни в перечне, ни в тексте: ссылки — таблица перечня программы и её текст условий
                out.append(CareMatrix(contract_id=contract_id, program=p, care_type=ct, present=False,
                                      source_refs=[prog_ref[p], heads[p][0][0]]))
    return out


# ------------------------------------------------------------------ точка входа

def parse(kc: KitContract, root: Path) -> ParsedContract:
    warnings: list[str] = []
    documents = []
    clauses: list[Clause] = []
    clinics: list[ClinicRow] = []
    care: list[CareMatrix] = []
    facts: dict = {"programs": [], "payments": [], "franchise_ids": []}

    for n, kf in enumerate(files_of(kc, "contract")):
        d = make_document(kf, kc.id, contract_id=kc.id, group_id=kc.group_id, n=n)
        documents.append(d)
        cl, f = _parse_contract_doc(read_docx(abs_path(root, kf)), d.id, kc.id, "Договор")
        clauses.extend(cl)
        if n == 0:
            facts = f
    programs = [p.name for p in facts["programs"]]
    if not programs:
        warnings.append(f"{kc.id}: в договоре не найдена таблица программ п. 2.3")

    for n, kf in enumerate(files_of(kc, "program")):
        d = make_document(kf, kc.id, contract_id=kc.id, group_id=kc.group_id, n=n)
        documents.append(d)
        path = abs_path(root, kf)
        doc = read_docx(path)
        has_icons = any(c.icons for t in doc.tables for r in t.rows for c in r)
        fallback = None if has_icons else _vml_icons(path)
        cl, rows, care_map, prog_ref, heads = _parse_program_doc(doc, d.id, kc.id, "Программа ДМС", programs,
                                                                 fallback, warnings)
        clauses.extend(cl)
        clinics.extend(rows)
        care.extend(_care_matrix(kc.id, programs, care_map, prog_ref, heads, warnings))

    for kf in kc.files:
        if kf.doc_type not in ("contract", "program"):
            documents.append(make_document(kf, kc.id, contract_id=kc.id, group_id=kc.group_id))
            warnings.append(f"{kf.path}: тип {kf.doc_type} адаптером Ренессанса не разбирается")

    # проверки качества на каждый прогон: суммы п. 2.3 против п. 1.1 и п. 2.4 (gk2.md §1, premcheck.py)
    progs: list[Program] = facts["programs"]
    persons = sum(p.persons or 0 for p in progs)
    premium = round(sum((p.premium_per_person or 0) * (p.persons or 0) for p in progs), 2)
    if facts.get("headcount") is not None and persons != facts["headcount"]:
        warnings.append(f"{kc.id}: Σ численности п. 2.3 = {persons} ≠ п. 1.1 = {facts['headcount']}")
    if facts.get("premium_total") is not None and abs(premium - facts["premium_total"]) > 0.5:
        warnings.append(f"{kc.id}: Σ премий п. 2.3 = {premium} ≠ п. 2.4 = {facts['premium_total']}")
    if facts["payments"] and facts.get("premium_total") is not None:
        paid = round(sum(p.amount or 0 for p in facts["payments"]), 2)
        if abs(paid - facts["premium_total"]) > 0.5:
            warnings.append(f"{kc.id}: Σ взносов п. 2.7 = {paid} ≠ п. 2.4 = {facts['premium_total']}")
    ids = [c.id for c in clauses] + [r.id for r in clinics]
    if len(ids) != len(set(ids)):
        warnings.append(f"{kc.id}: повторяющиеся идентификаторы пунктов/строк")

    contract = Contract(
        id=kc.id, group_id=kc.group_id, legal_entity=kc.legal_entity, number=facts.get("number"),
        insurer=Insurer.renaissance, date=facts.get("date"), period_from=facts.get("period_from"),
        period_to=facts.get("period_to"),
        headcount=facts.get("headcount") if facts.get("headcount") is not None else (persons or None),
        programs=progs, payments=facts["payments"],
        franchise=Franchise(declared=bool(facts["franchise_ids"]), mechanics_clause_ids=facts["franchise_ids"]),
        missing_docs=[] if files_of(kc, "program") else ["Приложение 1. Программа ДМС"],
    )
    return ParsedContract(contract=contract, documents=documents, clauses=clauses, clinics=clinics, care=care,
                          warnings=warnings)


def premium_total(pc: ParsedContract) -> float | None:
    """Итог договора (п. 2.4) — из пункта договора, для сверки «сумма премий = итог» в отчёте импорта."""
    for c in pc.clauses:
        if c.number == "2.4" and c.doc_id.endswith(".contract"):
            m = re.search(r"составляет\s+([\d\s\xa0]+,\d{2})", c.text)
            return _num(m.group(1)) if m else None
    return None
