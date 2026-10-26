"""Адаптер «Зетта Страхование жизни» (ГК 1, договоры № 1–4; план §6.2, docs/facts/kit/gk1.md).

Один docx «Договор № N (с приложениями)» содержит всё: текст договора (разделы 1–12 с таблицами численности,
премии и графика взносов), Приложение 2 «Варианты программ страхования» (одностолбцовая таблица T11 + текст
«ОБЪЕМ И ПОРЯДОК ПОЛУЧЕНИЯ МЕДИЦИНСКИХ УСЛУГ»), Приложение 3 (список застрахованных, обезличен) и Приложение 4.

Что делает разбор (всё детерминировано: одинаковый вход → одинаковые id и строки, повторный импорт идемпотентен):
  * пункты — каждый абзац и строка таблицы → Clause; номер — автонумерация Word из docx_reader или номер, набранный
    текстом («2.8.1.», «4.3.2», «1.1б.»), он выделяется в `number` и убирается из текста;
  * карточка — номер и дата договора, срок (п. «вступает в силу»), численность, программы (таблицы численности
    и премии), график взносов (таблица «№ платежа»), франшиза (заявлена ли и пункты механики);
  * клиники — автомат по строкам T11: «Вариант № N» → вид помощи → [«Франшиза (требуется активация) - X%.»]
    → «k. ЛПУ Адрес: …»; блоки «Общие для вариантов: 1 …, 3 …» / «Общие для всех вариантов» относятся ко всем
    перечисленным вариантам (строка клиники и заголовок раздела размножаются на каждый вариант);
  * матрица «программа × вид помощи» — заголовки разделов T11 в области каждого варианта (present=True).
    Отсутствие вида помощи (present=False) адаптер не выводит: это делает слой инструментов по config/care_types.yaml.

Проверки качества на каждый прогон (data-engineer: «no data quality assertions per run» — анти-паттерн) пишутся
в `warnings`: сумма «численность × тариф» = итог п. 2.4, сумма взносов = итог, численность = сумма по программам,
варианты T11 = программы договора, у каждой строки клиники есть адрес или это заглушка «ЛПУ по выбору Страховщика».
"""
from __future__ import annotations

import datetime as dt
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from ..constants import FRANCHISE_NOT_APPLICABLE, Insurer
from ..io.docx_reader import DocxDoc, Para, Table, read_docx
from ..kit import KitContract
from ..schemas import CareMatrix, Clause, ClinicRow, Contract, Franchise, Payment, Program
from .base import ParsedContract, abs_path, files_of, make_document, text_hash

# ---------------------------------------------------------------- распознавание формата

# Признак Зетты — наименование страховщика в преамбуле договора (не имя папки, план §6.2)
_INSURER_RE = re.compile(r"Зетта\s+Страхование", re.IGNORECASE)
_PREAMBLE_CHARS = 4000


def _docx_plain_text(path: Path, limit: int) -> str:
    """Первые `limit` знаков текста document.xml без разметки — дешёвая проверка преамбулы без полного разбора."""
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", "replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return ""
    xml = re.sub(r"</w:p>", "\n", xml[: limit * 20])
    return re.sub(r"<[^>]+>", "", xml)[:limit]


def matches(kc: KitContract, root: Path) -> bool:
    """Договор Зетты: в преамбуле документа типа contract (docx) назван страховщик «Зетта Страхование …»."""
    for kf in files_of(kc, "contract"):
        if kf.fmt == "docx" and _INSURER_RE.search(_docx_plain_text(abs_path(root, kf), _PREAMBLE_CHARS)):
            return True
    return False


# ---------------------------------------------------------------- числа и даты

_MONTHS = {m: i + 1 for i, m in enumerate([
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря"])}


def parse_amount(s: str) -> float | None:
    """Сумма в любом из трёх форматов договоров Зетты: «56.760,00», «227 040.00», «19 340,00», «2.322.095,00».

    Правило: запятая — всегда десятичный разделитель (точки тогда — разряды); без запятой несколько точек — разряды,
    одна точка — десятичная. Пробелы (в т. ч. неразрывные) — разряды.
    """
    s = re.sub(r"\s", "", s or "")
    m = re.search(r"\d[\d.,]*", s)
    if not m:
        return None
    s = m.group(0).rstrip(".,")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def _date(s: str) -> dt.date | None:
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", s or "")
    return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None


def _date_words(s: str) -> dt.date | None:
    """«"10" декабря 2025 г.» → 2025-12-10."""
    m = re.search(r"(\d{1,2})\W*\s+([а-я]+)\s+(\d{4})", s or "")
    if not m or m.group(2) not in _MONTHS:
        return None
    return dt.date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))


# ---------------------------------------------------------------- номера пунктов

# Автономер, пригодный как номер пункта: «2.6.1.», «1.», «1б.», «4.4.1». Маркеры списков («•», мягкий перенос,
# символ из частной области шрифта) и «1)» номером пункта не считаются.
_NUM_OK = re.compile(r"^\d+(?:\.\d+)*\.?(?:[а-я]\.?)?$")
# Номер, набранный текстом в начале абзаца: только многоуровневый («2.8.1.», «4.3.2», «10.3.1.», «1.1б.»), чтобы
# не принять за номер начало перечня «1. …» или сумму.
_TEXT_NUM = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2})+\.?[а-я]?\.?)(?:\s+|\t)(?=\S)")


def split_number(p: Para) -> tuple[str | None, str]:
    """(номер, текст): автономер из numbering.xml, иначе номер, набранный текстом (он убирается из текста)."""
    text = p.text.strip()
    num = (p.number or "").strip()
    if num and _NUM_OK.match(num):
        return num, text
    m = _TEXT_NUM.match(text)
    if m:
        return m.group(1), text[m.end():].strip()
    return None, text


def _is_heading(text: str) -> bool:
    """Заголовок раздела: текст набран прописными («ПРЕДМЕТ ДОГОВОРА», «ОБЪЕМ И ПОРЯДОК …»)."""
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 6 and all(c.isupper() for c in letters if c.isalpha() and c.lower() != c.upper())


# ---------------------------------------------------------------- T11: варианты, виды помощи, клиники

_VARIANT_RE = re.compile(r"^Вариант\s*№\s*(\d+)\s+(.+?)\s*$")
_COMMON_SOME_RE = re.compile(r"^Общие для вариантов\s*:\s*(.+)$")
_COMMON_ALL_RE = re.compile(r"^Общие (?:программы )?для всех вариантов", re.IGNORECASE)
_ITEM_RE = re.compile(r"^(\d+)\.\s+(.*)$", re.DOTALL)
_FRANCHISE_RE = re.compile(r"^Франшиза\s*\(требуется активация\)\s*-\s*(\d+(?:[.,]\d+)?)\s*%\s*[.,]?\s*", re.IGNORECASE)
_CITY_RE = re.compile(r"(?:^|,\s*)([^,]+?)\s+г(?:\s*,|$)")
_INSURER_CHOICE = "ЛПУ по выбору Страховщика"
# Строки-заглушки вместо конкретной клиники: «ЛПУ по выбору Страховщика (с франшизой в этом ЛПУ, если она
# предусмотрена программой страхования) - предварительно нужно обратиться на пульт …», «В базовых ЛПУ по программе
# страхования (…)». Адреса у них нет, размер франшизы не указан.
_STUB_RE = re.compile(r"^(ЛПУ по выбору Страховщика|В базовых ЛПУ[^(]*?)\s*(?:\(|-|$)")
_T11_HEADER = "Виды медицинской помощи и Медицинские учреждения"


@dataclass
class _Scope:
    """Область действия строк T11: вариант № N или блок «Общие для …» (список номеров вариантов)."""
    title: str
    variants: list[int]


@dataclass
class T11Row:
    row: int
    kind: str                      # variant | common | section | item
    text: str
    scope: _Scope | None = None
    section: str | None = None     # заголовок раздела (вид помощи) как в документе
    care_type: str | None = None   # вид помощи без префикса франшизы, нормализованный
    franchise_pct: float | None = None
    n: int | None = None
    name: str | None = None
    address: str | None = None


@dataclass
class T11Parse:
    variants: dict[int, str] = field(default_factory=dict)    # № → название программы
    rows: list[T11Row] = field(default_factory=list)


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\u00a0", " ")).strip()


def care_type_of(section: str) -> tuple[str, float | None]:
    """Заголовок раздела → (вид помощи, % франшизы). Префикс «Франшиза (требуется активация) - 15%.» снимается,
    регистр первой буквы и точка в конце выравниваются, чтобы «программа "Стандарт". Вызов врача на дом.» под
    франшизой и «Программа "Стандарт". Вызов врача на дом» без неё были одним видом помощи."""
    s = _norm_ws(section)
    pct = None
    m = _FRANCHISE_RE.match(s)
    if m:
        pct = float(m.group(1).replace(",", "."))
        s = s[m.end():]
    s = s.rstrip(" .:")
    return (s[:1].upper() + s[1:]) if s else s, pct


def parse_t11(table: Table) -> T11Parse:
    """Автомат по строкам одностолбцовой T11 (связь только порядком строк, docs/facts/kit/gk1.md §2)."""
    out = T11Parse()
    scope: _Scope | None = None
    section: str | None = None
    for ri, cells in enumerate(table.rows):
        text = _norm_ws(" ".join(c.text for c in cells))
        if not text or text == _T11_HEADER:
            continue
        m = _VARIANT_RE.match(text)
        if m:
            n = int(m.group(1))
            out.variants[n] = m.group(2)
            scope, section = _Scope(text, [n]), None
            out.rows.append(T11Row(ri, "variant", text, scope))
            continue
        m = _COMMON_SOME_RE.match(text)
        if m:
            # «Общие для вариантов: 1 Муром Стандарт, 3 Нижний Новгород …» → [1, 3]; номер — число перед названием
            nums = [int(x) for x in re.findall(r"(?:^|,)\s*(\d+)\s+\S", m.group(1))]
            # заголовок блока служит разделом для строк ЛПУ, идущих сразу за ним (так в «Общие программы …»)
            scope, section = _Scope(text, nums), text
            out.rows.append(T11Row(ri, "common", text, scope))
            continue
        if _COMMON_ALL_RE.match(text):
            scope, section = _Scope(text, []), text   # пустой список = все варианты (заполняется в конце)
            out.rows.append(T11Row(ri, "common", text, scope))
            continue
        m = _ITEM_RE.match(text)
        if m and section is not None:
            body = m.group(2)
            name, _, addr = body.partition(" Адрес: ")
            ct, pct = care_type_of(section)
            out.rows.append(T11Row(ri, "item", text, scope, section, ct, pct, int(m.group(1)), name.strip(),
                                   addr.strip() or None))
            continue
        section = text
        ct, pct = care_type_of(text)
        out.rows.append(T11Row(ri, "section", text, scope, section, ct, pct))
    every = sorted(out.variants)
    for r in out.rows:
        if r.scope is not None and not r.scope.variants:
            r.scope.variants = every
    return out


def _city(address: str | None) -> str | None:
    """Город из адреса «344019, Ростовская обл, Ростов-на-Дону г, 13-я линия ул, д.8» → «Ростов-на-Дону»."""
    if not address:
        return None
    m = _CITY_RE.search(address)
    return m.group(1).strip() if m else None


def _regions(program: str) -> list[str]:
    """Регионы из названия программы: часть до «Стандарт»/«Премиум», города через «+»
    («Челябинск+Магнитогорск+Учалы Стандарт» → 3 города). Источник — только название из договора."""
    head = re.split(r"\s+(?:Стандарт|Премиум)\b", program, maxsplit=1)[0]
    return [x.strip() for x in head.split("+") if x.strip()]


# ---------------------------------------------------------------- таблицы договора

def _find_table(doc: DocxDoc, pred) -> Table | None:
    for t in doc.tables:
        if t.rows and pred([_norm_ws(c.text) for c in t.rows[0]]):
            return t
    return None


def _cells(row) -> list[str]:
    return [_norm_ws(c.text) for c in row]


def _programs(doc: DocxDoc) -> tuple[list[Program], list[str]]:
    """Программы: численность — таблица «Численность | Название программы», тариф — «Сумма, руб. в год»."""
    warn: list[str] = []
    t_persons = _find_table(doc, lambda h: h[:1] == ["Численность"] and "Название программы" in h)
    t_premium = _find_table(doc, lambda h: bool(h) and h[0].startswith("Сумма, руб"))
    persons: dict[str, int] = {}
    order: list[str] = []
    if t_persons:
        for row in t_persons.rows[1:]:
            c = _cells(row)
            if len(c) >= 3 and c[0].isdigit():
                persons[c[-1]] = int(c[0])
                order.append(c[-1])
    premium: dict[str, tuple[float | None, int | None]] = {}
    if t_premium:
        for row in t_premium.rows[1:]:
            c = _cells(row)
            if len(c) >= 5:
                premium[c[-1]] = (parse_amount(c[0]), int(c[2]) if c[2].isdigit() else None)
                if c[-1] not in order:
                    order.append(c[-1])
    if not t_persons:
        warn.append("не найдена таблица численности по программам (п. 2.1)")
    if not t_premium:
        warn.append("не найдена таблица премии на одного застрахованного (п. 2.3)")
    progs = []
    for name in order:
        pp, pn = premium.get(name, (None, None))
        n = persons.get(name, pn)
        if pn is not None and name in persons and pn != persons[name]:
            warn.append(f"программа «{name}»: численность в п. 2.1 ({persons[name]}) ≠ п. 2.3 ({pn})")
        progs.append(Program(name=name, premium_per_person=pp, persons=n, regions=_regions(name)))
    return progs, warn


def _payments(doc: DocxDoc) -> list[Payment]:
    """График взносов: таблица «№ платежа | Предельная дата | Начало периода | Окончание периода | Сумма | Валюта»."""
    t = _find_table(doc, lambda h: bool(h) and h[0].startswith("№ платежа"))
    out: list[Payment] = []
    if not t:
        return out
    for row in t.rows[1:]:
        c = _cells(row)
        if len(c) >= 5 and c[0].isdigit():
            period = f"{c[2]}–{c[3]}" if c[2] and c[3] else None
            out.append(Payment(n=int(c[0]), due=_date(c[1]), period=period, amount=parse_amount(c[4])))
    return out


# ---------------------------------------------------------------- разбор договора

def parse(kc: KitContract, root: Path) -> ParsedContract:
    kfs = [f for f in files_of(kc, "contract") if f.fmt == "docx"]
    contract = Contract(id=kc.id, group_id=kc.group_id, legal_entity=kc.legal_entity, insurer=Insurer.zetta)
    out = ParsedContract(contract=contract)
    if not kfs:
        out.warnings.append("нет docx договора — адаптер Зетты разбирать нечего")
        return out
    kf = kfs[0]
    for extra in kc.files:
        if extra is not kf:
            out.warnings.append(f"{extra.title}: файл не разобран адаптером Зетты (ожидается один docx договора)")
    document = make_document(kf, kc.id, contract_id=kc.id, group_id=kc.group_id)
    out.documents.append(document)
    doc = read_docx(abs_path(root, kf))
    did = document.id

    def add(cid: str, number: str | None, path: list[str], text: str, *, program: str | None = None,
            footnotes: list[str] | None = None) -> Clause:
        c = Clause(id=cid, doc_id=did, contract_id=kc.id, number=number, path=" › ".join(x for x in path if x),
                   program=program, text=text, footnotes=footnotes or [], text_hash=text_hash(text))
        out.clauses.append(c)
        return c

    # --- проход по телу документа в порядке блоков: части «Договор» / «Приложение N», разделы, пункты
    part = "Договор"
    section: str | None = None
    last_number: str | None = None
    t11: Table | None = None
    present_appendices: set[str] = set()
    appendix_refs: list[tuple[str, str]] = []     # (номер приложения из перечня «ПРИЛОЖЕНИЯ:», текст)
    mechanics: list[str] = []                     # пункты механики франшизы
    premium_total: float | None = None
    for kind, idx in doc.blocks:
        if kind == "p":
            p = doc.paragraphs[idx]
            number, text = split_number(p)
            if not text:
                continue
            m = re.match(r"^Приложение\s+(\d+[а-я]?)$", text)
            if m:
                part, section, last_number = f"Приложение {m.group(1)}", None, None
                present_appendices.add(m.group(1))
            elif _is_heading(text):
                section, last_number = _norm_ws(text).rstrip(":"), number
            elif number:
                last_number = number
            if section and section.startswith("ПРИЛОЖЕНИЯ") and number and not _is_heading(text):
                appendix_refs.append((number, text))
            path = [part, section, number or (f"к п. {last_number}" if last_number else None)]
            c = add(f"{did}#p{idx}", number, path, text, footnotes=p.footnotes)
            if "франшиз" in text.casefold():
                mechanics.append(c.id)
            if number and "сумма страховой премии по настоящему Договору" in text and premium_total is None:
                premium_total = parse_amount(text.split("Договору", 1)[1])
        else:
            t = doc.tables[idx]
            first = _norm_ws(t.rows[0][0].text) if t.rows and t.rows[0] else ""
            if first == _T11_HEADER:
                t11 = t
                continue          # T11 разбирается автоматом ниже
            for ri, row in enumerate(t.rows):
                cells = _cells(row)
                body = " | ".join(x for x in cells if x)
                # строки без содержания: обезличенные данные, подписи-линии
                if not body or not re.sub(r"\[[^\]]*\]|_+|\||М\.П\.|\s", "", body):
                    continue
                path = [part, section, f"к п. {last_number}" if last_number else None, f"таблица {idx + 1}"]
                add(f"{did}#t{idx}r{ri}", None, path, body)

    # --- карточка договора
    head = " ".join(p.text for p in doc.paragraphs[:6])
    m = re.search(r"ДОГОВОР\s*№\s*([\w\-/]+)", head)
    contract.number = m.group(1) if m else None
    if doc.tables and doc.tables[0].rows:
        contract.date = _date_words(" ".join(c.text for c in doc.tables[0].rows[0]))
    for p in doc.paragraphs:
        if contract.period_from is None and "вступает в силу" in p.text:
            ds = re.findall(r"\d{2}\.\d{2}\.\d{4}", p.text)
            if len(ds) >= 2:
                contract.period_from, contract.period_to = _date(ds[0]), _date(ds[1])
        if contract.headcount is None and "Общая численность Застрахованных" in p.text:
            mm = re.search(r"составляет\s+(\d[\d\s]*)\s*\(", p.text)
            contract.headcount = int(re.sub(r"\s", "", mm.group(1))) if mm else None
    contract.programs, warn = _programs(doc)
    out.warnings += warn
    contract.payments = _payments(doc)

    # --- Приложение 2, T11: клиники, разделы как пункты программ, матрица
    if t11 is None:
        out.warnings.append("не найдена таблица вариантов программ (Приложение 2, T11): клиник нет")
        parsed = T11Parse()
    else:
        parsed = parse_t11(t11)
        _emit_t11(out, parsed, did, kc.id, t11.index, mechanics)

    contract.franchise = Franchise(
        declared=any(r.franchise_pct for r in parsed.rows if r.kind == "item") or bool(mechanics),
        mechanics_clause_ids=list(dict.fromkeys(mechanics)))

    # --- документы, на которые договор ссылается, но которых нет в docx (Приложения 1, 1б, …)
    contract.missing_docs = _missing_docs(appendix_refs, present_appendices, root, kf.path)

    out.warnings += _quality(out, parsed, premium_total)
    return out


def _emit_t11(out: ParsedContract, parsed: T11Parse, did: str, cid: str, ti: int, mechanics: list[str]) -> None:
    """Строки T11 → ClinicRow (по одной на вариант области), заголовки разделов → Clause и CareMatrix."""
    care: dict[tuple[str, str], CareMatrix] = {}
    for r in parsed.rows:
        ref = f"{did}#t{ti}r{r.row}"
        variants = r.scope.variants if r.scope else []
        if r.kind in ("variant", "common"):
            add_text = r.text
            for n in variants or [None]:
                prog = parsed.variants.get(n) if n is not None else None
                out.clauses.append(Clause(
                    id=f"{ref}" + (f"@v{n}" if n is not None and r.kind == "common" else ""), doc_id=did,
                    contract_id=cid, path=" › ".join(["Приложение 2", r.scope.title if r.scope else ""]),
                    program=prog, text=add_text, text_hash=text_hash(add_text)))
            continue
        if r.kind == "section":
            for n in variants:
                prog = parsed.variants.get(n)
                cl = Clause(id=f"{ref}@v{n}", doc_id=did, contract_id=cid, program=prog,
                            path=" › ".join(["Приложение 2", r.scope.title if r.scope else ""]),
                            text=r.text, text_hash=text_hash(r.text))
                out.clauses.append(cl)
                if "франшиз" in r.text.casefold() and r.franchise_pct is None:
                    mechanics.append(cl.id)        # «Для посещения клиник … с франшизой … платеж 2000 рублей»
                if prog and r.care_type:
                    cm = care.setdefault((prog, r.care_type), CareMatrix(
                        contract_id=cid, program=prog, care_type=r.care_type, present=True))
                    cm.source_refs.append(ref)
            continue
        # строка клиники: франшиза — на паре «клиника + вид помощи» (из заголовка раздела над ней)
        stub = _STUB_RE.match(r.name or "")
        insurer_choice = (r.name or "").startswith(_INSURER_CHOICE)
        by_referral = insurer_choice or "по направлению" in (r.section or "").casefold()
        if r.franchise_pct is not None:
            pct: float | str | None = r.franchise_pct
            note = "Франшиза (требуется активация)"
        elif stub:
            pct, note = None, r.name          # «… с франшизой в этом ЛПУ, если она предусмотрена» — размер не указан
        else:
            # п. 2.11: франшиза применяется только там, где она указана в Приложении 2. У строки без префикса
            # «Франшиза (требуется активация) - X%» франшиза не применяется: «—», а не 0 % — «0 %» в документе
            # не написано (docs/CONTRACTS.md §1: «—» ≠ 0)
            pct, note = FRANCHISE_NOT_APPLICABLE, None
        name = stub.group(1).strip() if stub else (r.name or "")
        for n in variants:
            prog = parsed.variants.get(n)
            if prog is None:
                continue
            out.clinics.append(ClinicRow(
                # id по docs/CONTRACTS.md §1: «<doc_id>#t<т>r<с>»; строка размножена по вариантам → «#v<n>»
                id=f"{ref}#v{n}", contract_id=cid, doc_id=did, program=prog,
                care_type=r.care_type or "", clinic_name=name, address=r.address, city=_city(r.address),
                franchise_pct=pct, by_referral=by_referral, access_note=note, source_ref=ref))
    out.care.extend(care.values())


def _missing_docs(refs: list[tuple[str, str]], present: set[str], root: Path, contract_path: str) -> list[str]:
    """Приложения из перечня раздела «ПРИЛОЖЕНИЯ:», которых нет в docx. Правила, лежащие файлом в папке ГК
    (PDF правил НС — Приложение 1б), отсутствующими не считаются: сверка по названию файла."""
    group_dir = root / Path(contract_path).parts[0]
    titles = [p.stem.casefold() for p in group_dir.rglob("*.pdf")] if group_dir.is_dir() else []
    out: list[str] = []
    for number, text in refs:
        top = re.match(r"^(\d+)", number)
        if not top or top.group(1) in present:
            continue
        core = re.sub(r",?\s*утвержденн.*$", "", text).strip(" .").casefold()
        if any(core and core in t for t in titles):
            continue
        out.append(f"Приложение {number.rstrip('.')} — {text.strip()}")
    return out


def _quality(out: ParsedContract, parsed: T11Parse, total: float | None) -> list[str]:
    """Проверки качества разбора на каждый прогон; расхождение — предупреждение, не исключение."""
    w: list[str] = []
    c = out.contract
    s = sum((p.premium_per_person or 0) * (p.persons or 0) for p in c.programs)
    if total is None:
        w.append("не найден итог страховой премии (п. 2.4)")
    elif abs(s - total) > 0.01:
        w.append(f"сумма «численность × тариф» {s:.2f} ≠ итог премии {total:.2f}")
    pay = sum(p.amount or 0 for p in c.payments)
    if total is not None and c.payments and abs(pay - total) > 0.01:
        w.append(f"сумма взносов {pay:.2f} ≠ итог премии {total:.2f}")
    if c.headcount is not None and c.programs and sum(p.persons or 0 for p in c.programs) != c.headcount:
        w.append("численность п. 2.1 ≠ сумме по программам")
    names = {p.name for p in c.programs}
    for n, v in sorted(parsed.variants.items()):
        if v not in names:
            w.append(f"вариант № {n} «{v}» не совпадает ни с одной программой договора")
    for name in names - set(parsed.variants.values()):
        w.append(f"программа «{name}» не найдена среди вариантов Приложения 2")
    ids = [x.id for x in out.clauses]
    if len(ids) != len(set(ids)):
        w.append("повторяющиеся id пунктов")
    if any(r.contract_id != c.id for r in out.clinics):
        w.append("строка клиники с чужим contract_id")
    no_addr = sum(1 for r in out.clinics if not r.address and not _STUB_RE.match(r.clinic_name))
    if no_addr:
        w.append(f"строк клиник без адреса: {no_addr}")
    return w


def total_premium(parsed: ParsedContract) -> float | None:
    """Итог премии из пункта «Общая сумма страховой премии по настоящему Договору …» (для отчёта импорта)."""
    for cl in parsed.clauses:
        if cl.number and "сумма страховой премии по настоящему Договору" in cl.text:
            return parse_amount(cl.text.split("Договору", 1)[1])
    return None


__all__ = ["care_type_of", "matches", "parse", "parse_amount", "parse_t11", "split_number", "total_premium"]
