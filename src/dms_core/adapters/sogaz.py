"""Адаптер «СОГАЗ» (ГК 3, договоры c09–c13): договор, программа(ы) ДМС, список МО (xlsx), памятка.

Устройство документов — docs/facts/kit/gk3.md, прототипы — reference/kit_analysis/gk3/ (xl.py, xl2.py, difs.py).
Принципы разбора:
  * ничего не «чиним»: противоречия документов (К17: программа «30 км от МКАД» против списка МО «в пределах МКАД»)
    остаются в данных как есть — пункт программы и строка клиники; адаптер лишь добавляет предупреждение;
  * номер пункта — автонумерация Word (docx_reader) или номер, набранный текстом в начале абзаца (п. 6.4.1,
    9.1.2 …); абзац без номера сохраняется отдельным пунктом с путём до ближайшего номера (п. 4.1.2: оба
    взаимоисключающих абзаца шаблона — два отдельных пункта);
  * франшиза в строке клиники: столбец «Безусловная франшиза, %» есть только у c10; нет столбца — None
    («не указано»), «—» — не применяется (это не 0 %);
  * отсутствующие документы (Правила — Прил. 1, Прил. 3, 5, 7.x, 8) берутся из перечня приложений договора
    (п. 10.9 «К настоящему Договору прилагаются…») и сверяются с файлами комплекта.
Каждый прогон детерминирован (одинаковый вход → одинаковые id и порядок) и сопровождается проверками качества
данных: несовпадения попадают в warnings, а не молча проглатываются.
"""
from __future__ import annotations

import datetime as dt
import re
import zipfile
from collections import OrderedDict
from pathlib import Path

from ..config import load_yaml
from ..constants import FRANCHISE_NOT_APPLICABLE, DocType, Insurer
from ..io.docx_reader import DocxDoc, read_docx
from ..io.xlsx_reader import read_xlsx
from ..kit import KitContract, KitFile
from ..schemas import CareMatrix, Clause, ClinicRow, Contract, Franchise, Payment, Program
from .base import ParsedContract, abs_path, files_of, make_document, text_hash

INSURER_MARKERS = ("«СОГАЗ»", "Страховое общество газовой промышленности")
REFERRAL_TEXT = "по направлению страховщика"            # столбец «Телефон» вместо номера — только по направлению
PSEUDO_CLINIC = "организация услуг по направлению страховщика"   # строка-«клиника» с зонами помощи на дому/скорой
ZONE_KINDS = ("помощь на дому", "скорая")                # виды помощи, у которых в строке псевдо-клиники — зона
MONTHS = {m: i for i, m in enumerate(
    ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября",
     "декабря"), start=1)}
DOC_LABEL = {"contract": "Договор", "program": "Программа ДМС", "clinics": "Список МО", "memo": "Памятка"}

# Номер, набранный текстом в начале абзаца: «6.4.1. …», «9.1.2 …», «IV. …» (gk3.md §4)
_TEXT_NUM = re.compile(r"^\s*((?:\d+\.)+\d*|\d+\.\d+(?:\.\d+)*|[IVX]+\.)\s")
_LETTER = re.compile(r"^[а-яa-z]\)$")
_BULLETS = {"-", "–", "—", "•", "", "o", "▪"}


# ------------------------------------------------------------------ выбор адаптера

def matches(kc: KitContract, root: Path) -> bool:
    """СОГАЗ — по преамбуле договора (первые абзацы называют страховщика), не по имени папки."""
    for kf in files_of(kc, "contract"):
        if kf.fmt != "docx":
            continue
        try:
            doc = read_docx(abs_path(root, kf))
        except (OSError, zipfile.BadZipFile, KeyError):
            continue
        head = " ".join(p.text for p in doc.paragraphs[:8])
        if any(m in head for m in INSURER_MARKERS):
            return True
    return False


# ------------------------------------------------------------------ xlsx (временно: до io/xlsx_reader.py)

# ------------------------------------------------------------------ номера и пункты

def norm_number(label: str | None, text: str) -> tuple[str | None, str]:
    """(номер пункта, вид): вид auto | text | letter | none.

    Автонумерация «1.3.» → «1.3»; «IV.» → «IV»; маркеры «-», «•» — не номер. Если автонумерации нет, номер берётся
    из начала текста (набран вручную). Буквенный подпункт «а)» возвращается как есть — родителя добавит вызывающий.
    """
    lab = (label or "").strip()
    if lab and lab not in _BULLETS and any(ch.isalnum() for ch in lab):   # «-», «•», «\uf0b7» — маркеры
        if _LETTER.match(lab):
            return lab, "letter"
        return lab.rstrip("."), "auto"
    m = _TEXT_NUM.match(text)
    if m:
        return m.group(1).rstrip("."), "text"
    return None, "none"


def _program_title(raw: str, known: list[str]) -> str:
    """Название блока программы → имена программ договора («Стандарт, Бизнес», «ВИП», «Бизнес»).

    Сопоставление без учёта регистра с названиями из таблицы п. 3.1 договора; не нашли — текст как есть.
    """
    parts = [p.strip() for p in re.split(r"[,;]| и ", raw) if p.strip()]
    found = []
    for p in parts:
        for k in known:
            if p.casefold() == k.casefold() and k not in found:
                found.append(k)
    return ", ".join(found) if found else raw.strip()


def _program_blocks(doc: DocxDoc, known: list[str]) -> dict[int, str]:
    """Индекс абзаца начала блока → название программы. Признак блока — заголовок «ПРОГРАММА(Ы) …»;
    название — остаток строки («ПРОГРАММА ВИП») или первый содержательный абзац после шапки («Стандарт, Бизнес»)."""
    starts: dict[int, str] = {}
    paras = doc.paragraphs
    for p in paras:
        m = re.match(r"^\s*ПРОГРАММ[АЫ]\b(.*)$", p.text)
        if not m or p.number or "ВКЛЮЧАЕТ" in p.text:     # «II. ПРОГРАММА ВКЛЮЧАЕТ В СЕБЯ» — раздел, не блок
            continue
        rest = m.group(1).strip()
        if not rest:
            for nxt in paras[p.index + 1:p.index + 4]:
                t = nxt.text.strip()
                if not t or "добровольного медицинского страхования" in t:
                    continue
                if nxt.number or t.isupper():   # дошли до раздела «I. ОБЩИЕ ПОЛОЖЕНИЯ» — названия нет
                    break
                rest = t
                break
        starts[p.index] = _program_title(rest, known) if rest else ""
    return starts


def _clauses_from_docx(doc: DocxDoc, *, doc_id: str, contract_id: str, label: str, doc_type: str,
                       known_programs: list[str]) -> list[Clause]:
    """Пункты документа по порядку тела: абзацы и строки таблиц.

    Абзац → Clause(id=<doc>#p<индекс>); строка таблицы → Clause(id=<doc>#t<таблица>r<строка>) с заголовком
    таблицы в тексте (таблицы лимитов программы и «Приложение №1» хранятся построчно, gk3.md §7). В памятке
    содержание лежит в таблице «раздел | пункты»: каждая строка ячейки с номером — отдельный пункт
    (<doc>#t<т>r<с>c<я>l<строка>).
    """
    out: list[Clause] = []
    blocks = _program_blocks(doc, known_programs) if doc_type == "program" else {}
    program: str | None = None
    section: str | None = None        # римский раздел программы («III»)
    last_num: str | None = None       # ближайший номер выше — для абзацев без номера и подпунктов «а)»
    last_full: str | None = None      # последний «полный» номер (без буквы) — родитель для «а)»

    def path(num: str | None) -> str:
        parts = [label]
        if program:
            parts.append(f"Программа «{program}»")
        if section and doc_type == "program":
            parts.append(f"Раздел {section}")
        if num and num == section:
            pass                               # заголовок раздела: «… › Раздел IV»
        elif num:
            parts.append(num)
        elif last_num:
            parts.append(f"{last_num} (абзац)")
        return " › ".join(parts)

    def add(cid: str, number: str | None, text: str, footnotes: list[str] | None = None):
        out.append(Clause(id=cid, doc_id=doc_id, contract_id=contract_id, number=number, path=path(number),
                          program=program, text=text, footnotes=footnotes or [], text_hash=text_hash(text)))

    for kind, i in doc.blocks:
        if kind == "p":
            p = doc.paragraphs[i]
            if i in blocks:
                program = blocks[i] or program
                section = None
                last_num = last_full = None
            number, how = norm_number(p.number, p.text)
            if how == "letter":
                number = f"{last_full} {number}" if last_full else number
            elif number and re.fullmatch(r"[IVX]+", number):
                section = number
                last_full = None
            elif number:
                last_full = number
            if number:
                last_num = number
            add(f"{doc_id}#p{i}", number, p.text.strip(), list(p.footnotes))
            continue
        t = doc.tables[i]
        if not t.rows:
            continue
        if doc_type == "memo":
            for ri, row in enumerate(t.rows):
                for ci, cell in enumerate(row):
                    for li, line in enumerate(x for x in cell.text.split("\n")):
                        line = line.strip()
                        if not line:
                            continue
                        number, _ = norm_number(None, line + " ")
                        m = re.match(r"^(\d+(?:\.\d+)*)\.(?=\S)", line)       # «1.1.Договор …» без пробела
                        if number is None and m:
                            number = m.group(1)
                        if number:
                            last_num = number
                        add(f"{doc_id}#t{i}r{ri}c{ci}l{li}", number, line)
            continue
        header = [c.text.replace("\n", " ").strip() for c in t.rows[0]]
        for ri, row in enumerate(t.rows):
            cells = [c.text.replace("\n", " ").strip() for c in row]
            if not any(cells):
                continue
            if ri == 0 or len(cells) != len(header):
                text = " | ".join(c for c in cells if c)
            else:
                text = " | ".join(f"{h}: {c}" if h and h != c else c for h, c in zip(header, cells) if c)
            out.append(Clause(id=f"{doc_id}#t{i}r{ri}", doc_id=doc_id, contract_id=contract_id, number=None,
                              path=path(None).rsplit(" (абзац)", 1)[0] + f" › табл. {i + 1}, стр. {ri + 1}",
                              program=program, text=text, text_hash=text_hash(text)))
    return out


# ------------------------------------------------------------------ карточка договора

def _num(s: str) -> float | None:
    s = re.sub(r"[\s ]", "", s or "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _ru_date(s: str) -> dt.date | None:
    """«30» апреля 2026 г. / 01.05.2026 → date."""
    m = re.search(r"«?(\d{1,2})»?\s+([а-я]+)\s+(\d{4})", s or "")
    if m and m.group(2) in MONTHS:
        return dt.date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1)))
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", s or "")
    if m:
        return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return None


def _clause_text(clauses: list[Clause], number: str) -> Clause | None:
    return next((c for c in clauses if c.number == number), None)


def _table_after(doc: DocxDoc, para_index: int) -> list[list[str]] | None:
    """Первая таблица сразу после абзаца para_index (таблица п. 3.1, график взносов)."""
    for t in doc.tables:
        if t.para_before >= para_index:
            return [[c.text.strip() for c in r] for r in t.rows]
    return None


def parse_programs(rows: list[list[str]]) -> list[Program]:
    """Таблица п. 3.1: «Наименование Программы | Коэффициент | Количество … | Премия на одно лицо | … »."""
    if not rows:
        return []
    head = [h.casefold() for h in rows[0]]

    def col(word: str) -> int | None:
        return next((k for k, h in enumerate(head) if word in h), None)

    c_name, c_cnt, c_prem = 0, col("количество"), col("на одно")
    out = []
    for r in rows[1:]:
        name = r[c_name].strip() if r else ""
        if not name or name.casefold().startswith("итого"):
            continue
        persons = _num(r[c_cnt]) if c_cnt is not None else None
        prem = _num(r[c_prem]) if c_prem is not None else None
        out.append(Program(name=name, persons=int(persons) if persons is not None else None,
                           premium_per_person=prem))
    return out


def parse_payments(rows: list[list[str]]) -> list[Payment]:
    """График взносов: «Взнос (№) | За период | Сумма взноса | Оплатить не позднее»."""
    out = []
    for r in rows[1:] if rows else []:
        if len(r) < 4 or not r[0].strip().isdigit():
            continue
        out.append(Payment(n=int(r[0]), period=r[1].strip() or None, amount=_num(r[2]), due=_ru_date(r[3])))
    return out


def find_missing_docs(lines: list[str], present_titles: list[str]) -> list[str]:
    """Приложения из перечня договора («К настоящему Договору прилагаются…»), которых нет среди файлов комплекта.

    Сверка по номеру приложения: файл «Приложение 4. Список …» закрывает «Приложение 4.»; «Приложения 7.1. – 7.6.»
    закрываются, только если в комплекте есть все номера диапазона (по номеру 7.x).
    """
    have = set()
    for t in present_titles:
        m = re.match(r"\s*Приложени[ея]\s*№?\s*(\d+(?:\.\d+)?)", t)
        if m:
            have.add(m.group(1))
    missing = []
    for line in lines:
        m = re.match(r"\s*Приложени[ея]\s*№?\s*(\d+)(?:\.(\d+))?\.?", line)
        if not m:
            continue
        main = m.group(1)
        if main in have or (m.group(2) and f"{main}.{m.group(2)}" in have):
            continue
        missing.append(re.sub(r"\s+", " ", line).strip())
    return missing


def _annex_lines(doc: DocxDoc) -> tuple[list[str], int | None]:
    """Строки перечня приложений после абзаца «…прилагаются и являются его неотъемлемой частью»."""
    start = next((p.index for p in doc.paragraphs if "прилагаются" in p.text and "неотъемлем" in p.text), None)
    if start is None:
        return [], None
    lines = []
    for p in doc.paragraphs[start + 1:]:
        if not re.match(r"\s*Приложени[ея]", p.text):
            break
        lines.append(p.text)
    return lines, start


# ------------------------------------------------------------------ список МО

def _cell(row: list[str | None], k: int | None) -> str | None:
    if k is None or k >= len(row) or row[k] is None:
        return None
    v = str(row[k]).strip()
    return v or None


def parse_franchise(v: str | None) -> float | str | None:
    """«0 %», «10%» → число; «—»/«-» → "—" (не применяется, ≠ 0); пусто → None (не указано)."""
    if v is None or not str(v).strip():
        return None
    s = str(v).strip()
    if s in ("—", "-", "–"):
        return FRANCHISE_NOT_APPLICABLE
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*%?", s)
    if m:
        return float(m.group(1).replace(",", "."))
    raise ValueError(f"нераспознанное значение франшизы: {s!r}")


def _city(address: str | None) -> str | None:
    """«125167, Москва г, …» → «Москва»; «…, Московская обл, Реутов г, …» → «Реутов»."""
    for part in (address or "").split(","):
        m = re.fullmatch(r"\s*(.+?)\s+г\.?\s*", part)
        if m:
            return m.group(1)
    return None


def _care_items(raw: str | None) -> list[str]:
    """Столбец F: несколько видов помощи через перевод строки; «;» в конце и двойные пробелы убираются."""
    out = []
    for line in (raw or "").split("\n"):
        t = re.sub(r"\s+", " ", line).strip().rstrip(";").strip()
        if t:
            out.append(t)
    return out


def parse_clinic_rows(sheets: dict[str, list[list[str | None]]], *, doc_id: str, contract_id: str
                      ) -> tuple[list[ClinicRow], list[str], bool]:
    """Строки списка МО → ClinicRow (по строке на вид помощи). Возвращает (строки, предупреждения, есть ли столбец
    франшизы). Заголовок ищется по ячейке «Программа страхования» (у СОГАЗ — строка 9); строки без программы —
    подписи сторон, они пропускаются."""
    rows_out: list[ClinicRow] = []
    warns: list[str] = []
    has_franchise_col = False
    for sheet, rows in sheets.items():
        hdr_i = next((i for i, r in enumerate(rows) if any((v or "").strip() == "Программа страхования" for v in r)),
                     None)
        if hdr_i is None:
            warns.append(f"{doc_id}: лист «{sheet}» — нет строки заголовка «Программа страхования»")
            continue
        head = [(v or "").strip().casefold() for v in rows[hdr_i]]

        def col(word: str, head: list[str] = head) -> int | None:
            return next((k for k, h in enumerate(head) if word in h), None)

        c_prog, c_name, c_addr = col("программа"), col("наименование"), col("адрес")
        c_phone, c_care, c_fr = col("телефон"), col("виды"), col("франшиз")
        has_franchise_col = has_franchise_col or c_fr is not None
        for i in range(hdr_i + 1, len(rows)):
            r = rows[i]
            program = _cell(r, c_prog)
            if not program:
                continue
            excel_row = i + 1
            ref = f"{doc_id}#{sheet}!{excel_row}"
            name = _cell(r, c_name) or ""
            address = _cell(r, c_addr)
            phone = _cell(r, c_phone) or ""
            cares = _care_items(_cell(r, c_care))
            if not name or not cares:
                warns.append(f"{ref}: пустое наименование или вид помощи")
            try:
                fr = parse_franchise(_cell(r, c_fr)) if c_fr is not None else None
            except ValueError as e:
                warns.append(f"{ref}: {e}")
                fr = None
            referral = REFERRAL_TEXT in phone.casefold()
            pseudo = PSEUDO_CLINIC in name.casefold()
            zones = [c for c in cares if c.casefold().startswith(ZONE_KINDS)] if pseudo else []
            for k, care in enumerate(cares or [""]):
                note = None
                if pseudo:
                    note = "Зона по списку МО: " + "; ".join(zones) if zones else "Организация по направлению Страховщика"
                elif referral:
                    note = "Организация услуг по направлению Страховщика"
                rows_out.append(ClinicRow(
                    id=ref if len(cares) <= 1 else f"{ref}#{k + 1}", contract_id=contract_id, doc_id=doc_id,
                    program=program, care_type=care, clinic_name=name, legal_name=name, address=address,
                    city=_city(address), franchise_pct=fr, by_referral=referral or pseudo, access_note=note,
                    source_ref=ref))
    return rows_out, warns, has_franchise_col


# ------------------------------------------------------------------ матрица «программа × вид помощи»

def _care_canon() -> list[tuple[str, list[str]]]:
    """(каноническое имя, признаки) из config/care_types.yaml: имя, синонимы и основа первого слова имени."""
    out = []
    for item in load_yaml("care_types.yaml")["care_types"].values():
        name = item["name"]
        keys = [name.casefold()] + [s.casefold() for s in item.get("synonyms", [])]
        first = name.split()[0].casefold()
        if first != "помощь" and len(first) > 6:
            keys.append(first[:-2])            # «амбулаторно-поликлиническ…», «стационарн…», «скор…»
        out.append((name, keys))
    return out


def canon_care(text: str, canon: list[tuple[str, list[str]]]) -> str | None:
    t = text.casefold()
    for name, keys in canon:
        if any(k in t for k in keys):
            return name
    return None


def build_care(contract_id: str, programs: list[str], clinics: list[ClinicRow], clauses: list[Clause]
               ) -> list[CareMatrix]:
    """present=True для пар «программа × вид помощи», подтверждённых разделом II программы («Программа включает в
    себя») или строкой списка МО. Отсутствие пары не означает «нет» — матрица хранит только найденное."""
    canon = _care_canon()
    acc: OrderedDict[tuple[str, str], list[str]] = OrderedDict()
    for c in clauses:
        if not (c.program and c.number and re.fullmatch(r"2\.\d+", c.number) and "Раздел II" in c.path):
            continue
        name = canon_care(c.text, canon)
        if not name:
            continue
        for prog in [p.strip() for p in c.program.split(",")]:
            if prog in programs:
                acc.setdefault((prog, name), []).append(c.id)
    for row in clinics:
        name = canon_care(row.care_type, canon)
        if name:
            refs = acc.setdefault((row.program, name), [])
            if row.source_ref not in refs:
                refs.append(row.source_ref)
    return [CareMatrix(contract_id=contract_id, program=p, care_type=n, present=True, source_refs=refs)
            for (p, n), refs in acc.items()]


# ------------------------------------------------------------------ разбор договора

def parse(kc: KitContract, root: Path) -> ParsedContract:
    cid = kc.id
    contract = Contract(id=cid, group_id=kc.group_id, legal_entity=kc.legal_entity, insurer=Insurer.sogaz)
    res = ParsedContract(contract=contract)

    # --- документы описи (id: <contract>.<doc_type>[.n])
    docs: list[tuple[KitFile, str]] = []
    for dtype in [t.value for t in DocType]:
        for n, kf in enumerate(files_of(kc, dtype)):
            d = make_document(kf, cid, contract_id=cid, group_id=kc.group_id, n=n)
            res.documents.append(d)
            docs.append((kf, d.id))

    # --- договор: карточка, пункты, перечень приложений
    contract_docs = [(kf, did) for kf, did in docs if kf.doc_type == "contract" and kf.fmt == "docx"]
    if not contract_docs:
        res.warnings.append(f"{cid}: нет договора (docx) — карточка не заполнена")
        return res
    kf, did = contract_docs[0]
    cdoc = read_docx(abs_path(root, kf))
    m = re.search(r"№\s*(\S+)", cdoc.paragraphs[0].text if cdoc.paragraphs else "")
    contract.number = m.group(1) if m else None
    if cdoc.tables and cdoc.tables[0].para_before <= 2:          # «г. Москва | | «30» апреля 2026 г.»
        contract.date = _ru_date(" ".join(c.text for c in cdoc.tables[0].rows[0]))
    p31 = next((p for p in cdoc.paragraphs if p.number == "3.1." and "Программам" in p.text), None)
    contract.programs = parse_programs(_table_after(cdoc, p31.index) or []) if p31 else []
    program_names = [p.name for p in contract.programs]
    cclauses = _clauses_from_docx(cdoc, doc_id=did, contract_id=cid, label=f"{DOC_LABEL['contract']} {kf.title}",
                                  doc_type="contract", known_programs=program_names)
    res.clauses.extend(cclauses)

    c13 = _clause_text(cclauses, "1.3")
    m = re.search(r"составляет\s+(\d[\d\s]*)", c13.text) if c13 else None
    contract.headcount = int(re.sub(r"\s", "", m.group(1))) if m else None
    c51 = _clause_text(cclauses, "5.1")
    if c51:
        dates = re.findall(r"«\d{1,2}»\s+[а-я]+\s+\d{4}", c51.text)
        contract.period_from = _ru_date(dates[0]) if dates else None
        contract.period_to = _ru_date(dates[1]) if len(dates) > 1 else None
    pay_para = next((p for p in cdoc.paragraphs if "уплачивается" in p.text and "порядке" in p.text), None)
    contract.payments = parse_payments(_table_after(cdoc, pay_para.index) or []) if pay_para else []

    # франшиза: объявлена, если пункт договора её устанавливает («установлена безусловная франшиза», п. 1.4)
    fr_ids = [c.id for c in cclauses if c.number and "франшиз" in c.text.casefold()]
    declared = any("установлена" in c.text and "франшиз" in c.text for c in cclauses if c.number)
    contract.franchise = Franchise(declared=declared, mechanics_clause_ids=fr_ids)

    lines, _ = _annex_lines(cdoc)
    contract.missing_docs = find_missing_docs(lines, [k.title for k in kc.files])
    if not lines:
        res.warnings.append(f"{cid}: в договоре не найден перечень приложений — missing_docs не определён")

    # п. 4.1.2: в шаблоне остались оба варианта («без ограничений» и «с учетом следующих условий») — оба пункта
    # сохранены как есть; ассистент должен показывать оба фрагмента.
    i412 = next((k for k, c in enumerate(cclauses) if c.number == "4.1.2"), None)
    if i412 is not None:
        tail = [c for c in cclauses[i412 + 1:i412 + 4] if c.number is None]
        if any("без ограничений" in c.text for c in tail) and any("с учетом следующих условий" in c.text
                                                                  for c in tail):
            res.warnings.append(f"{cid}: п. 4.1.2 договора содержит два взаимоисключающих варианта замены программы "
                                f"({', '.join(c.id for c in tail[:2])}) — оба сохранены")

    # --- программа(ы), памятка
    for kf2, did2 in docs:
        if kf2.fmt != "docx" or kf2.doc_type not in ("program", "memo"):
            continue
        d2 = read_docx(abs_path(root, kf2))
        cl = _clauses_from_docx(d2, doc_id=did2, contract_id=cid, label=f"{DOC_LABEL[kf2.doc_type]}",
                                doc_type=kf2.doc_type, known_programs=program_names)
        res.clauses.extend(cl)
        if kf2.doc_type == "program":
            res.warnings.extend(_program_checks(cid, cl, program_names))

    # --- список МО
    has_fr_col = False
    for kf3, did3 in docs:
        if kf3.doc_type != "clinics" or kf3.fmt != "xlsx":
            continue
        rows, warns, has_col = parse_clinic_rows(read_xlsx(abs_path(root, kf3)), doc_id=did3, contract_id=cid)
        res.clinics.extend(rows)
        res.warnings.extend(warns)
        has_fr_col = has_fr_col or has_col
    if contract.franchise.declared and not has_fr_col:
        res.warnings.append(f"{cid}: размер франшизы в документах не указан (франшиза объявлена п. 1.4 договора, "
                            f"в списке МО нет столбца «Безусловная франшиза, %») — уточнить у СОГАЗ")

    res.warnings.extend(_zone_conflicts(cid, program_names, res.clauses, res.clinics))
    res.care = build_care(cid, program_names, res.clinics, res.clauses)
    res.warnings.extend(_quality_checks(res))
    return res


def _program_checks(cid: str, clauses: list[Clause], programs: list[str]) -> list[str]:
    """Предупреждения по программе: битая ссылка п. 7.4 на «Раздел 5» и состав блоков программ."""
    warns = []
    for c in clauses:
        # п. 7.4 или абзац под ним («…указана в Разделе 5 Программы»), а в разделе V того же блока процентов нет
        in_74 = c.number == "7.4" or c.path.endswith("› 7.4 (абзац)")
        if in_74 and "франшиз" in c.text and re.search(r"Раздел[ае]?\s*(5|V)\b", c.text):
            sec5 = [x for x in clauses if x.program == c.program and re.search(r"› Раздел V( ›|$)", x.path)]
            if not any("%" in x.text for x in sec5):
                warns.append(f"{cid}: {c.id} (п. 7.4 программы «{c.program}») ссылается на размер франшизы "
                             f"в Разделе 5 Программы, но в разделе V размера нет — битая ссылка")
    blocks = {c.program for c in clauses if c.program}
    covered = {p.strip() for b in blocks for p in b.split(",")}
    for p in programs:
        if p not in covered:
            warns.append(f"{cid}: программа «{p}» из п. 3.1 договора не найдена в документе программ")
    return warns


def _zone_conflicts(cid: str, programs: list[str], clauses: list[Clause], clinics: list[ClinicRow]) -> list[str]:
    """К17: зона помощи на дому в программе (п. 3.2 «в пределах 30 км от МКАД») и в строке псевдо-клиники списка МО
    («в пределах МКАД»). Ничего не исправляется: оба текста остаются в данных, здесь — только предупреждение с id."""
    warns = []
    for prog in programs:
        prog_cl = [c for c in clauses if c.number == "3.2" and c.program
                   and prog in [x.strip() for x in c.program.split(",")] and "на дому" in c.text]
        rows = [r for r in clinics if r.program == prog and PSEUDO_CLINIC in r.clinic_name.casefold()
                and r.care_type.casefold().startswith("помощь на дому")]
        if not prog_cl or not rows:
            continue
        p_km = "30 км" in prog_cl[0].text
        r_km = any("30 км" in r.care_type for r in rows)
        if p_km != r_km:
            warns.append(f"{cid}: зона помощи на дому по программе «{prog}» различается: программа {prog_cl[0].id} "
                         f"и список МО {rows[0].source_ref} — оба фрагмента сохранены (противоречие документов)")
    return warns


def _quality_checks(res: ParsedContract) -> list[str]:
    """Проверки качества на каждый прогон: сумма численности программ = п. 1.3, программы списка МО есть в п. 3.1,
    уникальность id, contract_id у всех строк."""
    c = res.contract
    warns = []
    if c.programs and c.headcount is not None:
        total = sum(p.persons or 0 for p in c.programs)
        if total != c.headcount:
            warns.append(f"{c.id}: сумма численности по программам {total} ≠ п. 1.3 ({c.headcount})")
    names = {p.name for p in c.programs}
    for prog in sorted({r.program for r in res.clinics} - names):
        warns.append(f"{c.id}: в списке МО программа «{prog}», которой нет в п. 3.1 договора")
    ids = [x.id for x in res.clauses] + [x.id for x in res.clinics]
    if len(ids) != len(set(ids)):
        warns.append(f"{c.id}: повторяющиеся id пунктов/строк клиник")
    if any(x.contract_id != c.id for x in [*res.clauses, *res.clinics]):
        warns.append(f"{c.id}: строка с чужим contract_id")
    return warns
