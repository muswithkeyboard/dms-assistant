"""Разметка таблиц перечня клиник оператором (ADR 0012, docs/CONTRACTS.md §10).

Договор неизвестной СК разбирается запасным адаптером (adapters/generic.py) — только пункты. Оператор отмечает
таблицу перечня клиник и роли колонок (TableMapping), и строки таблицы становятся строками перечня (ClinicRow)
этого договора. Ничего не угадывается при применении: роли колонок задал человек (или профиль СК, который человек
сохранил); угадка ролей (suggest) — только предложение на экране разметки.

Таблицы (list_tables):
  * docx — все таблицы тела документа в порядке read_docx (номер таблицы = Table.index, как у адаптеров:
    ссылка «<doc_id>#t<N>r<строка>»); объединённые ячейки разворачиваются в сетку колонок документа (w:gridSpan —
    текст в первой колонке объединения, остальные пустые; w:vMerge — текст верхней ячейки повторяется вниз;
    w:gridBefore — сдвиг строки), чтобы номер колонки разметки был одним и тем же во всех строках;
  * xlsx — листы; номер строки ссылки — номер строки Excel (индекс + 1), ссылка «<doc_id>#<лист>!<строка>» — тот же
    вид, что у адаптера СОГАЗ (citations.list_table_ref узнаёт оба вида);
  * пустые таблицы и таблицы с одной непустой строкой пропускаются (подписи, шапки, рамки).

Формат id и source_ref строки перечня (проверяется citations.verify как строка перечня — цитата: название клиники):
  docx: «<doc_id>#t<N>r<R>», xlsx: «<doc_id>#<лист>!<R>»; ячейка вида помощи с несколькими строками даёт строку
  перечня на каждый вид помощи — id «…#<k>» (k с 1), source_ref — строка таблицы (как у адаптера СОГАЗ).

Таблицы без строки заголовка (Дополнение §10, перечни «по таблице на программу», так у «Ренессанса»):
  * header_row = None — данные с первой строки таблицы (после строки-названия, если program_from_title);
  * program_from_title — программа строк = текст строки-названия: первая строка таблицы с одной непустой короткой
    ячейкой (длинные одноячеечные строки сверху — текст условий, не название: пропускаются); первая строка с
    несколькими ячейками — названия нет: предупреждение, таблица не разбирается;
  * подпись такой таблицы — по раскладке, не по тексту (layout_signature): «layout:<колонок>:<номера колонок,
    заполненных в строках данных>» — одна разметка и профиль СК подходят ко всем таблицам программ договора и
    следующих договоров СК; suggest предлагает header_row = None + program_from_title сам (_content_roles: роли
    колонок по содержимому), если в договоре ≥ 2 таблиц с той же раскладкой.

Применение (apply) — правила, общие для любых СК:
  * данные — строки ниже header_row (header_row = None — см. выше); строка, совпавшая по подписи с заголовком, —
    повтор заголовка (пропуск);
  * строка с одной непустой ячейкой в таблице из нескольких колонок — подзаголовок раздела: если вида помощи нет ни
    колонкой, ни постоянным значением, подзаголовок — вид помощи следующих строк (так оформлены перечни
    «Ренессанса»: «Поликлиника | Врач на дом» над строками клиник);
  * пустая ячейка названия при заполненной строке (адрес, код) — следующий адрес той же клиники: название берётся из
    строки выше в том же разделе (у «Ренессанса» сеть с несколькими адресами пишется так); строка, где название не
    из чего взять, пропускается (предупреждение со счётом);
  * таблица из одной колонки (название и адрес в одной ячейке): если у большинства строк в начале номер «N.», строки
    без номера — подзаголовки разделов; номер снимается; «… Адрес: …» в ячейке названия делится на название и адрес,
    если колонки адреса нет;
  * франшиза: число («10», «10 %») — процент; «—», «нет», «не применяется» — «—» (не применяется, это не 0 %); пусто —
    None (не указано); иначе — None и предупреждение со счётом (как адаптеры, docs/CONTRACTS.md §1);
  * «по направлению»: «да», «по направлению», «+» → True;
  * программа: колонка → строка-заголовок блока (program_from_title, см. ниже) → строка-название → постоянная
    программа разметки → программа, переданная вызывающим (fallback_program: единственная программа договора). Нет
    ни одного источника — строки без программы: в пробном разборе — с пустой программой и предупреждением, сохранить
    такую разметку нельзя (mapping_no_program), а onboarding такую таблицу не применяет (w9-fix п. 1г: перечень без
    программы давал на экране «программу договора» «Перечень клиник договора»);
  * блоки программ (w9-fix п. 4, program_from_title): одноячеечная строка «Вариант № N <программа> …» в любом месте
    таблицы задаёт программу следующих строк; «Общие для вариантов: 1 …, 3 …» — строки относятся к нескольким
    программам (строка перечня на каждую, id «…#v<N>»), «Общие для всех вариантов» — ко всем вариантам таблицы;
    франшиза в тексте блока или раздела («Франшиза (требуется активация) - 15%.») — franchise_pct строк под ним
    (колонка франшизы важнее); выражения — config/texts_ru/mapping.yaml block_*, franchise_text;
  * одноячеечная строка-примечание («^^^^^ Внимание предыдущая клиника: …», «Примечание …», «* …»; note_prefixes,
    note_marks) — в access_note строки клиники выше, а не подзаголовок вида помощи (w9-fix п. 3);
  * ячейка названия «Итого», «Всего», «Страховая премия», число или сумма — не клиника: строка пропускается (skip_names,
    w9-fix п. 1в);
  * значок (картинка) в ячейке docx без текста — текст icon_mark («[значок]»): в колонке by_referral — «да»
    (w9-fix п. 6); в подпись раскладки значки не входят.
Проверка содержимого (content_check, w9-fix п. 1б): доля строк, где ячейка названия похожа на название организации,
и доля адресов в колонке адреса; колонок с суммами при отсутствии адреса — таблица премий. Ниже порога — разметка
подозрительна: предупреждение в пробном разборе, PUT без confirm_suspicious — 422, профиль СК не применяется сам.
Профиль СК применяется автоматически (auto_profile, w9-fix п. 2) только при совпадении СК договора и профиля
(insurer_key), сильной подписи (strong_signature) и прошедшей проверке содержимого; иначе — только предложение.
Раскладки сравниваются с допуском (signature_fits, w9-fix п. 5): колонка названия может быть заполнена реже порога,
если колонки адреса и кода совпали.
Слова заголовков и тексты — config/texts_ru/mapping.yaml. Модуль не зависит от dms_api; тексты документов в журнал
не пишутся.
"""
from __future__ import annotations

import re
import unicodedata
import zipfile
from collections.abc import Iterable
from functools import cache
from pathlib import Path
from typing import Any

from .config import load_yaml
from .constants import FRANCHISE_NOT_APPLICABLE, TABLE_PREVIEW_ROWS
from .kit import KitContract
from .schemas import CareMatrix, ClinicRow, ColumnRole, InsurerProfile, TableMapping, TablePreview, TableRef

# Сколько первых строк таблицы просматривается в поисках строки заголовка (подпись профиля и угадка ролей).
# Брифы называли строки 0..3; у СОГАЗ заголовок перечня — 9-я строка листа (над ним «Приложение № 4 …»), поэтому
# просматривается столько строк, сколько видит оператор в предпросмотре.
HEADER_SCAN = TABLE_PREVIEW_ROWS
# Слов в ячейке заголовка не больше (длиннее — это данные, а не название колонки)
HEADER_WORDS = 5
# Доля строк данных с признаком (номер в начале, адрес в ячейке), начиная с которой признак считается свойством
# колонки / таблицы
MAJORITY = 0.5
# Колонка входит в раскладку таблицы (подпись layout:…), если заполнена хотя бы в этой доле строк данных. Не
# большинство: у сети клиник с несколькими адресами название пишется один раз (ячейки ниже пустые), и в перечне
# «Ренессанса» «Санкт-Петербург» колонка названия заполнена в 44 % строк — при пороге 50 % раскладки таблиц
# программ одного договора разошлись бы.
LAYOUT_SHARE = 0.25
# Одна непустая ячейка длиннее этого — текст (условия программы, телефоны пульта), а не название программы
TITLE_MAX_CHARS = 80
# Колонка значков предлагается как «по направлению», если значки хотя бы в этой доле строк данных
ICON_SHARE = 0.05
# Код клиники в перечне: одно «слово» до 20 знаков с цифрой или прописными буквами («П5фВ», «АПР+п_4», «ЛАБ»,
# «(008_3089ПС)»); город («Москва») и название — не код
_CODE = re.compile(r"^\(?[\w+\-./]{1,20}\)?$")
LAYOUT_PREFIX = "layout:"
MATRIX_PREFIX = "matrix:"
# TableMapping.program у перечня и длинной матрицы: «все программы договора» (дополнение CONTRACTS §11) — строки
# таблицы размножаются на каждую программу договора (Contract.programs после перечней), как у СОГАЗ t7: таблица
# услуг, общая для программ
ALL_PROGRAMS = "*"

Rows = list[list[str]]

# Слова названия СК, не отличающие одну СК от другой (insurer_key): «СК», «Страхование», ОПФ
_INSURER_NOISE = {"ск", "страхование", "страховая", "компания", "ооо", "ао", "пао", "зао", "оао", "сао", "сострахование"}
# Сильная подпись (strong_signature): ячеек заголовка или заполненных колонок раскладки не меньше
STRONG_CELLS = 3
# Похожая таблица (similar): столько же колонок и доля общих заполненных колонок раскладки (Жаккар) не меньше
SIMILAR_SHARE = 0.5


@cache
def _cfg() -> dict[str, Any]:
    return dict(load_yaml("texts_ru/mapping.yaml") or {})


def warn_text(key: str, **kw: Any) -> str:
    return str((_cfg().get("warnings") or {}).get(key, key)).format(**kw)


def error_text(key: str, **kw: Any) -> str:
    return str((_cfg().get("errors") or {}).get(key, key)).format(**kw)


def default_program() -> str:
    return str(_cfg().get("default_program") or "Перечень клиник договора")


def icon_mark() -> str:
    return str(_cfg().get("icon_mark") or "[значок]")


def _is_icon(v: str) -> bool:
    return v.strip() == icon_mark()


# ------------------------------------------------------------------ нормализация и подпись

_FOOTNOTE = re.compile(r"\[\^\d+\]|[¹²³⁰-⁹]+|\*+")
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def norm_cell(text: str | None) -> str:
    """Ячейка → сравнимый текст: NFC, регистр, ё→е, без меток сносок ([^1], ¹, *), знаки → пробел, пробелы
    схлопнуты."""
    s = unicodedata.normalize("NFC", text or "").casefold().replace("ё", "е")
    s = _FOOTNOTE.sub(" ", s)
    s = _NON_WORD.sub(" ", s).replace("_", " ")
    return " ".join(s.split())


def signature(rows: Rows, header_row: int | None) -> str:
    """Подпись строки заголовка: нормализованные непустые ячейки через «|». Строки нет — пустая подпись."""
    if header_row is None or header_row < 0 or header_row >= len(rows):
        return ""
    return "|".join(c for c in (norm_cell(x) for x in rows[header_row]) if c)


def _nonempty(row: list[str]) -> list[str]:
    return [c for c in row if c and c.strip()]


def _data_rows(rows: Rows) -> Rows:
    """Строки данных для раскладки и угадки по содержимому: непустых ячеек больше одной (строка-название и
    подзаголовки разделов — одна ячейка)."""
    return [r for r in rows if len(_nonempty(r)) > 1]


def n_columns(rows: Rows) -> int:
    return max((len(r) for r in rows), default=0)


def _share(data: Rows, col: int, pred: Any = None) -> float:
    """Доля строк data, где ячейка колонки col непустая (и подходит под pred)."""
    if not data:
        return 0.0
    hit = 0
    for r in data:
        v = r[col].strip() if col < len(r) and r[col] else ""
        if _is_icon(v):
            v = ""                          # значок — не текст: в раскладку и угадку по содержимому не входит
        hit += bool(v) and (pred is None or bool(pred(v)))
    return hit / len(data)


def layout_signature(rows: Rows) -> str:
    """Подпись таблицы без строки заголовка — по раскладке (Дополнение §10): «layout:<колонок>:<номера колонок,
    заполненных хотя бы в LAYOUT_SHARE строк данных>». Текст ячеек (и название программы) в подпись не входит —
    таблицы программ одного договора и следующих договоров СК получают одну подпись. Строк данных нет — пусто."""
    data = _data_rows(rows)
    if not data:
        return ""
    n = n_columns(rows)
    cols = [c for c in range(n) if _share(data, c) >= LAYOUT_SHARE]
    return f"{LAYOUT_PREFIX}{n}:{','.join(map(str, cols))}"


def mapping_signature(rows: Rows, mapping: TableMapping) -> str:
    """Подпись таблицы для разметки: строки заголовка нет — по раскладке; широкая матрица — по раскладке и ролям
    (matrix_signature: в заголовке матрицы — названия программ, их в подпись не берём); иначе — по строке
    заголовка."""
    if mapping.header_row is None:
        return layout_signature(rows)
    if mapping.role == "care_matrix" and mapping.matrix_layout == "wide":
        return matrix_signature(rows, mapping)
    return signature(rows, mapping.header_row)


def matrix_signature(rows: Rows, mapping: TableMapping) -> str:
    """Подпись широкой матрицы (этап w10-fix2): «matrix:wide:<колонок>:<колонка видов помощи>:<её заголовок>:<колонки
    программ>». Названий программ в подписи нет: профиль СК подходит договору той же СК с другими программами, а
    программы берутся из заголовков нового договора (profile_mapping хранит «текст заголовка»). Строки заголовка
    или колонки видов помощи нет — пусто."""
    hr, care = mapping.header_row, _role_col(mapping, "care_type")
    if hr is None or not 0 <= hr < len(rows) or care is None:
        return ""
    head = rows[hr]
    care_head = norm_cell(head[care]) if care < len(head) else ""
    cols = ",".join(str(c) for c in sorted(mapping.program_cols))
    return f"{MATRIX_PREFIX}wide:{n_columns(rows)}:{care}:{care_head}:{cols}"


def _layout_parts(sig: str) -> tuple[int, set[int]] | None:
    """«layout:7:0,3,6» → (7, {0, 3, 6}); не подпись раскладки — None."""
    if not sig.startswith(LAYOUT_PREFIX):
        return None
    try:
        n, cols = sig[len(LAYOUT_PREFIX):].split(":", 1)
        return int(n), {int(c) for c in cols.split(",") if c}
    except ValueError:
        return None


def _role_col(mapping: TableMapping, role: str) -> int | None:
    return next((c for c, r in sorted(mapping.columns.items()) if r == role), None)


def signature_fits(rows: Rows, mapping: TableMapping, sig: str) -> bool:
    """Подпись таблицы rows по разметке mapping совпала с sig (подписью другой таблицы или профиля). Строка заголовка —
    точное совпадение. Раскладка — с допуском (w9-fix п. 5: у таблицы «Волжский» колонка названия заполнена реже
    LAYOUT_SHARE — адреса сети без названия): столько же колонок, расходятся только колонки названия и юрлица, а
    колонки адреса и кода разметки заполнены в обеих раскладках."""
    own = mapping_signature(rows, mapping)
    if not own or not sig:
        return False
    if own == sig:
        return True
    a, b = _layout_parts(own), _layout_parts(sig)
    if a is None or b is None or a[0] != b[0]:
        return False
    loose = {c for c, r in mapping.columns.items() if r in ("clinic_name", "legal_name")}
    if not (a[1] ^ b[1]) <= loose:
        return False
    need = [c for c in (_role_col(mapping, "address"), _role_col(mapping, "code")) if c is not None]
    return bool(need) and all(c in a[1] and c in b[1] for c in need)


def strong_signature(sig: str, mapping: TableMapping) -> bool:
    """Подпись, по которой профиль СК применяется сам (w9-fix п. 2б): раскладка — заполненных колонок ≥ STRONG_CELLS
    и роль address размечена; строка заголовка — непустых ячеек ≥ STRONG_CELLS. Слабая — только предложение."""
    if sig.startswith(MATRIX_PREFIX):
        # широкая матрица: заголовок колонки видов помощи есть и колонок программ ≥ 2
        parts = sig.split(":")
        return len(parts) >= 6 and bool(parts[4]) and len([c for c in parts[5].split(",") if c]) >= 2
    lay = _layout_parts(sig)
    if lay is not None:
        # матрица (ADR 0013): адреса у неё нет — достаточно числа заполненных колонок
        return len(lay[1]) >= STRONG_CELLS and (mapping.role == "care_matrix" or
                                                _role_col(mapping, "address") is not None)
    return len([c for c in sig.split("|") if c]) >= STRONG_CELLS


def similar(a: Rows, b: Rows) -> bool:
    """Таблицы похожи по раскладке (для «похожие неразмеченные таблицы»): столько же колонок (≥ 2) и доля общих
    заполненных колонок не меньше SIMILAR_SHARE."""
    pa, pb = _layout_parts(layout_signature(a)), _layout_parts(layout_signature(b))
    if pa is None or pb is None or pa[0] != pb[0] or pa[0] < 2:
        return False
    union = pa[1] | pb[1]
    return bool(union) and len(pa[1] & pb[1]) / len(union) >= SIMILAR_SHARE


def insurer_key(name: str | None) -> str:
    """Название СК для сравнения договора и профиля (w9-fix п. 2а): без регистра, кавычек, ё, ОПФ и слов
    «страхование», «СК», «компания»: «АО «Зетта Страхование жизни»» ↔ «зетта жизни»."""
    return " ".join(w for w in norm_cell(name or "").split() if w not in _INSURER_NOISE)


def title_row(rows: Rows, limit: int | None = None) -> int | None:
    """Строка-название таблицы (program_from_title): первая непустая строка из rows[:limit], не считая длинных
    одноячеечных строк текста над ней (условия программы, телефоны пульта — длиннее TITLE_MAX_CHARS). У строки
    одна непустая ячейка — это название; несколько ячеек (или строк нет) — названия нет: None."""
    for i, row in enumerate(rows[:limit]):
        cells = _nonempty(row)
        if not cells:
            continue
        if len(cells) == 1 and len(cells[0].strip()) > TITLE_MAX_CHARS:
            continue
        return i if len(cells) == 1 else None
    return None


def title_text(rows: Rows, i: int) -> str:
    return re.sub(r"\s+", " ", _nonempty(rows[i])[0]).strip()


# ------------------------------------------------------------------ таблицы документов


def _docx_grid(path: Path) -> list[Rows]:
    """Таблицы docx как сетки текста: read_docx даёт тексты ячеек (с автонумерацией Word), разметка XML — ширину
    ячеек (gridSpan, gridBefore) и вертикальные объединения (vMerge). Порядок таблиц — как в read_docx."""
    from lxml import etree

    from .io.docx_reader import NS, _blocks, q, read_docx

    doc = read_docx(path)
    with zipfile.ZipFile(path) as z:
        body = etree.fromstring(z.read("word/document.xml")).find("w:body", NS)
    tbls = [el for el in _blocks(body)] if body is not None else []
    tbls = [el for el in tbls if el.tag == q("tbl")]
    out: list[Rows] = []
    for t, el in zip(doc.tables, tbls, strict=False):
        grid: Rows = []
        above: dict[int, str] = {}
        for cells, tr in zip(t.rows, el.findall("w:tr", NS), strict=False):
            row: list[str] = []
            gb = tr.find("w:trPr/w:gridBefore", NS)
            row += [""] * int((gb.get(q("val")) if gb is not None else None) or 0)
            for cell, tc in zip(cells, tr.findall("w:tc", NS), strict=False):
                gs = tc.find("w:tcPr/w:gridSpan", NS)
                span = max(1, int((gs.get(q("val")) if gs is not None else None) or 1))
                vm = tc.find("w:tcPr/w:vMerge", NS)
                col = len(row)
                text = cell.text.strip()
                if not text and cell.icons:
                    text = icon_mark()                # картинка без текста — видна в предпросмотре (w9-fix п. 6)
                if vm is not None and (vm.get(q("val")) or "continue") == "continue" and not text:
                    text = above.get(col, "")         # продолжение вертикального объединения — текст верхней
                row.append(text)
                row += [""] * (span - 1)
                above[col] = text
            grid.append(row)
        out.append(grid)
    return out


def _xlsx_grid(path: Path) -> dict[str, Rows]:
    from .io.xlsx_reader import read_xlsx

    return {s: [[(c or "").strip() for c in r] for r in rows] for s, rows in read_xlsx(path).items()}


def _useful(rows: Rows) -> bool:
    """Не пустая и не одностроковая таблица: хотя бы две непустые строки."""
    return sum(1 for r in rows if _nonempty(r)) >= 2


def list_tables(kc: KitContract, root: Path, doc_ids: dict[str, str]) -> list[tuple[TableRef, str, Rows]]:
    """Таблицы документов договора: (TableRef, название документа, строки). doc_ids — путь файла описи → id
    документа в базе (файлы без документа — не разобраны — пропускаются). Нечитаемый файл пропускается: его
    ошибка уже есть в отчёте обработки."""
    from .adapters.base import abs_path

    out: list[tuple[TableRef, str, Rows]] = []
    for kf in kc.files:
        did = doc_ids.get(kf.path)
        if did is None:
            continue
        try:
            if kf.fmt == "docx":
                for i, rows in enumerate(_docx_grid(abs_path(root, kf))):
                    if _useful(rows):
                        out.append((TableRef(doc_id=did, kind="docx_table", index=i), kf.title, rows))
            elif kf.fmt == "xlsx":
                for sheet, rows in _xlsx_grid(abs_path(root, kf)).items():
                    if _useful(rows):
                        out.append((TableRef(doc_id=did, kind="xlsx_sheet", sheet=sheet), kf.title, rows))
        except Exception:  # noqa: BLE001, S112 — битый файл: таблиц нет, причина — в отчёте обработки договора
            continue
    return out


def ref_key(ref: TableRef) -> tuple[str, str, int | None, str | None]:
    return (ref.doc_id, ref.kind, ref.index, ref.sheet)


def table_label(ref: TableRef, doc_title: str) -> str:
    """Название таблицы для предупреждений и отчёта: «<документ>, таблица N» / «<документ>, лист «…»»."""
    if ref.kind == "xlsx_sheet":
        return f"{doc_title}, лист «{ref.sheet}»"
    return f"{doc_title}, таблица {(ref.index or 0) + 1}"


def find_table(tables: Iterable[tuple[TableRef, str, Rows]], ref: TableRef) -> tuple[TableRef, str, Rows] | None:
    key = ref_key(ref)
    return next((t for t in tables if ref_key(t[0]) == key), None)


def preview(ref: TableRef, title: str, rows: Rows, mapping: TableMapping | None,
            profiles: list[InsurerProfile], same_layout: int = 1, insurer_name: str | None = None,
            programs: list[str] | None = None) -> TablePreview:
    """TablePreview: первые строки, размер, предложение (профиль по подписи или угадка), подпись — действующей
    разметки, иначе предложения (по строке заголовка или по раскладке). same_layout — сколько таблиц договора
    (с этой) с той же раскладкой: от него зависит предложение «строки заголовка нет»."""
    suggested, pid = suggest(rows, profiles, same_layout, insurer_name, programs)
    head = mapping or suggested
    return TablePreview(ref=ref, doc_title=title, rows=[list(r) for r in rows[:TABLE_PREVIEW_ROWS]],
                        n_rows=len(rows), n_cols=n_columns(rows),
                        signature=mapping_signature(rows, head) if head else None,
                        mapping=mapping, suggested=suggested, profile_id=pid)


def layout_counts(tables: Iterable[tuple[TableRef, str, Rows]]) -> dict[str, int]:
    """Раскладка → сколько таблиц договора с ней (для suggest и флажка «Применить ко всем таблицам …»)."""
    out: dict[str, int] = {}
    for _ref, _title, rows in tables:
        sig = layout_signature(rows)
        if sig:
            out[sig] = out.get(sig, 0) + 1
    return out


def same_signature_tables(tables: Iterable[tuple[TableRef, str, Rows]], rows: Rows, mapping: TableMapping
                          ) -> list[tuple[TableRef, str, Rows]]:
    """Таблицы договора с той же подписью, что у таблицы rows по разметке mapping (по раскладке или по строке
    заголовка), к которым разметка подходит (validate) — «Применить ко всем таблицам с такой же раскладкой»."""
    sig = mapping_signature(rows, mapping)
    if not sig:
        return []
    out = []
    for t in tables:
        if not signature_fits(t[2], mapping, sig):
            continue
        try:
            validate_any(t[2], mapping, single_program=True)
        except MappingError:
            continue
        out.append(t)
    return out


# ------------------------------------------------------------------ проверка разметки

class MappingError(ValueError):
    """Разметка не подходит к таблице: код ошибки контракта (§10) и сообщение по-русски."""

    def __init__(self, code: str, message: str, field: str | None = None):
        super().__init__(message)
        self.code, self.message, self.field = code, message, field


def validate(rows: Rows, mapping: TableMapping) -> None:
    """Колонка названия клиники есть; колонки и строка заголовка — внутри таблицы."""
    if "clinic_name" not in mapping.columns.values():
        raise MappingError("mapping_no_name", error_text("mapping_no_name"), "columns")
    n_cols = max((len(r) for r in rows), default=0)
    for col in mapping.columns:
        if col < 0:
            # номер колонки отрицательный: «Колонка 0 вне таблицы» (col + 1) вводило в заблуждение (w9-fix п. 6)
            raise MappingError("mapping_bad_column", error_text("mapping_negative_column", col=col, n=n_cols),
                               "columns")
        if col >= n_cols:
            raise MappingError("mapping_bad_column", error_text("mapping_bad_column", col=col + 1, n=n_cols),
                               "columns")
    if mapping.header_row is None:                   # заголовка нет — данные с первой строки (Дополнение §10)
        return
    if mapping.header_row < 0 or mapping.header_row >= len(rows):
        raise MappingError("mapping_bad_header", error_text("mapping_bad_header", row=mapping.header_row + 1,
                                                            n=len(rows)), "header_row")


def has_program_source(mapping: TableMapping) -> bool:
    """В разметке указано, откуда программа строк: колонка программы, программа для всей таблицы или из названия
    таблицы / строк-вариантов (w9-fix п. 1г)."""
    return "program" in mapping.columns.values() or bool((mapping.program or "").strip()) or mapping.program_from_title


# ------------------------------------------------------------------ проверка содержимого (w9-fix п. 1б)

_MONEY = re.compile(r"^[<>≤≥~]?\s*[\d\s\u00a0]+(?:[.,]\d+)?\s*(?:%|руб\.?|р\.?|₽)?$", re.IGNORECASE)
_LETTERS = re.compile(r"[A-Za-zА-Яа-яЁё]{2}")      # «МЦ», «ЦКБ» — название; «< 20%» — нет


def _skip_words() -> list[str]:
    return [w for w in (norm_cell(x) for x in _cfg().get("skip_names") or []) if w]


def not_clinic_name(v: str) -> bool:
    """Ячейка названия — не клиника (w9-fix п. 1в): итог / шапка таблицы премий (skip_names: «Итого:», «Всего»,
    «Страховая премия», «Программа»), число или сумма («27 002,00», «< 20%»), нет и двух букв подряд."""
    s = (v or "").strip()
    if not s or _is_icon(s):
        return True
    if _MONEY.match(s) or not _LETTERS.search(s):
        return True
    n = norm_cell(s)
    return any(n == w or n.startswith(w + " ") for w in _skip_words())


def _check_cfg() -> dict[str, float]:
    d = dict(_cfg().get("content_check") or {})
    return {"name_share": float(d.get("name_share", 0.6)), "address_share": float(d.get("address_share", 0.5)),
            "money_share": float(d.get("money_share", 0.6))}


def content_check(rows: Rows, mapping: TableMapping) -> list[str]:
    """Содержимое таблицы по разметке похоже на перечень клиник? Причины подозрения по-русски (пусто — похоже).

    Строки данных — непустые строки ниже заголовка (заголовка нет — все), кроме одноячеечных (подзаголовки,
    названия) и строк с пустой ячейкой названия. Проверяется: (1) доля строк, где ячейка названия похожа на название
    организации (not_clinic_name — нет), не ниже content_check.name_share; (2) если колонка адреса размечена — доля
    строк с адресными словами или индексом (_ADDR) среди заполненных, не ниже address_share; (3) колонки адреса нет, а
    другая колонка таблицы почти вся из чисел и сумм (money_share) — это таблица премий / численности, не перечень."""
    cfg = _check_cfg()
    start = 0 if mapping.header_row is None else mapping.header_row + 1
    c_name = _role_col(mapping, "clinic_name")
    c_addr = _role_col(mapping, "address")
    data = [r for r in rows[start:] if len(_nonempty(r)) > 1 and _cell(r, c_name)]
    if not data:
        return []
    out: list[str] = []
    ok = sum(1 for r in data if not not_clinic_name(_cell(r, c_name))) / len(data)
    if ok < cfg["name_share"]:
        out.append(warn_text("susp_name", pct=round(ok * 100), min=round(cfg["name_share"] * 100)))
    if c_addr is not None:
        filled = [_cell(r, c_addr) for r in data if _cell(r, c_addr) and not _is_icon(_cell(r, c_addr))]
        share = sum(1 for v in filled if _ADDR.search(v)) / len(filled) if filled else 0.0
        if share < cfg["address_share"]:
            out.append(warn_text("susp_address", pct=round(share * 100), min=round(cfg["address_share"] * 100)))
    else:
        for c in range(n_columns(data)):
            if c == c_name:
                continue
            vals = [_cell(r, c) for r in data if _cell(r, c)]
            if len(vals) >= 2 and sum(1 for v in vals if _MONEY.match(v)) / len(vals) >= cfg["money_share"]:
                out.append(warn_text("susp_money"))
                break
    return out


# ------------------------------------------------------------------ предложение разметки

def _word_hit(w: str, h: str) -> bool:
    """Слово config ↔ слово заголовка: короткое (до 3 букв: «мо», «лпу», «код») — целиком; от 4 букв — начало
    слова заголовка, у слов от 5 букв без последней буквы («программа» ↔ «программы», «адрес» ↔ «адреса»)."""
    if len(w) <= 3:
        return h == w
    return h.startswith(w[:-1] if len(w) >= 5 else w)


def _phrase_hit(phrase: str, words: list[str]) -> bool:
    """Фраза из слов config — подряд в словах ячейки заголовка (_word_hit)."""
    ph = norm_cell(phrase).split()
    if not ph:
        return False
    return any(all(_word_hit(w, words[i + k]) for k, w in enumerate(ph)) for i in range(len(words) - len(ph) + 1))


def guess_roles(header: list[str]) -> dict[int, ColumnRole]:
    """Роли колонок по словам ячеек заголовка (config/texts_ru/mapping.yaml header_words, порядок ролей — порядок
    config): у колонки одна роль, у роли — одна колонка (первая слева)."""
    words_cfg: dict[str, list[str]] = dict(_cfg().get("header_words") or {})
    out: dict[int, ColumnRole] = {}
    taken: set[str] = set()
    # ячейка заголовка короткая: длинная ячейка — данные («Клиника МЕДСИ в Митино …»), а не название колонки
    cells = [(i, w) for i, w in ((i, norm_cell(c).split()) for i, c in enumerate(header)) if len(w) <= HEADER_WORDS]
    for role, phrases in words_cfg.items():
        if role in taken:
            continue
        for i, words in cells:
            if i in out or not words:
                continue
            if any(_phrase_hit(p, words) for p in phrases or []):
                out[i] = role  # type: ignore[assignment]
                taken.add(role)
                break
    return dict(sorted(out.items()))


_ADDR = re.compile(r"\d{6}|\bул\b|улиц|\bпр\b|просп|пр-т|\bд\b\s*\d|\bдом\b|\bг\b|шоссе|\bш\b|переул|\bпер\b|"
                   r"бульв|\bб-р\b|набережн|област|\bобл\b|\bкрай\b|республик|\bрайон|\bр-н\b", re.IGNORECASE)


def _is_code(v: str) -> bool:
    """Короткий код клиники («П5фВ», «АПР+п_4», «ЛАБ»): одно слово до 20 знаков с цифрой или только прописные."""
    s = v.strip()
    return bool(_CODE.match(s)) and (any(ch.isdigit() for ch in s) or (s.isupper() and len(s) >= 2))


def _content_roles(rows: Rows) -> dict[int, ColumnRole] | None:
    """Роли колонок по содержимому строк данных (таблица без строки заголовка): адрес — колонка, где у большинства
    строк адресные слова (_ADDR); название клиники — первая колонка левее адреса с буквенным текстом хотя бы в
    LAYOUT_SHARE строк; код — колонка раскладки, где у большинства заполненных ячеек короткий код (_is_code).
    Строки данных — где непустых ячеек больше одной. Адреса или названия нет — None."""
    data = _data_rows(rows)
    if len(data) < 2:
        return None
    n_cols = n_columns(data)

    def is_addr(v: str) -> bool:
        return bool(_ADDR.search(v))

    addr = max(range(n_cols), key=lambda c: _share(data, c, is_addr))
    if _share(data, addr, is_addr) < MAJORITY:
        return None
    name = next((c for c in range(addr) if _share(data, c, lambda v: re.search(r"[A-Za-zА-Яа-яЁё]{3}", v))
                 >= LAYOUT_SHARE), None)
    if name is None:
        return None
    cols: dict[int, ColumnRole] = {name: "clinic_name", addr: "address"}
    for c in range(n_cols):
        filled = _share(data, c)
        if c in cols or filled < LAYOUT_SHARE:
            continue
        if _share(data, c, _is_code) / filled >= MAJORITY:
            cols[c] = "code"
            break
    # колонка значков (картинки без текста: у «Ренессанса» — «по направлению», w9-fix п. 6): значки хотя бы в
    # ICON_SHARE строк данных и значков больше, чем текста (текст в ней — только легенда значков) — by_referral
    for c in range(n_cols):
        marks = sum(1 for r in data if c < len(r) and _is_icon(r[c]))
        if c not in cols and marks >= max(1, ICON_SHARE * len(data)) and _share(data, c) * len(data) < marks:
            cols[c] = "by_referral"
            break
    return referral_column(rows, 0, cols)


def referral_column(rows: Rows, start: int, roles: dict[int, ColumnRole]) -> dict[int, ColumnRole]:
    """Предложение роли by_referral по содержимому ВСЕЙ таблицы (этап w10-fix2, не только строк предпросмотра):
    колонка без роли, где «по направлению» (referral_in_text) написано хотя бы в ICON_SHARE строк данных (не меньше
    одной строки) — у СОГАЗ так заполнена колонка «Телефон» в строках ниже предпросмотра. Роль by_referral уже есть —
    без изменений."""
    if "by_referral" in roles.values():
        return dict(sorted(roles.items()))
    data = _data_rows(rows[start:])
    out = dict(roles)
    for c in range(n_columns(data)):
        if c in out:
            continue
        hits = sum(1 for r in data if c < len(r) and referral_in_text(r[c]))
        if hits and hits >= ICON_SHARE * len(data):
            out[c] = "by_referral"
            break
    return dict(sorted(out.items()))


def _content_guess(rows: Rows) -> TableMapping | None:
    """Таблица без строки с названиями колонок, одна в договоре: роли — по содержимому (_content_roles); строка
    заголовка — первая строка, если над строками клиник только одноячеечные строки (название и подзаголовки
    остаются данными: вид помощи разделов), иначе строка перед первой строкой данных."""
    roles = _content_roles(rows)
    if roles is None:
        return None
    first = next(i for i, r in enumerate(rows) if len(_nonempty(r)) > 1)
    head = 0 if first > 0 and all(len(_nonempty(r)) <= 1 for r in rows[:first]) else max(0, first - 1)
    return TableMapping(header_row=head, columns=roles)


def _title_layout_guess(rows: Rows, same_layout: int) -> TableMapping | None:
    """Перечень «по таблице на программу» (Дополнение §10): у таблицы несколько колонок, первая строка — название
    (одна ячейка, title_row) и в договоре ≥ 2 таблиц с той же раскладкой → header_row = None, программа — из
    названия, роли колонок — по содержимому."""
    if same_layout < 2 or n_columns(rows) < 2 or title_row(rows) is None:
        return None
    roles = _content_roles(rows)
    if roles is None:
        return None
    return TableMapping(header_row=None, columns=roles, program_from_title=True)


def suggest(rows: Rows, profiles: list[InsurerProfile], same_layout: int = 1, insurer_name: str | None = None,
            programs: list[str] | None = None) -> tuple[TableMapping | None, str | None]:
    """Предложение разметки: (1) профиль СК, чья подпись совпала с подписью таблицы по его разметке (строка
    заголовка или раскладка); (1а) матрица видов помощи (ADR 0013, _matrix_guess): ≥ 2 колонок заголовка — программы
    договора / перечня (programs), слева — виды помощи; или длинная таблица «программа / вид помощи»; (2) первая строка из HEADER_SCAN, где по словам заголовка нашлась колонка названия
    клиники вместе с другой ролью; (3) таблица программ без заголовка, если таких в договоре несколько
    (_title_layout_guess); (4) угадка по содержимому (_content_guess). Ничего — (None, None)."""
    p = profile_for(rows, profiles, insurer_name)
    if p is not None:
        return p.mapping, p.id
    matrix = _matrix_guess(rows, programs or [])
    if matrix is not None:
        return matrix, None
    # строка заголовка — где по словам нашлись название клиники и ещё хотя бы одна роль (одно слово «Клиника» в
    # ячейке данных заголовком не считается)
    for hr in range(min(len(rows), HEADER_SCAN)):
        roles = guess_roles(rows[hr])
        if "clinic_name" in roles.values() and len(roles) >= 2:
            return TableMapping(header_row=hr, columns=referral_column(rows, hr + 1, roles)), None
    return _title_layout_guess(rows, same_layout) or _content_guess(rows) or _list_guess(rows), None


def _list_guess(rows: Rows) -> TableMapping | None:
    """Перечень одной колонкой (w9-fix п. 4, так у «Зетты»): у большинства непустых строк номер «N.», у
    большинства нумерованных — «Адрес:». Колонка 0 — название; строка заголовка — первая строка, если она без
    номера; есть строки-заголовки блоков («Вариант № N …») — программа из них (program_from_title)."""
    if n_columns(rows) != 1:
        return None
    cells = [r[0] for r in rows if r and r[0].strip()]
    num = [c for c in cells if _LEAD_NUM.match(c)]
    if not num or len(num) / len(cells) < MAJORITY or \
            sum(1 for c in num if _ADDR_LABEL.search(c)) / len(num) < MAJORITY:
        return None
    first = next(i for i, r in enumerate(rows) if r and r[0].strip())
    head = None if _LEAD_NUM.match(rows[first][0]) or _is_variant(rows[first][0]) else first
    start = 0 if head is None else head + 1
    variants = _variants(rows, start)
    blocks = any(_block_of(r[0], variants) is not None for r in rows[start:] if r and r[0].strip())
    return TableMapping(header_row=head, columns={0: "clinic_name"}, program_from_title=blocks)


def profile_for(rows: Rows, profiles: list[InsurerProfile], insurer_name: str | None = None
                ) -> InsurerProfile | None:
    """Профиль СК, подходящий к таблице по подписи (signature_fits: строка заголовка профиля или раскладка с допуском)
    — для предложения на экране разметки. Профили СК договора (insurer_key) — раньше профилей других СК."""
    fit = [p for p in profiles if p.signature and signature_fits(rows, p.mapping, p.signature)]
    key = insurer_key(insurer_name)
    return next((p for p in fit if key and insurer_key(p.insurer_name) == key), None) or (fit[0] if fit else None)


def auto_profile(rows: Rows, profiles: list[InsurerProfile], insurer_name: str | None,
                 programs: list[str] | None = None) -> tuple[InsurerProfile | None, str | None]:
    """Профиль СК, который применяется к таблице сам при обработке договора (w9-fix п. 2), и причина, почему подошедший
    по подписи профиль применён не сам (ключ warnings: why_no_insurer / why_other_insurer / why_weak /
    why_suspicious). Сам — только если (а) СК договора задана и совпала с СК профиля (insurer_key), (б) подпись
    сильная (strong_signature), (в) содержимое таблицы по разметке профиля похоже на перечень (content_check) и
    разметка подходит к таблице (validate). Профиль матрицы (ADR 0013) — те же условия, проверка содержимого —
    matrix_check по программам договора (programs). Профиля по подписи нет — (None, None)."""
    p = profile_for(rows, profiles, insurer_name)
    if p is None:
        return None, None
    key = insurer_key(insurer_name)
    if not key:
        return None, "why_no_insurer"
    if insurer_key(p.insurer_name) != key:
        return None, "why_other_insurer"
    if not strong_signature(p.signature, p.mapping):
        return None, "why_weak"
    try:
        validate_any(rows, p.mapping, single_program=bool(programs) and len(programs or []) == 1)
    except MappingError:
        return None, "why_weak"
    if check_any(rows, p.mapping, programs):
        return None, "why_suspicious"
    return p, None

# ------------------------------------------------------------------ применение разметки

_LEAD_NUM = re.compile(r"^\s*\d{1,4}[.)]\s+")
_ADDR_LABEL = re.compile(r"\s*\bадрес\s*:\s*", re.IGNORECASE)
_PCT = re.compile(r"(\d+(?:[.,]\d+)?)\s*%?")


def parse_franchise(v: str) -> tuple[float | str | None, bool]:
    """(значение, распознано). Число — процент; «—», «нет», «не применяется» — «—»; пусто — None."""
    s = (v or "").strip()
    if not s:
        return None, True
    none_words = [norm_cell(x) for x in _cfg().get("franchise_none") or []]
    if s in ("—", "-", "–") or norm_cell(s) in [w for w in none_words if w]:
        return FRANCHISE_NOT_APPLICABLE, True
    m = _PCT.fullmatch(s)
    if m:
        return float(m.group(1).replace(",", ".")), True
    return None, False


def parse_referral(v: str) -> bool:
    """«да», «по направлению», «+» (config/texts_ru/mapping.yaml referral_yes) → True."""
    s = (v or "").strip()
    if not s:
        return False
    words = norm_cell(s).split()
    for marker in _cfg().get("referral_yes") or []:
        m = str(marker).casefold()
        if m == "+" and s == "+":
            return True
        if m and m != "+" and any(w == m or (len(m) >= 4 and w.startswith(m)) for w in words):
            return True
    return False


def referral_in_text(text: str) -> bool:
    """«По направлению» в тексте строки перечня (config referral_text: «по направлению», «только по направлению
    врача», «Организация услуг по направлению страховщика») — в любой колонке, независимо от её роли."""
    rx = _rx("referral_text")
    return bool(rx and rx.search(re.sub(r"\s+", " ", text or "")))


def _cell(row: list[str], col: int | None) -> str:
    return row[col].strip() if col is not None and col < len(row) and row[col] else ""


def _care_items(raw: str) -> list[str]:
    """Ячейка вида помощи: несколько видов через перевод строки (как у СОГАЗ) — по строке перечня на каждый."""
    out = [re.sub(r"\s+", " ", x).strip().rstrip(";").strip() for x in raw.split("\n")]
    return [x for x in out if x]


def row_ref(ref: TableRef, r: int) -> str:
    """source_ref строки таблицы: docx «<doc_id>#t<N>r<R>», xlsx «<doc_id>#<лист>!<номер строки Excel>»."""
    if ref.kind == "xlsx_sheet":
        return f"{ref.doc_id}#{ref.sheet}!{r + 1}"
    return f"{ref.doc_id}#t{ref.index}r{r}"




# ------------------------------------------------------------------ блоки программ, примечания, франшиза в тексте

@cache
def _rx(key: str) -> re.Pattern[str] | None:
    v = _cfg().get(key)
    return re.compile(str(v), re.IGNORECASE) if v else None


def franchise_in_text(text: str) -> tuple[float | None, str]:
    """Франшиза в тексте строки-заголовка (config franchise_text: «Франшиза (требуется активация) - 15%.», «франшиза
    15 %») → (процент, текст без франшизы, если она — начало строки). Нет — (None, текст)."""
    rx = _rx("franchise_text")
    m = rx.search(text) if rx else None
    if m is None:
        return None, text
    pct = float(m.group(1).replace(",", "."))
    if text[:m.start()].strip():
        return pct, text
    rest = text[m.end():].strip()
    return pct, (rest[:1].upper() + rest[1:]) if rest else text


def is_note(text: str) -> bool:
    """Одноячеечная строка-примечание к строке клиники выше (config note_marks / note_prefixes): «^^^^^ Внимание
    предыдущая клиника: …», «* …», «Примечание: …»."""
    t = (text or "").strip()
    if not t:
        return False
    if any(m and t.startswith(m) for m in (str(x) for x in _cfg().get("note_marks") or [])):
        return True
    n = norm_cell(t)
    words = [norm_cell(x) for x in _cfg().get("note_prefixes") or []]
    return any(w and (n == w or n.startswith(w + " ")) for w in words)


def _is_variant(text: str) -> bool:
    rx = _rx("block_variant")
    return bool(rx and rx.match(re.sub(r"\s+", " ", text).strip()))


def _block_of(text: str, variants: dict[int, str]) -> list[tuple[int | None, str]] | None:
    """Строка-заголовок блока программ (w9-fix п. 4) → программы следующих строк [(номер варианта, программа)]:
    «Вариант № N <программа>» — одна; «Общие для вариантов: 1 …, 3 …» — по номерам (числа перед словом);
    «Общие для всех вариантов» — все варианты таблицы. Не заголовок блока — None."""
    t = re.sub(r"\s+", " ", text).strip()
    rx = _rx("block_variant")
    m = rx.match(t) if rx else None
    if m:
        return [(int(m.group(1)), m.group(2).strip())]
    rx = _rx("block_some")
    m = rx.match(t) if rx else None
    if m:
        nums = [int(x) for x in re.findall(r"(?:^|,)\s*(\d+)\s+\S", m.group(1))]
        return [(n, variants[n]) for n in dict.fromkeys(nums) if n in variants]
    rx = _rx("block_all")
    if rx and rx.match(t):
        return [(n, v) for n, v in sorted(variants.items())]
    return None


def _variants(rows: Rows, start: int) -> dict[int, str]:
    """Номер варианта → программа по всем строкам «Вариант № N <программа>» таблицы (блок «Общие для вариантов»
    может стоять раньше вариантов, которые называет)."""
    out: dict[int, str] = {}
    rx = _rx("block_variant")
    if rx is None:
        return out
    for row in rows[start:]:
        cells = _nonempty(row)
        m = rx.match(re.sub(r"\s+", " ", cells[0]).strip()) if len(cells) == 1 else None
        if m:
            out.setdefault(int(m.group(1)), m.group(2).strip())
    return out


def _section_text(text: str) -> tuple[str, float | None]:
    """Подзаголовок раздела → (вид помощи, франшиза из текста): префикс франшизы снят, точка и двоеточие в конце —
    тоже (как вид помощи у адаптеров)."""
    pct, rest = franchise_in_text(re.sub(r"\s+", " ", text).strip())
    return re.sub(r"[\s.:;]+$", "", rest), pct


# ------------------------------------------------------------------ применение

def apply_rows(contract_id: str, ref: TableRef, rows: Rows, mapping: TableMapping, doc_id: str,
               fallback_program: str | None = None, row_programs: dict[int, list[str]] | None = None
               ) -> tuple[list[ClinicRow], dict[str, int]]:
    """Строки таблицы → строки перечня по разметке; второе значение — счётчики для предупреждений (ключи
    config/texts_ru/mapping.yaml warnings: no_name_rows, header_repeats, bad_franchise, no_program_rows,
    skipped_names, notes; флаги no_title, no_program_source — 1). Тексты ячеек в счётчики не попадают.

    row_programs (этап w10-fix) — заполняется программами, действующими в каждой строке таблицы (номер строки →
    программы; пусто — общая строка): строка-название и строки под ней — программа из названия, строка-вариант и
    её блок — программа варианта (блок «Общие для …» — программы его вариантов), колонка программы — её ячейка,
    иначе программа для всей таблицы; «*» (все программы договора) и строки выше названия — пусто. По ней
    onboarding даёт программу пунктам-строкам таблицы, не ставшим строками перечня (подзаголовки, условия)."""
    rp: dict[int, list[str]] = row_programs if row_programs is not None else {}
    validate(rows, mapping)
    stats: dict[str, int] = {}

    def bump(key: str, n: int = 1) -> None:
        stats[key] = stats.get(key, 0) + n

    col: dict[str, int] = {}
    for c, role in sorted(mapping.columns.items()):
        if role != "ignore":
            col.setdefault(role, c)
    c_name = col["clinic_name"]
    # начало данных: ниже строки заголовка; заголовка нет — с первой строки, а с program_from_title — ниже
    # строки-названия (Дополнение §10)
    start = 0 if mapping.header_row is None else mapping.header_row + 1
    variants = _variants(rows, start) if mapping.program_from_title else {}
    # блоки программ: среди одноячеечных строк данных есть заголовок блока («Вариант № N …», «Общие для …»)
    blocks = mapping.program_from_title and any(
        len(_nonempty(r)) == 1 and _block_of(_nonempty(r)[0], variants) is not None for r in rows[start:])
    title_prog: str | None = None
    if mapping.program_from_title and not blocks:
        t_row = title_row(rows, None if mapping.header_row is None else mapping.header_row)
        if t_row is None:
            # первая строка — не одна ячейка: названия программы нет, таблица не разбирается (не угадываем)
            return [], {"no_title": 1}
        title_prog = title_text(rows, t_row)
        start = max(start, t_row + 1)
        rp[t_row] = [title_prog]
    const_prog = title_prog or (mapping.program or "").strip() or (fallback_program or "").strip() or None

    def current() -> list[str]:
        """Программы строк в этой точке таблицы (для row_programs)."""
        if block:
            return [p for _n, p in block]
        return [] if const_prog in (None, ALL_PROGRAMS) else [const_prog]
    if const_prog is None and not blocks and "program" not in col:
        bump("no_program_source")
    multi = len([c for c, r in mapping.columns.items() if r != "ignore"]) > 1 or \
        max((len(_nonempty(r)) for r in rows[start:]), default=0) > 1
    head_sig = signature(rows, mapping.header_row)
    data = list(range(start, len(rows)))
    # таблица из одной колонки: нумерованный список клиник с подзаголовками без номера
    numbered = (not multi and data and
                sum(1 for i in data if _LEAD_NUM.match(_cell(rows[i], c_name))) / len(data) >= MAJORITY)
    section: str | None = None
    section_pct: float | None = None
    block: list[tuple[int | None, str]] = []
    block_pct: float | None = None
    prev_name: str | None = None
    last: list[ClinicRow] = []            # строки перечня предыдущей строки таблицы (к ним — примечание)
    out: list[ClinicRow] = []
    for i in data:
        row = rows[i]
        cells = _nonempty(row)
        if not cells:
            continue
        rp[i] = current()
        if head_sig and signature(rows, i) == head_sig:
            bump("header_repeats")
            continue
        name = _cell(row, c_name)
        if _is_icon(name):
            name = ""                           # значок в колонке названия (легенда значков) — названия нет
        if (multi and len(cells) == 1 and len(row) > 1) or (numbered and not _LEAD_NUM.match(name)):
            text = re.sub(r"\s+", " ", cells[0]).strip()
            if is_note(text):
                # примечание к строке клиники выше (w9-fix п. 3) — в её условия, раздел не меняется
                note = text.lstrip("^* ").strip()
                for r in last:
                    r.access_note = f"{r.access_note}; {note}" if r.access_note else note
                bump("notes")
                continue
            got = _block_of(text, variants) if blocks else None
            if got is not None:
                # заголовок блока программ: программа(ы) следующих строк. «Вариант № N …» раздел сбрасывает;
                # «Общие для …» — сам раздел для строк клиник сразу под ним (так у адаптера Зетты)
                block, prev_name, last = got, None, []
                rp[i] = current()                 # строка-вариант — начало своего блока
                block_pct = franchise_in_text(text)[0]
                section, section_pct = (None, None) if _is_variant(text) else _section_text(text)
                continue
            # подзаголовок раздела: вид помощи следующих строк (если вида помощи нет в разметке)
            section, section_pct = _section_text(text)
            prev_name, last = None, []
            continue
        address = _cell(row, col.get("address"))
        if numbered:
            name = _LEAD_NUM.sub("", name, count=1)
        if "address" not in col and _ADDR_LABEL.search(name):
            name, address = (x.strip() for x in _ADDR_LABEL.split(name, maxsplit=1))
        name = re.sub(r"\s+", " ", name).strip()
        if not name:
            if prev_name is None:
                bump("no_name_rows")
                continue
            name = prev_name                         # следующий адрес той же клиники (ячейка названия пустая)
        elif not_clinic_name(name):
            # «Итого:», «Страховая премия», число, сумма — не клиника (w9-fix п. 1в)
            bump("skipped_names")
            prev_name, last = None, []
            continue
        prev_name = name
        cell_prog = _cell(row, col.get("program"))
        if cell_prog:
            rp[i] = [cell_prog]
        progs: list[tuple[int | None, str]] = (
            [(None, cell_prog)] if cell_prog else block or ([(None, const_prog)] if const_prog else []))
        if not progs and ("program" in col or blocks):
            # колонка программы размечена, а в строке пусто — это не строка перечня (подписи сторон, примечания
            # под таблицей: так СОГАЗ, adapters/sogaz.py пропускает строки без программы); блоки программ — строка
            # до первого блока
            bump("no_program_rows")
            continue
        if not progs:
            progs = [(None, "")]                 # источника программы нет: только пробный разбор (no_program_source)
        raw_care = _cell(row, col.get("care_type"))
        cares = _care_items(raw_care) if raw_care else [(mapping.care_type or "").strip() or (section or "")]
        raw_fr = _cell(row, col.get("franchise_pct"))
        pct, ok = parse_franchise(raw_fr)
        if not ok:
            bump("bad_franchise")
        if pct is None and not raw_fr:
            # франшиза, написанная в самой строке («… (ФРАНШИЗА 10%)» в названии клиники, этап w10-fix: так у
            # «Ренессанса»; без неё копия договора давала «можно» вместо «с условием»), иначе — раздела / блока
            pct = franchise_in_text(" ".join(cells))[0]
            if pct is None:
                pct = section_pct if section_pct is not None else block_pct
        # «по направлению»: колонка by_referral, иначе — текст всей строки (referral_in_text, этап w10-fix2: у СОГАЗ
        # «Организация услуг по направлению страховщика» стоит в колонке «Телефон», и оператор, не отметивший эту
        # колонку, получал «можно» вместо «с условием»)
        referral = parse_referral(_cell(row, col.get("by_referral"))) or referral_in_text(" ".join(cells))
        src = row_ref(ref, i)
        last = []
        for k_prog, (num, program) in enumerate(progs):
            # строка блока «Общие для вариантов» — строка перечня на каждую программу: id «…#v<N>» (как у адаптера)
            pid = f"#v{num if num is not None else k_prog + 1}" if len(progs) > 1 else ""
            for k, care in enumerate(cares):
                r = ClinicRow(
                    id=src + pid + (f"#{k + 1}" if len(cares) > 1 else ""), contract_id=contract_id, doc_id=doc_id,
                    program=program, care_type=care, clinic_name=name,
                    legal_name=_cell(row, col.get("legal_name")) or None, address=address or None,
                    city=_cell(row, col.get("city")) or None, code=_cell(row, col.get("code")) or None,
                    franchise_pct=pct, by_referral=referral,  # type: ignore[arg-type]
                    access_note=_cell(row, col.get("access_note")) or None, source_ref=src)
                out.append(r)
                last.append(r)
    return out, stats


def expand_all_programs(rows: list[ClinicRow], programs: list[str]) -> list[ClinicRow]:
    """Строки перечня с программой «*» (ALL_PROGRAMS — все программы договора, дополнение CONTRACTS §11) → по
    строке на каждую программу договора (id «…#v<N>», ссылка — та же строка таблицы). Программ нет — такие строки
    не учитываются (лучше «перечня нет», чем строки без программы)."""
    out: list[ClinicRow] = []
    for r in rows:
        if r.program != ALL_PROGRAMS:
            out.append(r)
            continue
        for k, name in enumerate(programs):
            out.append(r.model_copy(update={"id": f"{r.id}#v{k + 1}", "program": name}))
    return out


# Порядок и ключи предупреждений о таблице (config/texts_ru/mapping.yaml warnings)
STAT_KEYS = ("no_name_rows", "header_repeats", "bad_franchise", "no_program_rows", "skipped_names", "notes")


def stat_warnings(label: str, stats: dict[str, int], n_rows: int) -> list[str]:
    """Счётчики одной таблицы → предупреждения по-русски."""
    if stats.get("no_title"):
        return [warn_text("no_title", table=label)]
    out = [warn_text(k, table=label, n=stats[k]) for k in STAT_KEYS if stats.get(k)]
    if stats.get("no_program_source"):
        out.append(warn_text("no_program_source", table=label))
    if not n_rows:
        out.append(warn_text("empty", table=label))
    return out


def apply(contract_id: str, ref: TableRef, rows: Rows, mapping: TableMapping, doc_id: str,
          title: str | None = None, fallback_program: str | None = None) -> tuple[list[ClinicRow], list[str]]:
    """Строки таблицы → строки перечня договора по разметке (правила — в описании модуля). Предупреждения —
    по-русски, без текстов ячеек (только счётчики и название таблицы)."""
    out, stats = apply_rows(contract_id, ref, rows, mapping, doc_id, fallback_program)
    return out, stat_warnings(title or table_label(ref, doc_id), stats, len(out))


# ================================================================== матрица «программа × вид помощи» (ADR 0013)
#
# Три способа (CONTRACTS §11): широкая таблица (строки — виды помощи, колонки — программы: matrix_layout = wide,
# колонка care_type + program_cols), длинная (matrix_layout = long: колонки program / care_type / present) и виды
# помощи строк размеченного перечня (derive_care у перечня — matrix_from_clinics). Правила:
#   * значение ячейки — cell_value (слова — config/texts_ru/mapping.yaml matrix_values): «да / + / входит / ✓» —
#     present=True, «нет / — / не входит / x» — present=False (единственный источник «нельзя» по матрице — явное
#     «нет» в ячейке); пусто — строки матрицы нет; прочий текст (суммы, лимиты, примечания) — строки нет, ячейка —
#     в список пропущенных (skipped) и сводное предупреждение: оператор видит, что не учтено;
#   * ссылка строки матрицы (source_refs) — ячейка / строка таблицы: wide «<строка таблицы>c<колонка>» (у каждой
#     программы своя ячейка — ссылка одной строки матрицы не подменяет другую в citations), long — строка таблицы
#     (несколько видов помощи в ячейке — «…#k»), derive_care — строка перечня + «#care» (DERIVED_SUFFIX: ссылка
#     строки матрицы не совпадает с id строки перечня, и citations проверяет её как строку матрицы — цитата
#     «программа: вид помощи — входит», а не название клиники);
#   * пара «программа × вид помощи» и с «да», и с «нет» — противоречие: пара не учитывается (предупреждение), чтобы
#     ни «можно», ни «нельзя» не вышло из неоднозначной разметки;
#   * программа wide — program_cols (пустое значение — текст ячейки заголовка); программа должна совпасть с
#     программой договора / перечня (match_program, нормализация как у перечня) — иначе предупреждение (строки
#     остаются с названием колонки).
# Проверка содержимого (matrix_check) — как content_check перечня: колонка видов помощи похожа на виды помощи
# (config/care_types.yaml, слова care_words), значения колонок программ — «да / нет», программы — программы договора.

# Ссылок у строки матрицы не больше (derive_care: строк перечня одного вида помощи бывают сотни)
MATRIX_REFS = 50
# Пропущенных ячеек в ответе пробного разбора (CONTRACTS §11)
SKIPPED_SAMPLE = 20
# Ссылка строки матрицы derive_care: source_ref строки перечня + суффикс
DERIVED_SUFFIX = "#care"


def _mcfg() -> dict[str, Any]:
    return dict(_cfg().get("matrix_values") or {})


def mwarn(key: str, **kw: Any) -> str:
    return str((_cfg().get("matrix_warnings") or {}).get(key, key)).format(**kw)


def _mark(s: str) -> str:
    """Ячейка как знак: без пробелов и точки / запятой / точки с запятой («+.», « ✓ »)."""
    return re.sub(r"[\s.;,]+", "", s)


def cell_value(text: str | None) -> bool | None:
    """Значение ячейки матрицы: True — входит, False — явное «нет», None — пропуск (пусто или прочий текст: суммы,
    лимиты, примечания; пустую ячейку от текста вызывающий отличает сам). Слова — config matrix_values."""
    s = (text or "").strip()
    if not s:
        return None
    cfg = _mcfg()
    mark = _mark(s)
    if mark and mark in {str(x) for x in cfg.get("absent_marks") or []}:
        return False
    if mark and mark in {str(x) for x in cfg.get("present_marks") or []}:
        return True
    n = norm_cell(s)
    if not n:
        return None
    if n in {norm_cell(str(x)) for x in cfg.get("absent") or []}:
        return False
    if n in {norm_cell(str(x)) for x in cfg.get("present") or []}:
        return True
    # «Да, входит», «Включено в программу» — ячейка только из слов «да»; с отрицанием («не», «нет», «без») — не «да»
    words = n.split()
    yes_words = {norm_cell(str(x)) for x in cfg.get("present_words") or []}
    if words and all(w in yes_words for w in words) and any(w != "в" for w in words):
        return True
    return None


def match_program(name: str, known: list[str]) -> str | None:
    """Программа разметки → программа договора / перечня: то же название после нормализации (регистр, ё, знаки);
    иначе единственная программа, чьё название целиком (словами) внутри названия колонки («Программа «ВИП»» →
    «ВИП»), или единственная, внутри которой название колонки. Нет совпадения — None."""
    n = norm_cell(name)
    if not n:
        return None
    same = [k for k in known if norm_cell(k) == n]
    if same:
        return same[0]
    inside = [k for k in known if norm_cell(k) and f" {norm_cell(k)} " in f" {n} "]
    if len(inside) == 1:
        return inside[0]
    outer = [k for k in known if f" {n} " in f" {norm_cell(k)} "]
    return outer[0] if len(outer) == 1 else None


@cache
def _care_phrases() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(фразы видов помощи config/care_types.yaml — названия, синонимы, слова специалистов; основы care_words)."""
    types = (load_yaml("care_types.yaml") or {}).get("care_types") or {}
    phrases: list[str] = []
    for spec in types.values():
        for x in [spec.get("name", ""), *(spec.get("synonyms") or []), *(spec.get("route_words") or [])]:
            n = norm_cell(str(x))
            if n:
                phrases.append(n)
    stems = tuple(norm_cell(str(x)) for x in _cfg().get("care_words") or [] if norm_cell(str(x)))
    return tuple(phrases), stems


def looks_like_care(text: str) -> bool:
    """Ячейка похожа на вид помощи: фраза config/care_types.yaml целиком (словами) или слово, начинающееся с основы
    care_words («Поликлиническое обслуживание», «Вызов врача на дом», «Стоматология»)."""
    n = norm_cell(text)
    if not n:
        return False
    phrases, stems = _care_phrases()
    if any(f" {p} " in f" {n} " for p in phrases):
        return True
    return any(w.startswith(s) for w in n.split() for s in stems)


def _care_text(raw: str) -> str:
    """Вид помощи из ячейки: пробелы схлопнуты, «:» / «.» / «;» в конце сняты (как подзаголовки перечня)."""
    return re.sub(r"[\s.:;]+$", "", re.sub(r"\s+", " ", raw)).strip()


def _not_care(v: str) -> bool:
    """Ячейка видов помощи — не вид помощи: пусто, число, сумма, «Итого» (skip_names)."""
    s = (v or "").strip()
    if not s or _is_icon(s) or _MONEY.match(s) or not _LETTERS.search(s):
        return True
    n = norm_cell(s)
    return any(n == w or n.startswith(w + " ") for w in _skip_words())


def wide_programs(rows: Rows, mapping: TableMapping) -> dict[int, str]:
    """wide: колонка → программа (program_cols; пустое значение — текст ячейки заголовка). Колонка без названия
    программы пропускается."""
    head = rows[mapping.header_row] if mapping.header_row is not None and 0 <= mapping.header_row < len(rows) else []
    out: dict[int, str] = {}
    for c, name in sorted(mapping.program_cols.items()):
        text = (name or "").strip() or (re.sub(r"\s+", " ", head[c]).strip() if c < len(head) else "")
        if text:
            out[c] = text
    return out


def profile_mapping(rows: Rows, mapping: TableMapping) -> TableMapping:
    """Разметка для профиля СК: у широкой матрицы программа колонки, равная тексту её заголовка, хранится пустой
    («текст заголовка»), чтобы в следующем договоре программа бралась из его таблицы, а не из этого договора."""
    if mapping.role != "care_matrix" or mapping.matrix_layout != "wide" or mapping.header_row is None:
        return mapping
    head = rows[mapping.header_row] if 0 <= mapping.header_row < len(rows) else []

    def from_header(c: int, p: str) -> bool:
        # заголовок называет эту программу: тот же текст или программа словами внутри заголовка («Программа «ВИП»»
        # ↔ «ВИП»); программа, переименованная оператором («Премиум» → «Премиум (ВИП)»), хранится как есть
        h, n = norm_cell(head[c]) if c < len(head) else "", norm_cell(p)
        return bool(n) and (h == n or f" {n} " in f" {h} ")
    cols = {c: ("" if from_header(c, p) else p) for c, p in mapping.program_cols.items()}
    return mapping.model_copy(update={"program_cols": cols})


def validate_matrix(rows: Rows, mapping: TableMapping, single_program: bool = False) -> None:
    """Разметка матрицы подходит к таблице: вид матрицы выбран, колонка видов помощи есть (mapping_no_care),
    колонки — внутри таблицы, источник программы есть (mapping_no_program): wide — хотя бы одна колонка программы с
    названием; long — колонка программы, программа для всей таблицы, из названия или единственная программа
    договора (single_program)."""
    if mapping.matrix_layout not in ("wide", "long"):
        raise MappingError("mapping_invalid", error_text("mapping_no_layout"), "matrix_layout")
    if "care_type" not in mapping.columns.values():
        raise MappingError("mapping_no_care", error_text("mapping_no_care"), "columns")
    n_cols = n_columns(rows)
    for col in [*mapping.columns, *mapping.program_cols]:
        if col < 0:
            raise MappingError("mapping_bad_column", error_text("mapping_negative_column", col=col, n=n_cols),
                               "columns")
        if col >= n_cols:
            raise MappingError("mapping_bad_column", error_text("mapping_bad_column", col=col + 1, n=n_cols),
                               "columns")
    if mapping.header_row is not None and not 0 <= mapping.header_row < len(rows):
        raise MappingError("mapping_bad_header", error_text("mapping_bad_header", row=mapping.header_row + 1,
                                                            n=len(rows)), "header_row")
    care_col = _role_col(mapping, "care_type")
    if mapping.matrix_layout == "wide":
        progs = {c: p for c, p in wide_programs(rows, mapping).items() if c != care_col}
        if not progs:
            raise MappingError("mapping_no_program", error_text("mapping_no_program_matrix"), "program_cols")
    elif not (has_program_source(mapping) or single_program):
        raise MappingError("mapping_no_program", error_text("mapping_no_program_matrix"), "program")


def validate_any(rows: Rows, mapping: TableMapping, single_program: bool = False) -> None:
    """validate для перечня, validate_matrix для матрицы."""
    if mapping.role == "care_matrix":
        validate_matrix(rows, mapping, single_program)
    else:
        validate(rows, mapping)


class MatrixResult:
    """Итог разбора матрицы: строки (по паре программа × вид помощи), пропущенные ячейки (первые SKIPPED_SAMPLE и
    общее число), противоречивые пары, программы разметки без совпадения с программами договора."""

    def __init__(self) -> None:
        self.rows: list[CareMatrix] = []
        self.skipped: list[dict[str, str]] = []
        self.skipped_total = 0
        self.conflicts = 0
        self.unknown: list[str] = []
        self.no_program_source = False
        self.no_programs = False          # программа «*», а программ у договора нет


def _collect(contract_id: str, cells: list[tuple[str, str, bool, str]], res: MatrixResult) -> None:
    """(программа, вид помощи, present, ссылка) → строки матрицы: одна на пару (ссылки — все ячейки пары); пара и с
    «да», и с «нет» — противоречие, не учитывается (ни «можно», ни «нельзя» из неё)."""
    acc: dict[tuple[str, str], tuple[set[bool], list[str]]] = {}
    for prog, care, present, src in cells:
        vals, refs = acc.setdefault((prog, care), (set(), []))
        vals.add(present)
        if src not in refs:
            refs.append(src)
    for (prog, care), (vals, refs) in acc.items():
        if len(vals) > 1:
            res.conflicts += 1
            continue
        res.rows.append(CareMatrix(contract_id=contract_id, program=prog, care_type=care,
                                   present=next(iter(vals)), source_refs=refs[:MATRIX_REFS]))


def apply_matrix_rows(contract_id: str, ref: TableRef, rows: Rows, mapping: TableMapping,
                      programs: list[str]) -> MatrixResult:
    """Строки таблицы → строки матрицы по разметке (правила — в описании раздела). programs — программы договора и
    перечня: программа разметки сопоставляется с ними (match_program); единственная программа договора — источник
    программы длинной таблицы без своего источника."""
    single = programs[0] if len(programs) == 1 else None
    validate_matrix(rows, mapping, single_program=single is not None)
    res = MatrixResult()
    care_col = _role_col(mapping, "care_type")
    head_sig = signature(rows, mapping.header_row)
    start = 0 if mapping.header_row is None else mapping.header_row + 1
    cells: list[tuple[str, str, bool, str]] = []

    def program_of(name: str) -> str:
        got = match_program(name, programs) if programs else None
        if got is None:
            if programs and name not in res.unknown:
                res.unknown.append(name)
            return name
        return got

    def skip(i: int, c: int, text: str) -> None:
        res.skipped_total += 1
        if len(res.skipped) < SKIPPED_SAMPLE:
            # номера — как на экране разметки (с 1); текст ячейки — только в ответ пробного разбора, не в журнал
            res.skipped.append({"cell": f"строка {i + 1}, колонка {c + 1}",
                                "text": re.sub(r"\s+", " ", text).strip()[:120]})

    if mapping.matrix_layout == "wide":
        progs = {c: program_of(p) for c, p in wide_programs(rows, mapping).items() if c != care_col}
        for i in range(start, len(rows)):
            row = rows[i]
            if head_sig and signature(rows, i) == head_sig:
                continue                                  # повтор заголовка внутри таблицы
            care = _care_text(_cell(row, care_col))
            if _not_care(care):
                continue                                  # пустая ячейка, «Итого», числа — не вид помощи
            for c, prog in progs.items():
                raw = _cell(row, c)
                val = cell_value(raw)
                if val is None:
                    if raw and not _is_icon(raw):
                        skip(i, c, raw)
                    continue
                cells.append((prog, care, val, f"{row_ref(ref, i)}c{c}"))
    else:
        c_prog = _role_col(mapping, "program")
        c_pres = _role_col(mapping, "present")
        const: str | None = (mapping.program or "").strip() or None
        if mapping.program_from_title and c_prog is None:
            t_row = title_row(rows, None if mapping.header_row is None else mapping.header_row)
            if t_row is not None:
                const = title_text(rows, t_row)
                start = max(start, t_row + 1)
        const = const or single
        if const is None and c_prog is None:
            res.no_program_source = True
        for i in range(start, len(rows)):
            row = rows[i]
            if not _nonempty(row) or (head_sig and signature(rows, i) == head_sig):
                continue
            raw_care = _cell(row, care_col)
            items = [x for x in (_care_text(y) for y in _care_items(raw_care)) if not _not_care(x)] if raw_care else []
            prog_raw = re.sub(r"\s+", " ", _cell(row, c_prog) or const or "").strip()
            if not items or not prog_raw:
                continue
            # «*» — все программы договора (таблица услуг, общая для программ): строка — каждой программе
            if prog_raw == ALL_PROGRAMS:
                targets = list(programs)
                if not targets:
                    res.no_programs = True
                    continue
            else:
                targets = [program_of(prog_raw)]
            val: bool | None = True                       # колонки «входит» нет: строка таблицы = вид помощи входит
            if c_pres is not None:
                raw = _cell(row, c_pres)
                val = cell_value(raw)
                if val is None:
                    if raw and not _is_icon(raw):
                        skip(i, c_pres, raw)
                    continue
            for prog in targets:
                for k, care in enumerate(items):
                    cells.append((prog, care, val, row_ref(ref, i) + (f"#{k + 1}" if len(items) > 1 else "")))
    _collect(contract_id, cells, res)
    return res


def matrix_warnings(label: str, res: MatrixResult) -> list[str]:
    """Итог разбора одной матрицы → предупреждения по-русски (без текстов ячеек, кроме названий программ)."""
    out: list[str] = []
    if res.no_program_source:
        out.append(mwarn("no_program_source", table=label))
    if res.no_programs:
        out.append(mwarn("all_no_programs", table=label))
    if res.skipped_total:
        out.append(mwarn("skipped_cells", table=label, n=res.skipped_total))
    if res.conflicts:
        out.append(mwarn("conflicts", table=label, n=res.conflicts))
    out += [mwarn("unknown_program", table=label, program=p) for p in res.unknown]
    if not res.rows:
        out.append(mwarn("empty", table=label))
    return out


def apply_matrix(contract_id: str, ref: TableRef, rows: Rows, mapping: TableMapping, doc_id: str,
                 programs: list[str], title: str | None = None) -> tuple[list[CareMatrix], list[str]]:
    """CONTRACTS §11: строки таблицы → строки матрицы договора и предупреждения (doc_id — для названия таблицы,
    ссылки строк матрицы начинаются с ref.doc_id)."""
    res = apply_matrix_rows(contract_id, ref, rows, mapping, programs)
    return res.rows, matrix_warnings(title or table_label(ref, doc_id), res)


def matrix_from_clinics(contract_id: str, clinics: list[ClinicRow]) -> list[CareMatrix]:
    """derive_care (ADR 0013 п. 3): виды помощи строк размеченного перечня → матрица present=True по программам (так
    устроены перечни «Ренессанса» и СОГАЗ — адаптеры строят матрицу так же). Ячейка «Поликлиника | Врач на дом» —
    два вида помощи (как у адаптера «Ренессанса»). Строки без программы или без вида помощи пропускаются (w9-fix
    п. 1а: строка без вида помощи ничего не подтверждает). Ссылки — source_ref строк перечня + DERIVED_SUFFIX (до
    MATRIX_REFS)."""
    acc: dict[tuple[str, str], list[str]] = {}
    for r in clinics:
        prog = (r.program or "").strip()
        if not prog:
            continue
        for part in (r.care_type or "").split("|"):
            care = _care_text(part)
            if _not_care(care):
                continue
            refs = acc.setdefault((prog, care), [])
            ref = r.source_ref + DERIVED_SUFFIX
            if ref not in refs and len(refs) < MATRIX_REFS:
                refs.append(ref)
    return [CareMatrix(contract_id=contract_id, program=p, care_type=c, present=True, source_refs=refs)
            for (p, c), refs in acc.items()]


@cache
def _care_codes() -> tuple[tuple[str, tuple[str, ...]], ...]:
    types = (load_yaml("care_types.yaml") or {}).get("care_types") or {}
    return tuple((str(code), tuple(n for n in (norm_cell(str(x)) for x in [spec.get("name", ""),
                                                                           *(spec.get("synonyms") or [])]) if n))
                 for code, spec in types.items())


def care_key(text: str) -> str:
    """Вид помощи для сравнения матрицы с перечнем: код config/care_types.yaml (название или синоним — словами внутри
    текста: «Стоматологическая помощь» ↔ «Стоматология»), иначе нормализованный текст."""
    n = norm_cell(text)
    for code, phrases in _care_codes():
        if any(f" {p} " in f" {n} " for p in phrases):
            return code
    return n


def list_conflicts(matrix: list[CareMatrix], clinics: list[ClinicRow]) -> list[tuple[CareMatrix, list[str]]]:
    """Явное «нет» матрицы при клиниках этого вида помощи в перечне той же программы (этап w10-fix2, `docs/rules.md` п. 2):
    [(строка матрицы present=False, ссылки строк перечня + DERIVED_SUFFIX)]. Вид помощи сравнивается по care_key;
    флажок derive_care не нужен — противоречие документов видно и без него."""
    have: dict[tuple[str, str], list[str]] = {}
    for r in clinics:
        prog = (r.program or "").strip()
        for part in (r.care_type or "").split("|"):
            care = _care_text(part)
            if not prog or _not_care(care):
                continue
            refs = have.setdefault((norm_cell(prog), care_key(care)), [])
            ref = r.source_ref + DERIVED_SUFFIX
            if ref not in refs and len(refs) < MATRIX_REFS:
                refs.append(ref)
    out = []
    for m in matrix:
        refs = have.get((norm_cell(m.program), care_key(m.care_type))) if not m.present else None
        if refs:
            out.append((m, refs))
    return out


def list_conflict_warning(contra: list[tuple[CareMatrix, list[str]]], limit: int = 5) -> str:
    """Предупреждение о парах list_conflicts: число и первые «программа: вид помощи» (названия, не текст документа)."""
    names = ", ".join(f"«{m.program}: {m.care_type}»" for m, _refs in contra[:limit])
    return mwarn("list_conflict", n=len(contra), pairs=names + (" …" if len(contra) > limit else ""))


def _mcheck_cfg() -> dict[str, float]:
    d = dict(_cfg().get("matrix_check") or {})
    return {k: float(d.get(k, 0.5)) for k in ("care_share", "value_share", "program_share")}


def matrix_check(rows: Rows, mapping: TableMapping, programs: list[str]) -> list[str]:
    """Содержимое таблицы по разметке матрицы похоже на матрицу видов помощи? Причины подозрения (пусто — похоже):
    (1) доля строк, где ячейка видов помощи похожа на вид помощи (looks_like_care), не ниже care_share; (2) wide —
    доля распознанных «да / нет» среди заполненных ячеек колонок программ не ниже value_share; (3) программ разметки,
    совпавших с программами договора / перечня (если они известны), не меньше program_share."""
    cfg = _mcheck_cfg()
    care_col = _role_col(mapping, "care_type")
    start = 0 if mapping.header_row is None else mapping.header_row + 1
    data = [r for r in rows[start:] if not _not_care(_cell(r, care_col))]
    out: list[str] = []
    if not data:
        return out
    ok = sum(1 for r in data if looks_like_care(_cell(r, care_col))) / len(data)
    if ok < cfg["care_share"]:
        out.append(mwarn("susp_care", pct=round(ok * 100), min=round(cfg["care_share"] * 100)))
    names: list[str] = []
    if mapping.matrix_layout == "wide":
        progs = {c: p for c, p in wide_programs(rows, mapping).items() if c != care_col}
        vals = [_cell(r, c) for r in data for c in progs if _cell(r, c) and not _is_icon(_cell(r, c))]
        share = sum(1 for v in vals if cell_value(v) is not None) / len(vals) if vals else 0.0
        if share < cfg["value_share"]:
            out.append(mwarn("susp_values", pct=round(share * 100), min=round(cfg["value_share"] * 100)))
        names = list(progs.values())
        out += swapped_columns(rows, mapping, programs)
    else:
        c_prog = _role_col(mapping, "program")
        if c_prog is not None:
            names = list(dict.fromkeys(_cell(r, c_prog) for r in data if _cell(r, c_prog)))
        elif (mapping.program or "").strip():
            names = [str(mapping.program)]
    # «*» — все программы договора (этап w10-fix2): не неизвестная программа, с программами не сравнивается;
    # содержимое такой таблицы проверяется только по видам помощи (care_share выше)
    names = [n for n in names if n.strip() != ALL_PROGRAMS]
    if programs and names:
        hit = sum(1 for n in names if match_program(n, programs) is not None) / len(names)
        if hit < cfg["program_share"]:
            out.append(mwarn("susp_programs", pct=round(hit * 100), min=round(cfg["program_share"] * 100)))
    return out


def swapped_columns(rows: Rows, mapping: TableMapping, programs: list[str]) -> list[str]:
    """Широкая матрица: колонкам назначены чужие программы (этап w10-fix2)? Колонка подозрительна, если оператор
    назначил ей программу, отличную от текста её заголовка, а заголовок сам называет другую программу — программу
    договора (match_program) или программу, назначенную другой колонке этой таблицы (перепутанные «Москва» /
    «Барнаул»). Пустое значение — «текст заголовка»: не подозрительно. Причина — одна строка с номерами колонок."""
    hr = mapping.header_row
    head = rows[hr] if hr is not None and 0 <= hr < len(rows) else []
    given = {c: (p or "").strip() for c, p in mapping.program_cols.items()}
    bad: list[int] = []
    for c, p in sorted(given.items()):
        h = re.sub(r"\s+", " ", head[c]).strip() if c < len(head) else ""
        if not p or not h or norm_cell(p) == norm_cell(h) or match_program(h, [p]) is not None:
            continue
        own = match_program(p, programs) or p
        other = match_program(h, programs) if programs else None
        named_elsewhere = any(d != c and q and (norm_cell(q) == norm_cell(h) or match_program(h, [q]) is not None)
                              for d, q in given.items())
        if (other is not None and norm_cell(other) != norm_cell(own)) or named_elsewhere:
            bad.append(c)
    return [mwarn("susp_swapped", cols=", ".join(str(c + 1) for c in bad))] if bad else []


def check_any(rows: Rows, mapping: TableMapping, programs: list[str] | None = None) -> list[str]:
    """Проверка содержимого по роли разметки: перечень — content_check, матрица — matrix_check."""
    if mapping.role == "care_matrix":
        return matrix_check(rows, mapping, programs or [])
    return content_check(rows, mapping)


def _matrix_guess(rows: Rows, programs: list[str]) -> TableMapping | None:
    """Предложение матрицы (CONTRACTS §11). Широкая: строка из первых HEADER_SCAN, где ≥ 2 ячеек правее первой
    колонки совпали с программами договора / перечня (match_program), а в колонке левее первой программы у
    большинства строк ниже — виды помощи (looks_like_care); program_cols — совпавшие колонки (название программы
    договора). Длинная: заголовок со словами программы и вида помощи (header_words) без названия клиники; колонка
    «входит» (present_header) — present."""
    for hr in range(min(len(rows), HEADER_SCAN)):
        head = rows[hr]
        hits = {c: p for c, p in ((c, match_program(v, programs)) for c, v in enumerate(head) if c > 0 and v.strip())
                if p is not None}
        if len(hits) < 2:
            continue
        first = min(hits)
        below = [r for r in rows[hr + 1:] if _nonempty(r)]
        for care_col in range(first - 1, -1, -1):
            vals = [_cell(r, care_col) for r in below if _cell(r, care_col)]
            if vals and sum(1 for v in vals if looks_like_care(v)) / len(vals) >= MAJORITY:
                return TableMapping(role="care_matrix", matrix_layout="wide", header_row=hr,
                                    columns={care_col: "care_type"}, program_cols=dict(sorted(hits.items())))
    wide = _wide_by_headers(rows)
    if wide is not None:
        return wide
    present_words = [norm_cell(str(x)) for x in _mcfg().get("present_header") or []]
    for hr in range(min(len(rows), HEADER_SCAN)):
        roles = guess_roles(rows[hr])
        if "clinic_name" in roles.values() or not {"care_type", "program"} <= set(roles.values()):
            continue
        cols: dict[int, ColumnRole] = {c: r for c, r in roles.items() if r in ("care_type", "program")}
        for c, v in enumerate(rows[hr]):
            words = norm_cell(v).split()
            if c not in cols and any(_phrase_hit(w, words) for w in present_words if w):
                cols[c] = "present"
                break
        return TableMapping(role="care_matrix", matrix_layout="long", header_row=hr, columns=dict(sorted(cols.items())))
    return None


def _program_header(v: str) -> bool:
    """Ячейка заголовка может быть названием программы (широкая матрица без известных программ): короткий текст с
    буквами, не вид помощи, не значение «да / нет», не слово роли колонки (header_words) и не заголовок «входит»."""
    t = re.sub(r"\s+", " ", v or "").strip()
    if not t or len(t) > TITLE_MAX_CHARS or not _LETTERS.search(t) or _MONEY.match(t):
        return False
    if looks_like_care(t) or cell_value(t) is not None or guess_roles([t]):
        return False
    words = norm_cell(t).split()
    return not any(_phrase_hit(w, words) for w in (norm_cell(str(x)) for x in _mcfg().get("present_header") or [])
                   if w)


def _wide_by_headers(rows: Rows) -> TableMapping | None:
    """Широкая матрица при первой обработке, когда программ договора ещё нет (этап w10-fix2): строка из первых
    HEADER_SCAN без колонки названия клиники, где правее колонки видов помощи ≥ 2 ячеек похожи на названия программ
    (_program_header); колонка видов помощи — первая или вторая заполненная ячейка заголовка, и у большинства строк
    ниже в ней — виды помощи (looks_like_care); среди заполненных ячеек колонок программ распознанных «да / нет»
    (cell_value) не меньше matrix_check.value_share — иначе это таблица лимитов, а не матрица. Программы — текст
    заголовков (program_cols со значением «»)."""
    share = _mcheck_cfg()["value_share"]
    for hr in range(min(len(rows), HEADER_SCAN)):
        head = rows[hr]
        if "clinic_name" in guess_roles(head).values():
            continue
        filled = [c for c, v in enumerate(head) if v.strip()]
        below = [r for r in rows[hr + 1:] if _nonempty(r)]
        for care_col in filled[:2]:
            cols = [c for c in filled if c > care_col and _program_header(head[c])]
            if len(cols) < 2 or not below:
                continue
            vals = [_cell(r, care_col) for r in below if _cell(r, care_col)]
            if not vals or sum(1 for v in vals if looks_like_care(v)) / len(vals) < MAJORITY:
                continue
            cells = [_cell(r, c) for r in below for c in cols if _cell(r, c) and not _is_icon(_cell(r, c))]
            if not cells or sum(1 for v in cells if cell_value(v) is not None) / len(cells) < share:
                continue
            return TableMapping(role="care_matrix", matrix_layout="wide", header_row=hr,
                                columns={care_col: "care_type"}, program_cols={c: "" for c in cols})
    return None
