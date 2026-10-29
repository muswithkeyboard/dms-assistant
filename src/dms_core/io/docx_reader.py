"""Чтение docx с восстановлением автонумерации Word (план §6.1, docs/CONTRACTS.md §2).

pandoc как источник не используется: он теряет автонумерацию («2.6.1» → «1.»). Номера восстанавливаются из
word/numbering.xml (+ нумерация через стили word/styles.xml) — основа: прототип
reference/kit_analysis/gk3/docx_num.py. Результат сверяется с рендером LibreOffice на всём комплекте
(tests/io/test_numbering_lo.py, dms_core.io.lo_check).

Что делает чтение:
  * тело документа обходится по порядку: абзацы и таблицы (blocks); содержимое w:sdt / w:customXml
    разворачивается на месте;
  * номер абзаца = метка автонумерации (lvlText с подставленными счётчиками); текстовый номер, набранный руками
    («4.1.2. …»), остаётся в тексте — его выделяет адаптер;
  * абзацы внутри таблиц тоже нумеруются (счётчики общие с телом: список может продолжаться через таблицу);
    они лежат в Cell.paras (in_table=True), в DocxDoc.paragraphs — только абзацы тела;
  * скрытый текст (w:vanish в свойствах прогона) пропускается; абзац, у которого скрыт сам знак абзаца и весь
    текст, не попадает в результат, но счётчик списка двигает (у Зетты такие абзацы есть, docs/facts/kit/gk1.md);
    каждый такой пропуск с меткой записывается в DocxDoc.skipped_hidden (позиция, метка) — по нему сверка с
    LibreOffice (lo_check) отличает скрытый абзац от потерянного пункта;
  * сноски (word/footnotes.xml) вклеиваются в список footnotes того абзаца, где стоит ссылка, а в тексте остаётся
    метка «[^N]»;
  * картинка (DrawingML a:blip или VML v:imagedata) в ячейке таблицы → маркер «[icon:<sha1 8>]» в Cell.icons
    (у Ренессанса иконка = «по записи через центр»; смысл иконки определяет адаптер, не чтение).
"""
from __future__ import annotations

import hashlib
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lxml import etree

from dms_core.io.errors import InputError
from dms_core.io.zipguard import MAX_PART_UNPACKED, PartReader

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
V = "urn:schemas-microsoft-com:vml"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
NS = {"w": W, "r": R, "a": A, "v": V, "mc": MC}

# Защита от «бомб»: docx — тоже zip. Пределы части, суммы и степени сжатия — общие (io/zipguard.py, w16-sec);
# части читаются потоком с обрывом при превышении.
MAX_PART_BYTES = MAX_PART_UNPACKED


def q(tag: str) -> str:
    return f"{{{W}}}{tag}"


@dataclass
class Para:
    index: int
    number: str | None
    level: int | None
    text: str
    style: str | None
    footnotes: list[str] = field(default_factory=list)
    in_table: bool = False


@dataclass
class Cell:
    text: str
    icons: list[str] = field(default_factory=list)
    paras: list[Para] = field(default_factory=list)   # абзацы ячейки с номерами (in_table=True); text — их склейка
    footnotes: list[str] = field(default_factory=list)  # тексты сносок из абзацев ячейки (метки [^N] — в text)


@dataclass
class Table:
    index: int
    rows: list[list[Cell]]
    para_before: int          # индекс последнего абзаца тела перед таблицей (-1 — таблица в начале)


@dataclass
class DocxDoc:
    paragraphs: list[Para]
    tables: list[Table]
    blocks: list[tuple[str, int]]   # ("p", индекс абзаца) | ("t", индекс таблицы) — порядок тела документа
    # скрытые нумерованные абзацы, пропущенные чтением: (позиция, метка); позиция — номер абзаца в порядке
    # iter_all_paras, перед которым стоял скрытый абзац (len(...) — после последнего)
    skipped_hidden: list[tuple[int, str]] = field(default_factory=list)


class DocxError(InputError):
    """Файл не читается как docx; сообщение — по-русски, для пользователя."""

    def __init__(self, message: str, code: str = "bad_docx"):
        super().__init__(code, message)


# ------------------------------------------------------------------ форматы номеров

_RU = "абвгдежзиклмнопрстуфхцчшщэюя"
_ROMAN = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
          (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]


def _roman(n: int) -> str:
    out = []
    for v, r in _ROMAN:
        while n >= v:
            out.append(r)
            n -= v
    return "".join(out)


def _letters(n: int, alphabet: str) -> str:
    """Буквенный формат Word: a…z, затем aa…zz (буква повторяется), а не «base-26»."""
    k = len(alphabet)
    return alphabet[(n - 1) % k] * ((n - 1) // k + 1)


def _fmt(n: int, f: str) -> str:
    """Значение счётчика в формате numFmt Word."""
    if f in ("decimal", "decimalZero"):
        return f"{n:02d}" if f == "decimalZero" and n < 10 else str(n)
    if f == "upperRoman":
        return _roman(n)
    if f == "lowerRoman":
        return _roman(n).lower()
    if f == "lowerLetter":
        return _letters(n, "abcdefghijklmnopqrstuvwxyz")
    if f == "upperLetter":
        return _letters(n, "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    if f == "russianLower":
        return _letters(n, _RU)
    if f == "russianUpper":
        return _letters(n, _RU.upper())
    if f == "bullet":
        return "•"
    if f == "none":
        return ""
    return str(n)


def _val(el: Any) -> str | None:
    return el.get(q("val")) if el is not None else None


def _onoff(el: Any) -> bool:
    """Логическое свойство Word: есть элемент и val не «0/false/off»."""
    return el is not None and _val(el) not in ("0", "false", "off")


def _parse_lvl(lvl: Any) -> dict[str, Any]:
    restart = _val(lvl.find("w:lvlRestart", NS))
    return {
        "start": int(_val(lvl.find("w:start", NS)) or 1),
        "fmt": _val(lvl.find("w:numFmt", NS)) or "decimal",
        "text": _val(lvl.find("w:lvlText", NS)) or "",
        "isLgl": _onoff(lvl.find("w:isLgl", NS)),
        "restart": int(restart) if restart is not None else None,
        "pStyle": _val(lvl.find("w:pStyle", NS)),
    }


_DEFAULT_LVL: dict[str, Any] = {"start": 1, "fmt": "decimal", "text": "", "isLgl": False, "restart": None,
                                "pStyle": None}


class _Numbering:
    """Счётчики списков Word: abstractNum (уровни) ← num (экземпляр, lvlOverride/startOverride) ← стиль абзаца.

    Правила (как в Word и LibreOffice; сверено на комплекте):
      * экземпляры num одного abstractNum без переопределений делят счётчики — нумерация продолжается;
      * num с lvlOverride/startOverride — отдельный экземпляр списка со своими счётчиками;
      * абзац уровня k сбрасывает счётчики более глубоких уровней (с учётом lvlRestart);
      * isLgl: все подставляемые уровни выводятся арабскими цифрами;
      * numStyleLink: abstractNum может ссылаться на стиль нумерации, а тот — на другой num.
    """

    def __init__(self, numbering_xml: bytes | None, styles_xml: bytes | None):
        self.abs: dict[str, dict[str, Any]] = {}
        self.num: dict[str, dict[str, Any]] = {}
        self.styles: dict[str, dict[str, Any]] = {}
        self.counters: dict[str, dict[int, int]] = {}
        if numbering_xml:
            x = etree.fromstring(numbering_xml)
            for a in x.findall("w:abstractNum", NS):
                lv = {int(lvl.get(q("ilvl")) or 0): _parse_lvl(lvl) for lvl in a.findall("w:lvl", NS)}
                self.abs[a.get(q("abstractNumId")) or ""] = {
                    "lv": lv, "link": _val(a.find("w:numStyleLink", NS))}
            for n in x.findall("w:num", NS):
                ov_start: dict[int, int] = {}
                ov_lvl: dict[int, dict[str, Any]] = {}
                for o in n.findall("w:lvlOverride", NS):
                    il = int(o.get(q("ilvl")) or 0)
                    so = o.find("w:startOverride", NS)
                    if so is not None:
                        ov_start[il] = int(_val(so) or 1)
                    lvl = o.find("w:lvl", NS)
                    if lvl is not None:
                        ov_lvl[il] = _parse_lvl(lvl)
                self.num[n.get(q("numId")) or ""] = {
                    "aid": _val(n.find("w:abstractNumId", NS)), "start": ov_start, "lvl": ov_lvl}
        if styles_xml:
            s = etree.fromstring(styles_xml)
            for st in s.findall("w:style", NS):
                d: dict[str, Any] = {"based": _val(st.find("w:basedOn", NS))}
                np_ = st.find("w:pPr/w:numPr", NS)
                if np_ is not None:
                    if np_.find("w:numId", NS) is not None:
                        d["numId"] = _val(np_.find("w:numId", NS))
                    if np_.find("w:ilvl", NS) is not None:
                        d["ilvl"] = int(_val(np_.find("w:ilvl", NS)) or 0)
                d["vanish"] = _onoff(st.find("w:rPr/w:vanish", NS))
                self.styles[st.get(q("styleId")) or ""] = d

    # --- стили

    def style_numpr(self, sid: str | None, depth: int = 0) -> tuple[str | None, int | None]:
        """numId и ilvl из стиля абзаца (по цепочке basedOn). ilvl может быть None — тогда уровень ищется по
        w:lvl/w:pStyle в abstractNum, иначе 0."""
        d = self.styles.get(sid or "")
        if not d or depth > 10:
            return None, None
        if "numId" in d:
            return d["numId"], d.get("ilvl")
        return self.style_numpr(d.get("based"), depth + 1)

    def style_hidden(self, sid: str | None, depth: int = 0) -> bool:
        d = self.styles.get(sid or "")
        if not d or depth > 10:
            return False
        return bool(d.get("vanish")) or self.style_hidden(d.get("based"), depth + 1)

    def level_for_style(self, num_id: str, sid: str | None) -> int | None:
        a = self._abstract(num_id)
        if not a or not sid:
            return None
        for il, lvl in a["lv"].items():
            if lvl.get("pStyle") == sid:
                return il
        return None

    # --- списки

    def _abstract(self, num_id: str, depth: int = 0) -> dict[str, Any] | None:
        n = self.num.get(num_id)
        if not n or depth > 5:
            return None
        a = self.abs.get(n["aid"] or "")
        if a and not a["lv"] and a.get("link"):
            # numStyleLink: определение уровней лежит у списка, на который ссылается стиль нумерации
            sn, _ = self.style_numpr(a["link"])
            if sn and sn != num_id:
                return self._abstract(sn, depth + 1)
        return a

    def _levels(self, num_id: str) -> dict[int, dict[str, Any]]:
        a = self._abstract(num_id)
        lv = dict(a["lv"]) if a else {}
        lv.update(self.num[num_id]["lvl"])            # lvlOverride/w:lvl заменяет определение уровня
        return lv

    def label(self, num_id: str | None, ilvl: int) -> str:
        """Следующая метка списка num_id на уровне ilvl (счётчик сдвигается)."""
        if not num_id or num_id == "0" or num_id not in self.num:
            return ""
        n = self.num[num_id]
        lv = self._levels(num_id)
        if not lv:
            return ""
        overridden = bool(n["start"] or n["lvl"])
        key = f"num:{num_id}" if overridden else f"abs:{n['aid']}"   # lvlOverride — отдельный экземпляр
        c = self.counters.setdefault(key, {})
        cur = lv.get(ilvl, _DEFAULT_LVL)

        def start_of(k: int) -> int:
            return int(n["start"].get(k, lv.get(k, _DEFAULT_LVL)["start"]))

        c[ilvl] = c.get(ilvl, start_of(ilvl) - 1) + 1
        # уровни выше, которых ещё не было, считаются «пройденными» со значением start: после «1.1.1» без
        # предшествующего «1.1» следующий абзац уровня 1 получает «1.2» (так Word и LibreOffice; ГК 1, Зетта)
        for k in range(ilvl):
            c.setdefault(k, start_of(k))
        # сброс более глубоких уровней: по умолчанию любой более высокий уровень их перезапускает;
        # lvlRestart=r — перезапуск только когда встретился уровень с индексом < r (0 — никогда)
        for deeper in [k for k in c if k > ilvl]:
            r = lv.get(deeper, _DEFAULT_LVL)["restart"]
            if r is None or (r != 0 and ilvl < r):
                del c[deeper]

        def rep(m: re.Match[str]) -> str:
            k = int(m.group(1)) - 1
            other = lv.get(k, _DEFAULT_LVL)
            value = c.get(k, start_of(k))
            f = "decimal" if (cur.get("isLgl") and other["fmt"] != "bullet") else other["fmt"]
            return _fmt(value, f)

        if cur["fmt"] == "bullet":
            return "•"        # маркер списка: символ шрифта Symbol/Wingdings (U+F0B7 и т. п.) → единый «•»
        return re.sub(r"%(\d)", rep, cur["text"])


# ------------------------------------------------------------------ текст абзаца

_SKIP_SUBTREES = {q("pPr"), q("rPr"), q("txbxContent"), q("del"), q("moveFrom"), f"{{{MC}}}Fallback"}


def _hidden(run: Any) -> bool:
    return _onoff(run.find("w:rPr/w:vanish", NS))


def _runs(p: Any) -> Iterator[Any]:
    """Прогоны абзаца по порядку, включая гиперссылки, поля, вставки; без надписей (txbxContent) и удалений."""
    for ch in p:
        if ch.tag in _SKIP_SUBTREES:
            continue
        if ch.tag == q("r"):
            yield ch
        elif ch.tag == q("p"):
            continue
        else:
            yield from _runs(ch)


def _para_text(p: Any) -> tuple[str, list[str]]:
    """Текст абзаца без скрытых прогонов и id сносок по порядку."""
    out: list[str] = []
    notes: list[str] = []
    for run in _runs(p):
        if _hidden(run):
            continue
        for el in run:
            tag = el.tag
            if tag == q("t"):
                out.append(el.text or "")
            elif tag == q("tab") or tag == q("ptab"):
                out.append("\t")
            elif tag in (q("br"), q("cr")):
                out.append(" ")
            elif tag == q("noBreakHyphen"):
                out.append("-")
            elif tag == q("sym"):
                ch = el.get(q("char")) or ""
                try:
                    out.append(chr(int(ch, 16)))
                except ValueError:
                    pass
            elif tag == q("footnoteReference"):
                fid = el.get(q("id")) or ""
                notes.append(fid)
                out.append(f"[^{fid}]")
    return "".join(out), notes


def _footnotes(xml: bytes | None) -> dict[str, str]:
    if not xml:
        return {}
    x = etree.fromstring(xml)
    res = {}
    for f in x.findall("w:footnote", NS):
        parts = [_para_text(p)[0].strip() for p in f.iter(q("p"))]
        t = " ".join(x for x in parts if x).strip()
        if t:
            res[f.get(q("id")) or ""] = t
    return res


def _image_hashes(z: PartReader) -> dict[str, str]:
    """rId картинки → первые 8 знаков sha1 её байтов (одинаковые иконки дают одинаковый маркер)."""
    try:
        rels = etree.fromstring(z.read("word/_rels/document.xml.rels"))
    except KeyError:
        return {}
    out = {}
    for rel in rels:
        if "image" not in (rel.get("Type") or "") or rel.get("TargetMode") == "External":
            continue
        target = rel.get("Target") or ""
        path = target.lstrip("/") if target.startswith("/") else "word/" + target
        try:
            out[rel.get("Id") or ""] = hashlib.sha1(z.read(path)).hexdigest()[:8]
        except KeyError:
            continue
    return out


def _icons(el: Any, images: dict[str, str]) -> list[str]:
    """Маркеры картинок внутри элемента: DrawingML (a:blip r:embed) и VML (v:imagedata r:id).
    Ветка mc:Fallback пропускается — там дубль той же картинки для старых версий Word."""
    out: list[str] = []

    def walk(e: Any) -> None:
        for ch in e:
            if not isinstance(ch.tag, str) or ch.tag == f"{{{MC}}}Fallback":
                continue
            rid = None
            if ch.tag == f"{{{A}}}blip":
                rid = ch.get(f"{{{R}}}embed")
            elif ch.tag == f"{{{V}}}imagedata":
                rid = ch.get(f"{{{R}}}id")
            if rid and rid in images:
                out.append(f"[icon:{images[rid]}]")
            walk(ch)

    walk(el)
    return out


def _read_part(z: PartReader, name: str) -> bytes | None:
    """Часть пакета потоком в пределах zipguard (None — части нет); превышение — DocxError too_large."""
    try:
        return z.read(name)
    except KeyError:
        return None
    except InputError as e:
        raise DocxError(e.message, e.code) from None


def _blocks(parent: Any) -> Iterator[Any]:
    """Абзацы и таблицы по порядку; w:sdt/w:customXml/w:smartTag разворачиваются."""
    for ch in parent:
        if ch.tag in (q("p"), q("tbl")):
            yield ch
        elif ch.tag in (q("sdt"), q("customXml"), q("sdtContent"), q("smartTag")):
            yield from _blocks(ch)


# ------------------------------------------------------------------ документ

def read_docx(path: Path | str) -> DocxDoc:
    try:
        with zipfile.ZipFile(path) as zf:
            try:
                z = PartReader(zf, "DOCX")
            except InputError as e:
                raise DocxError(e.message, e.code) from None
            document = _read_part(z, "word/document.xml")
            if document is None:
                raise DocxError("В файле нет word/document.xml: это не документ Word (docx).")
            numbering = _Numbering(_read_part(z, "word/numbering.xml"), _read_part(z, "word/styles.xml"))
            notes = _footnotes(_read_part(z, "word/footnotes.xml"))
            images = _image_hashes(z)
    except zipfile.BadZipFile as e:
        raise DocxError("Файл не открывается как docx (повреждён или другой формат).") from e
    body = etree.fromstring(document).find("w:body", NS)
    if body is None:
        raise DocxError("В документе нет тела (w:body).")

    paragraphs: list[Para] = []
    tables: list[Table] = []
    blocks: list[tuple[str, int]] = []
    skipped_hidden: list[tuple[int, str]] = []
    seq = [0]     # сколько абзацев уже выдано в порядке iter_all_paras (тело и ячейки по порядку документа)

    def para(p: Any, in_table: bool) -> Para | None:
        style = _val(p.find("w:pPr/w:pStyle", NS))
        np_ = p.find("w:pPr/w:numPr", NS)
        num_id: str | None = None
        ilvl: int | None = None
        if np_ is not None:
            num_id = _val(np_.find("w:numId", NS))
            il = np_.find("w:ilvl", NS)
            ilvl = int(_val(il) or 0) if il is not None else None
        if num_id is None and style:
            # нумерация через стиль абзаца (ilvl абзаца, если задан, важнее уровня из стиля)
            sn, sl = numbering.style_numpr(style)
            if sn is not None:
                num_id = sn
                if ilvl is None:
                    ilvl = sl if sl is not None else numbering.level_for_style(sn, style)
        text, fids = _para_text(p)
        mark_hidden = _onoff(p.find("w:pPr/w:rPr/w:vanish", NS)) or numbering.style_hidden(style)
        label = numbering.label(num_id, ilvl or 0) if num_id else ""
        if mark_hidden and not text.strip():
            # скрытый абзац в результат не попадает, но счётчик списка двигает: в Зетте (ГК 1) перед «4.1.1»
            # стоят скрытые пустые абзацы уровней 1–4 и 4.1 — без них видимый пункт получил бы «1.1.1»
            if label:
                skipped_hidden.append((seq[0], label))
            return None
        if not text.strip() and not label:
            return None
        seq[0] += 1
        return Para(index=-1, number=label or None, level=(ilvl or 0) if label else None, text=text,
                    style=style, footnotes=[notes[f] for f in fids if f in notes], in_table=in_table)

    def cell_paras(tc: Any) -> list[Para]:
        """Абзацы ячейки по порядку; вложенные таблицы разворачиваются в ту же ячейку."""
        out: list[Para] = []
        for el in _blocks(tc):
            if el.tag == q("p"):
                pr = para(el, in_table=True)
                if pr is not None:
                    pr.index = len(out)
                    out.append(pr)
            else:
                for tr in el.findall("w:tr", NS):
                    for inner in tr.findall("w:tc", NS):
                        for pr in cell_paras(inner):
                            pr.index = len(out)
                            out.append(pr)
        return out

    for el in _blocks(body):
        if el.tag == q("p"):
            pr = para(el, in_table=False)
            if pr is not None:
                pr.index = len(paragraphs)
                paragraphs.append(pr)
                blocks.append(("p", pr.index))
            continue
        rows: list[list[Cell]] = []
        for tr in el.findall("w:tr", NS):
            cells = []
            for tc in tr.findall("w:tc", NS):
                ps = cell_paras(tc)
                parts = [f"{p.number} {p.text.strip()}".strip() if p.number else p.text.strip() for p in ps]
                cells.append(Cell(text="\n".join(x for x in parts if x), icons=_icons(tc, images), paras=ps,
                                  footnotes=[f for p in ps for f in p.footnotes]))
            rows.append(cells)
        t = Table(index=len(tables), rows=rows, para_before=len(paragraphs) - 1)
        tables.append(t)
        blocks.append(("t", t.index))
    return DocxDoc(paragraphs=paragraphs, tables=tables, blocks=blocks, skipped_hidden=skipped_hidden)


def iter_all_paras(doc: DocxDoc) -> Iterator[Para]:
    """Все абзацы в порядке документа: тело и ячейки таблиц (строка за строкой, ячейка за ячейкой)."""
    for kind, i in doc.blocks:
        if kind == "p":
            yield doc.paragraphs[i]
        else:
            for row in doc.tables[i].rows:
                for cell in row:
                    yield from cell.paras


def to_text(doc: DocxDoc) -> str:
    """«номер\\tтекст» по абзацам тела — для сверки нумерации с LibreOffice."""
    return "\n".join(f"{p.number or ''}\t{p.text}" for p in doc.paragraphs)
