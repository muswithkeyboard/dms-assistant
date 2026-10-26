"""Сверка автонумерации read_docx с рендером LibreOffice (план §14: номера пунктов совпадают с LO ≥ 99 %).

LibreOffice выгружает docx в текст (`soffice --headless --convert-to "txt:Text (encoded):UTF8"`): строка абзаца =
отступ + метка списка + табуляция/пробел + текст; абзацы ячеек таблиц — отдельными строками. Сверка:
  1. абзацы read_docx (тело и ячейки, iter_all_paras) и строки LO выравниваются по «ключу» — только буквы текста
     (цифры выброшены: в LO номер сноски стоит в тексте, у нас — метка «[^N]»);
  2. у каждой выровненной пары метка LO = часть строки до начала нашего текста;
  3. «нумерованная» пара — у нас или у LO метка содержит букву/цифру (маркеры «•» не считаются номерами);
     совпадение — метки равны. Нумерованные строки без пары считаются расхождением (кроме исключённых правилом
     ниже) — доля не завышается.
Исключение (docs/facts/kit/gk1.md): LO показывает метку у скрытых пустых абзацев (w:vanish), Word — нет. Чтение
docx явно сообщает о таких пропусках (DocxDoc.skipped_hidden: позиция и метка); они вставляются в нашу сторону
как «скрытые» сегменты, и только метки LO, выровненные с ними, исключаются из доли (hidden_skipped). Любая другая
нумерованная метка LO, не занятая нашим абзацем, — расхождение «нет у нас» (lo_extra): потерянный пункт снижает
долю.
Файл без автонумерации ни у нас, ни у LO (ГК 2) — статус no_numbering: не проверяется, в долю не входит.

Запуск отчёта по комплекту: `uv run python -m dms_core.io.lo_check` → data/derived/numbering_check.json.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dms_core.io.docx_reader import iter_all_paras, read_docx

SOFFICE_CANDIDATES = ("soffice", "libreoffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice")


def find_soffice() -> str | None:
    for name in SOFFICE_CANDIDATES:
        found = shutil.which(name) or (name if Path(name).is_file() else None)
        if found:
            return found
    return None


def lo_text(docx: Path, soffice: str, cache_dir: Path | None = None, timeout: int = 180) -> list[str]:
    """Строки текстовой выгрузки LibreOffice (с кэшем по имени и времени изменения файла)."""
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        # hash() строк случаен между процессами — ключ кэша через sha1
        ident = f"{docx.resolve()}|{docx.stat().st_mtime_ns}".encode()
        cached = cache_dir / f"{hashlib.sha1(ident).hexdigest()[:16]}.txt"
        if cached.is_file():
            return cached.read_text(encoding="utf-8-sig").splitlines()
    with tempfile.TemporaryDirectory() as tmp:
        # отдельный профиль: не мешает открытому LibreOffice пользователя
        profile = Path(tmp) / "profile"
        subprocess.run([soffice, f"-env:UserInstallation=file://{profile}", "--headless", "--convert-to",
                        "txt:Text (encoded):UTF8", "--outdir", tmp, str(docx)],
                       capture_output=True, check=True, timeout=timeout)
        out = Path(tmp) / (docx.stem + ".txt")
        text = out.read_text(encoding="utf-8-sig")
    if cache_dir is not None:
        cached.write_text(text, encoding="utf-8")
    return text.splitlines()


_NOTE = re.compile(r"\[\^-?\d+\]")


def _key(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalpha())


def _numbered(label: str) -> bool:
    return any(ch.isalnum() for ch in label)


@dataclass
class FileCheck:
    file: str
    status: str = "checked"      # checked | no_numbering (нет автонумерации — не проверяется)
    numbered: int = 0            # нумерованных абзацев (пары + непарные у нас + лишние метки LO)
    equal: int = 0
    rate: float | None = None    # equal / numbered; None — нумерации нет, проверять нечего
    hidden_skipped: int = 0      # метки LO, сопоставленные скрытым абзацам из DocxDoc.skipped_hidden
    hidden_declared: int = 0     # сколько скрытых нумерованных абзацев сообщило чтение
    lo_extra: int = 0            # нумерованные метки LO без нашего абзаца (потерянные пункты) — в numbered
    mismatches: list[dict[str, str]] = field(default_factory=list)


_LABEL = re.compile(r"\(?(?:\d+[a-zа-яё]?[.)]?(?:\d+[a-zа-яё]?[.)]?)*|(?:[IVXLCDM]+|[ivxlcdm]+|[A-Za-zА-Яа-яЁё])[.)])|[•·▪◦\-–—o§]")


def _is_label(seg: str) -> bool:
    """Похоже ли на метку списка: «1.», «2.6.1.», «IV.», «а)», «(3)», маркер."""
    return len(seg) <= 20 and bool(_LABEL.fullmatch(seg))


def _seg_key(seg: str) -> str:
    """Ключ выравнивания: метки — как есть, текст — только буквы (номера сносок в LO стоят цифрами в тексте)."""
    if _is_label(seg):
        return "L:" + seg
    # первые 12 букв: мягкий перенос строки (w:br) у нас — пробел, а LO рвёт абзац на строки
    k = _key(seg)[:12]
    return "T:" + k if k else "N:" + re.sub(r"\s+", "", seg)


_LEAD = re.compile(r"(\S{1,20}) (?=\S)")


def _segments(parts: Iterable[str]) -> Iterator[str]:
    """Непустые сегменты; ведущая метка с пробелом («I. ОБЩИЕ…», суффикс метки «space») — отдельный сегмент."""
    for seg in parts:
        seg = seg.strip()
        if not seg:
            continue
        m = _LEAD.match(seg)
        if m and _is_label(m.group(1)):
            yield m.group(1)
            seg = seg[m.end():].strip()
        if seg:
            yield seg


def compare(paras: list[tuple[str, str]], lo_lines: list[str], name: str = "",
            hidden: Iterable[tuple[int, str]] = ()) -> FileCheck:
    """paras — (метка, текст) по порядку документа; lo_lines — строки выгрузки LO; hidden — скрытые абзацы,
    пропущенные чтением (DocxDoc.skipped_hidden: позиция в paras, перед которой стоял абзац, и его метка).

    Обе стороны режутся на сегменты по табуляции (LO склеивает ячейки строки таблицы через «\t», а метку от
    текста отделяет «\t»); сегменты выравниваются difflib по ключу. Метка LO для нашего абзаца — префикс
    сегмента перед нашим текстом (суффикс метки «пробел») или предыдущий сегмент LO, если он похож на метку
    и не сопоставлен нашему тексту.
    """
    # наши сегменты: (ключ, номер абзаца, вид: "label" | "first" | "text" | "hidden")
    hidden_at: dict[int, list[str]] = {}
    hidden_list = list(hidden)
    for pos, lbl in hidden_list:
        hidden_at.setdefault(pos, []).append(lbl)
    ours: list[tuple[str, int, str, str]] = []
    for i in range(len(paras) + 1):
        # скрытый абзац — только метка; LO показывает её отдельной строкой без текста
        for lbl in hidden_at.get(i, []):
            ours.append(("L:" + lbl.strip(), -1, "hidden", lbl))
        if i == len(paras):
            break
        lbl, txt = paras[i]
        if lbl:
            ours.append((_seg_key(lbl), i, "label", lbl))
        first = True
        for seg in _segments(_NOTE.sub("", txt).split("\t")):
            ours.append((_seg_key(seg), i, "first" if first else "text", seg))
            first = False
    lo = list(_segments(seg for ln in lo_lines for seg in ln.split("\t")))
    ka = [o[0] for o in ours]
    kb = [_seg_key(x) for x in lo]
    a2b: dict[int, int] = {}
    sm = difflib.SequenceMatcher(None, ka, kb, autojunk=False)
    for tag, a0, a1, b0, b1 in sm.get_opcodes():
        if tag == "equal" or (tag == "replace" and a1 - a0 == b1 - b0):
            for i, j in zip(range(a0, a1), range(b0, b1), strict=True):
                if tag == "equal":
                    a2b[i] = j
                elif ours[i][2] == "hidden":
                    continue          # скрытый абзац прикрывает только метку LO, совпавшую с его меткой точно
                elif ours[i][0][:2] == kb[j][:2]:
                    a2b[i] = j
    b_text = {j for i, j in a2b.items() if ours[i][2] not in ("label", "hidden")}   # LO, занятые нашим текстом
    b_hidden = {j for i, j in a2b.items() if ours[i][2] == "hidden"}               # LO, выровненные со скрытыми
    consumed = {j for i, j in a2b.items() if ours[i][2] != "hidden"}               # LO, занятые нашими абзацами

    res = FileCheck(file=name, hidden_declared=len(hidden_list))
    first_seg: dict[int, int] = {}
    label_seg: dict[int, int] = {}
    for k, (_, pi, kind, _) in enumerate(ours):
        if kind == "first":
            first_seg[pi] = k
        elif kind == "label":
            label_seg[pi] = k

    def lo_label_for(pi: int) -> str | None:
        """Метка LO для абзаца pi; None — абзац не выровнен."""
        k = first_seg.get(pi)
        if k is None:                                   # абзац без текста: только метка
            kl = label_seg.get(pi)
            if kl is None or kl not in a2b:
                return None
            return lo[a2b[kl]]
        if k not in a2b:
            return None
        j = a2b[k]
        probe = ours[k][3][:12]
        pos = lo[j].find(probe)
        if pos > 0 and _is_label(lo[j][:pos].strip()):
            return lo[j][:pos].strip()
        if j > 0 and (j - 1) not in b_text and _is_label(lo[j - 1]):
            consumed.add(j - 1)
            return lo[j - 1]
        return ""

    for pi, (lbl, txt) in enumerate(paras):
        ll = lo_label_for(pi)
        if ll is None:
            if _numbered(lbl):
                res.numbered += 1
                if len(res.mismatches) < 50:
                    res.mismatches.append({"ours": lbl, "lo": "<нет пары>", "start": txt.strip()[:40]})
            continue
        if not (_numbered(lbl) or _numbered(ll)):
            continue
        res.numbered += 1
        if lbl.strip() == ll.strip():
            res.equal += 1
        elif len(res.mismatches) < 50:
            # в отчёт — только метки и первые слова (без текстов документов целиком)
            res.mismatches.append({"ours": lbl, "lo": ll, "start": txt.strip()[:40]})
    # Нумерованные метки LO, не занятые ни одним нашим абзацем. Исключаются из доли только выровненные со
    # скрытыми абзацами, о которых сообщило чтение (w:vanish); остальные — пункт есть у LO, у нас потерян.
    for j, seg in enumerate(lo):
        if j in consumed or not (_is_label(seg) and _numbered(seg)):
            continue
        if j in b_hidden:
            res.hidden_skipped += 1
            continue
        res.lo_extra += 1
        res.numbered += 1
        if len(res.mismatches) < 50:
            nxt = lo[j + 1][:40] if j + 1 < len(lo) and not _is_label(lo[j + 1]) else ""
            res.mismatches.append({"ours": "<нет абзаца>", "lo": seg, "start": nxt})
    if res.numbered:
        res.rate = round(res.equal / res.numbered, 4)
    else:
        res.status = "no_numbering"
    return res


def check_file(docx: Path, soffice: str, cache_dir: Path | None = None) -> FileCheck:
    doc = read_docx(docx)
    paras = [(p.number or "", p.text) for p in iter_all_paras(doc)]
    return compare(paras, lo_text(docx, soffice, cache_dir), name=docx.name, hidden=doc.skipped_hidden)


def summary(checks: list[FileCheck]) -> dict[str, object]:
    """Итог честно: доля — только по файлам с проверяемой нумерацией; файлы без неё перечислены отдельно."""
    checked = [c for c in checks if c.status == "checked"]
    equal = sum(c.equal for c in checked)
    numbered = sum(c.numbered for c in checked)
    return {
        "files_total": len(checks),
        "files_checked": len(checked),
        "files_no_numbering": [c.file for c in checks if c.status == "no_numbering"],
        "files_ok": sum(1 for c in checked if (c.rate or 0) >= 0.99),
        "labels_equal": equal,
        "labels_numbered": numbered,
        "rate": round(equal / numbered, 4) if numbered else None,
        "hidden_skipped": sum(c.hidden_skipped for c in checked),
        "lo_extra": sum(c.lo_extra for c in checked),
    }


def main(argv: list[str] | None = None) -> int:
    from dms_core.config import derived_dir, kit_dir

    soffice = find_soffice()
    if not soffice:
        print("нет LibreOffice (soffice)", file=sys.stderr)
        return 2
    kit = kit_dir()
    files = sorted(kit.rglob("*.docx"))
    report = []
    checks = []
    for f in files:
        t = time.perf_counter()
        r = check_file(f, soffice, cache_dir=derived_dir() / "lo_txt_cache")
        r.file = str(f.relative_to(kit))
        checks.append(r)
        d = asdict(r)
        d["seconds"] = round(time.perf_counter() - t, 2)
        report.append(d)
        if r.status == "no_numbering":
            print(f"{'—':>7}  нет автонумерации — не проверяется  {r.file}")
        else:
            print(f"{r.rate:7.2%}  {r.equal}/{r.numbered}  скрытых={r.hidden_skipped}/{r.hidden_declared}  "
                  f"лишних у LO={r.lo_extra}  {r.file}")
    total = summary(checks)
    out = derived_dir() / "numbering_check.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": total, "files": report}, ensure_ascii=False, indent=1), encoding="utf-8")
    rate = total["rate"]
    print(f"файлов с автонумерацией: {total['files_checked']} из {total['files_total']}, "
          f"≥99 %: {total['files_ok']}/{total['files_checked']}; меток совпало "
          f"{total['labels_equal']}/{total['labels_numbered']}"
          + (f" ({rate:.2%})" if isinstance(rate, float) else "")
          + f"; без автонумерации (не проверяются): {len(total['files_no_numbering'])}")
    print(f"отчёт: {out}")
    return 0 if all(c.rate is not None and c.rate >= 0.99 for c in checks if c.status == "checked") else 1


if __name__ == "__main__":
    raise SystemExit(main())
