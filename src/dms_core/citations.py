"""Проверка ссылок черновика ответа (план §6.5, CONTRACTS §7, `docs/rules.md` п. 1, 3а, ADR 0005).

Модель возвращает DraftAnswer со ссылками `ref` на то, что ей отдали инструменты. Здесь — код, а не модель, решает,
что из этого попадёт на экран:

1. Ссылка жива, только если одновременно:
   - ref принадлежит договору ответа (ссылка на пункт/строку другого договора ГК — отброс «чужой договор»);
   - документ ссылки — условия договора (каталог и общие правила СК — нет, config/precedence.yaml);
   - ref вернул инструмент в этом прогоне агента (`run_refs`): модель не может сослаться на то, чего не видела;
   - для пункта: цитата ≥ 25 знаков и 4 слов найдена в тексте пункта (`locate_quote`): точно после нормализации
     (пробелы, кавычки, ё, дефисы/тире, регистр) или с ≤ 2 правками символов, при которых не изменилось ни одно
     слово-отрицание/ограничение и ни одно число («не», «без», «кроме», «исключ*», «только», «не более» …);
   - для ссылки на документ «договор» целиком (карточка, payments_next, date_calc — ref = doc_id): цитата ищется
     по пунктам и строкам таблиц этого документа; без цитаты утверждение подтверждается результатом вычисления
     инструмента (числа и даты утверждения — из результата), а на экран идёт строка источника с этими числами;
   - для строки перечня клиник: проверка по id, цитата (если есть) — название клиники / юрлицо / адрес строки;
   - для таблицы перечня клиник целиком (ref = id таблицы `<doc>#t<N>` / листа `<doc>#<лист>`, этап w7-fix: ссылка
     «клиники нет в перечне» без строки чужой клиники): в прогоне вернулась хотя бы одна строка этой таблицы, цитата
     — заголовок таблицы (программа строк таблицы, как в первой строке таблицы документа) или название документа;
   - для матрицы «программа × вид помощи»: source_ref есть в матрице договора;
   - ссылка содержательно связана с утверждением: у текста утверждения и цитаты/пункта есть общее значимое слово
     (леммы dms_core.index.morph без служебных и общих слов) или общее число.
   На экран (Citation.quote) идёт фрагмент ТЕКСТА ДОКУМЕНТА по найденным позициям, не текст модели.
2. Утверждение без живых ссылок удаляется целиком (на экран не попадает).
3. Проверка Checks со значением yes/absent (и каждое условие conditions) держится, только если есть живое
   утверждение с этим именем в `supports`; иначе → not_found (условие — убирается). `absent` («нет в документах»)
   — ещё и подтверждение инструментом этого прогона (`_absent_confirmed`), просмотр (`looked_at`) и разобранный
   документ нужного типа; иначе → `not_found` («не нашли»), из которого «нельзя» не бывает (`docs/rules.md` п. 3а).
4. Альтернативы — только существующие строки клиник этого договора и программы контекста (или программы, по которой
   агент вызывал clinics_query).
5. Противоречие остаётся, только если живы обе цитаты; без живой пары `documents` → consistent.

Все причины отброса — в `Verified.dropped` (метрики: доля отброшенных, ссылки на чужой договор).
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from .config import load_yaml
from .constants import DocType
from .index import morph
from .numtext import known_numbers, numbers
from .precedence import doc_level, is_contract_terms
from .schemas import (
    CareMatrix,
    Checks,
    Citation,
    Clause,
    ClinicRow,
    Document,
    DraftAnswer,
    DraftCitation,
    DraftContradiction,
    DraftStatement,
)
from .store.db import KitStore, program_matches
from .tools.clinics import NAME_THRESHOLD as CLINIC_QUERY_THRESHOLD
from .tools.clinics import name_score, normalize_name

MIN_QUOTE_CHARS = 25        # план §6.5: цитата пункта не короче 25 знаков …
MIN_QUOTE_WORDS = 4         # … и 4 слов
MAX_EDITS = 2               # нечёткое совпадение: не больше 2 правок символов (опечатка, пунктуация)
ALIGN_THRESHOLD = 80        # rapidfuzz partial_ratio — только чтобы найти место для сравнения правками
NAME_THRESHOLD = 92         # цитата строки клиник против названия/юрлица/адреса
QUOTE_SHOW_CHARS = 300      # «короткий фрагмент» в Citation (ТЗ п. 8, п. 4)

# Слова, изменение которых меняет смысл условия на противоположный или сужает его (дефект 2 ревизии w2):
# нечёткое совпадение цитаты не допускается, если в них есть хоть одно различие. Числа проверяются отдельно.
GUARD_WORDS = frozenset({"не", "нет", "ни", "без", "кроме", "только", "более", "менее", "свыше", "до", "после",
                         "исключительно", "лишь"})
GUARD_PREFIXES = ("исключ",)

# Общие слова документов ДМС, которые есть почти в каждом пункте: по ним связь утверждения с цитатой не считается.
GENERIC_LEMMAS = frozenset({
    "договор", "программа", "страхование", "страховой", "страховщик", "страхователь", "застрахованный",
    "услуга", "медицинский", "помощь", "настоящий", "условие", "соответствие", "пункт", "раздел", "приложение",
    "указанный", "являться", "который", "весь", "этот", "такой", "свой", "случай", "лицо", "год", "рубль", "руб",
})

# Имена проверок Checks, которые подтверждаются утверждениями (DraftStatement.supports)
CHECK_FIELDS = ("exclusion", "care_in_program", "clinic_in_program")

# Причины отброса (уходят в dropped и в метрики; тексты — для журнала разбора, без текстов документов)
R_FOREIGN = "ссылка на чужой договор"
R_NOT_TERMS = "документ не является условием договора"
R_NOT_IN_RUN = "ссылки не было в результатах инструментов этого прогона"
R_UNKNOWN = "ссылка не найдена в документах договора"
R_QUOTE_SHORT = "цитата короче 25 знаков или 4 слов"
R_QUOTE_NOT_FOUND = "цитата не найдена в тексте пункта"
R_NAME_MISMATCH = "цитата не совпадает с клиникой строки"
R_UNRELATED = "ссылка не связана с утверждением (нет общих значимых слов и чисел)"
R_CALC = "ссылка на договор без цитаты: числа утверждения не из вычисления инструмента"
R_STATEMENT = "утверждение без живых ссылок"
R_ALT_ROW = "альтернатива: строки клиник нет в договоре или в прогоне"
R_ALT_PROGRAM = "альтернатива: строка другой программы"
R_ABSENT = "absent без просмотренного разобранного документа → not_found"
R_ABSENT_TOOL = "absent не подтверждён результатом инструмента этого прогона → not_found"
R_NO_SUPPORT = "проверка без подтверждающего утверждения (supports) → not_found"
R_CONTRADICTION = "противоречие без двух живых цитат"

REF_REASONS = (R_FOREIGN, R_NOT_TERMS, R_NOT_IN_RUN, R_UNKNOWN, R_QUOTE_SHORT, R_QUOTE_NOT_FOUND, R_NAME_MISMATCH,
               R_UNRELATED, R_CALC, R_ALT_ROW, R_ALT_PROGRAM)


# Какие документы должны быть разобраны, чтобы «нет в документах» было правдой (план §6.5, `docs/rules.md` п. 3а).
def _absent_sources() -> dict[str, tuple[DocType, ...]]:
    """Какие типы документов должны быть разобраны, чтобы проверка могла быть absent (config/verdict_rules.yaml)."""
    cfg = load_yaml("verdict_rules.yaml")["absent_sources"]
    return {k: tuple(DocType(t) for t in v) for k, v in cfg.items()}


@dataclass
class Verified:
    """Итог проверки по одному договору (CONTRACTS §7). `draft` — очищенный черновик: только живые ссылки,
    у каждой живой ссылки quote заменён фрагментом текста документа."""
    draft: DraftAnswer
    citations: dict[str, Citation]                     # ref → проверенная ссылка (первая цитата по ref)
    dropped: list[tuple[str, str]]                     # (ref или поле, причина)
    # (ref, цитата) → ссылка: одна строка может цитироваться разными фрагментами
    quoted: dict[tuple[str, str], Citation] = field(default_factory=dict)
    doc_levels: dict[str, int] = field(default_factory=dict)   # doc_id → уровень старшинства (precedence.yaml)
    total_refs: int = 0                                # сколько ссылок модель дала всего (знаменатель метрики)

    def citation(self, dc: DraftCitation) -> Citation | None:
        return self.quoted.get((dc.ref, dc.quote)) or self.citations.get(dc.ref)


def dropped_share(v: Verified) -> float:
    """Доля отброшенных ссылок (метрика плана §10.1)."""
    refs = sum(1 for _, why in v.dropped if why in REF_REASONS and why not in (R_ALT_ROW, R_ALT_PROGRAM))
    return refs / v.total_refs if v.total_refs else 0.0


def foreign_refs(v: Verified) -> list[str]:
    """Ссылки на чужой договор, которые модель попыталась показать (метрика «перенос», план §14)."""
    return [ref for ref, why in v.dropped if why == R_FOREIGN]


# ------------------------------------------------------------------ нормализация текста для сравнения цитат

_QUOTE_CHARS = frozenset("«»“”„‟\"'‘’`")
_DASH_CHARS = frozenset("-‐‑‒–—―−")
_WORD_RE = re.compile(r"[0-9a-zа-я]+")


def _norm_map(text: str) -> tuple[str, list[int]]:
    """Нормализованный текст и для каждого его знака — позиция в исходном тексте (для offsets и показа фрагмента).

    Регистр, ё→е, все кавычки → ", дефисы и тире → пробел, любые пробелы (в т. ч. неразрывные) → один пробел.
    """
    out: list[str] = []
    pos: list[int] = []
    space = True                                   # в начале строки пробел не ставим
    for i, ch in enumerate(text or ""):
        if ch.isspace() or ch in _DASH_CHARS:
            norm = " "
        elif ch in _QUOTE_CHARS:
            norm = '"'
        else:
            norm = ch.casefold().replace("ё", "е")
        for c in norm:
            if c == " ":
                if space:
                    continue
                space = True
            else:
                space = False
            out.append(c)
            pos.append(i)
    while out and out[-1] == " ":
        out.pop()
        pos.pop()
    return "".join(out), pos


def normalize(text: str) -> str:
    """Регистр, ё→е, кавычки → ", дефисы/тире → пробел, пробелы → один пробел."""
    return _norm_map(text)[0]


def _guard_tokens(norm: str) -> list[str]:
    """Слова, которые нечёткое совпадение не вправе изменить: отрицания, ограничения, «исключ*», числа."""
    return [w for w in _WORD_RE.findall(norm)
            if w in GUARD_WORDS or w.startswith(GUARD_PREFIXES) or any(ch.isdigit() for ch in w)]


def locate_quote(quote: str, text: str) -> tuple[int, int] | None:
    """Где цитата стоит в тексте: (начало, конец) в ИСХОДНОМ тексте или None.

    Основной путь — точное вхождение после нормализации. Нечёткий — только опечатка/пунктуация: окно текста,
    найденное partial_ratio, отличается от цитаты не более чем на MAX_EDITS правок, и в нём те же слова-отрицания,
    ограничения и числа в том же порядке (удалённое «не» или изменённое число — не совпадение).
    """
    q, _ = _norm_map(quote)
    t, pos = _norm_map(text)
    if not q or not t:
        return None
    at = t.find(q)
    if at >= 0:
        return pos[at], pos[at + len(q) - 1] + 1
    if len(q) > len(t) + MAX_EDITS:
        return None
    al = fuzz.partial_ratio_alignment(q, t, score_cutoff=ALIGN_THRESHOLD)
    if al is None:
        return None
    best: tuple[int, int, int] | None = None       # (правки, начало, конец) в нормализованном тексте
    for ds in range(-MAX_EDITS, MAX_EDITS + 1):
        for de in range(-MAX_EDITS, MAX_EDITS + 1):
            s, e = max(0, al.dest_start + ds), min(len(t), al.dest_end + de)
            if e <= s:
                continue
            d = Levenshtein.distance(q, t[s:e], score_cutoff=MAX_EDITS)
            if d <= MAX_EDITS and (best is None or d < best[0]):
                best = (d, s, e)
    if best is None:
        return None
    _, s, e = best
    if _guard_tokens(q) != _guard_tokens(t[s:e]):
        return None
    return pos[s], pos[e - 1] + 1


def quote_found(quote: str, text: str) -> bool:
    """Цитата есть в тексте (точно после нормализации или с опечаткой, не меняющей смысла)."""
    return locate_quote(quote, text) is not None


def quote_long_enough(quote: str, text: str) -> bool:
    """≥ 25 знаков и ≥ 4 слов; короткий пункт целиком (короче порога) — тоже годится."""
    q = normalize(quote)
    if len(q) >= MIN_QUOTE_CHARS and len(q.split()) >= MIN_QUOTE_WORDS:
        return True
    return bool(q) and q == normalize(text)


_SPACES = re.compile(r"\s+")


def _short(text: str) -> str:
    text = _SPACES.sub(" ", text).strip()
    return text if len(text) <= QUOTE_SHOW_CHARS else text[: QUOTE_SHOW_CHARS - 1].rstrip() + "…"


# ------------------------------------------------------------------ связь утверждения со ссылкой

def _content_lemmas(text: str) -> set[str]:
    return {w for w in morph.lemmas(text) if len(w) >= 3 and not w.isdigit() and w not in GENERIC_LEMMAS}


def related(statement: str, evidence: Iterable[str]) -> bool:
    """Утверждение и ссылка говорят об одном: общая значимая лемма, общая основа (5 первых букв слова ≥ 5 знаков:
    «стоматология» ↔ «стоматологический») или общее число/дата."""
    ev = [e for e in evidence if e]
    a = _content_lemmas(statement)
    b: set[str] = set()
    for e in ev:
        b |= _content_lemmas(e)
    if a & b:
        return True
    if {w[:5] for w in a if len(w) >= 5} & {w[:5] for w in b if len(w) >= 5}:
        return True
    st_nums = numbers(statement)
    return bool(st_nums and st_nums & known_numbers(ev))


# ------------------------------------------------------------------ индекс ссылок договора

_LIST_TABLE = re.compile(r"^(.+?#t\d+)r\d+|^(.+?#[^#!]+)!\d+")


def list_table_ref(row_ref: str) -> str | None:
    """id таблицы перечня клиник по id строки: «c05.program#t6r2» → «c05.program#t6», «c01.contract#t11r202#v2» →
    «c01.contract#t11», «c09.clinics#Список медицинских организаций!10» → «c09.clinics#Список медицинских
    организаций»; другой вид id — None."""
    m = _LIST_TABLE.match(row_ref or "")
    return (m.group(1) or m.group(2)) if m else None


@dataclass
class _ContractRefs:
    """Всё, на что можно сослаться в одном договоре: документы, пункты, строки клиник, ссылки матрицы."""
    contract_id: str
    docs: dict[str, Document]
    clauses: dict[str, Clause]
    rows: dict[str, ClinicRow]              # row_id → строка
    rows_by_source: dict[str, ClinicRow]    # source_ref строки → строка
    care: dict[str, CareMatrix]             # source_ref → строка матрицы
    care_rows: list[CareMatrix] = field(default_factory=list)
    tables: dict[str, list[ClinicRow]] = field(default_factory=dict)   # id таблицы перечня → её строки (w7-fix)

    @classmethod
    def load(cls, store: KitStore, contract_id: str) -> _ContractRefs:
        rows = store.clinics(contract_id)
        care_rows = store.care(contract_id)
        care: dict[str, CareMatrix] = {}
        for m in care_rows:
            for ref in m.source_refs:
                care.setdefault(ref, m)
        clauses = {c.id: c for c in store.clauses(contract_id)}
        tables: dict[str, list[ClinicRow]] = {}
        for r in rows:
            t = list_table_ref(r.id)
            if t and t not in clauses:
                tables.setdefault(t, []).append(r)
        return cls(
            contract_id=contract_id,
            docs={d.id: d for d in store.documents(contract_id)},
            clauses=clauses,
            rows={r.id: r for r in rows},
            rows_by_source={r.source_ref: r for r in rows},
            care=care,
            care_rows=care_rows,
            tables=tables,
        )

    def has(self, ref: str) -> bool:
        return (ref in self.clauses or ref in self.rows or ref in self.care or ref in self.rows_by_source
                or ref in self.docs or ref in self.tables)

    def doc_type_of(self, ref: str) -> DocType | None:
        doc_id = self.doc_id_of(ref)
        doc = self.docs.get(doc_id) if doc_id else None
        return doc.doc_type if doc else None

    def doc_id_of(self, ref: str) -> str | None:
        if ref in self.clauses:
            return self.clauses[ref].doc_id
        row = self.rows.get(ref) or self.rows_by_source.get(ref)
        if row is not None:
            return row.doc_id
        head = ref.split("#", 1)[0]
        return head if head in self.docs else None

    def doc_clauses(self, doc_id: str) -> list[Clause]:
        return [c for c in self.clauses.values() if c.doc_id == doc_id]


class _Index:
    """Свои ссылки договора + ленивый поиск по другим договорам (только чтобы назвать причину «чужой договор»)."""

    def __init__(self, store: KitStore, contract_id: str) -> None:
        self.store = store
        self.own = _ContractRefs.load(store, contract_id)
        self._others: dict[str, _ContractRefs] | None = None
        try:
            gid = store.contract(contract_id).group_id
            self.group_doc_ids = {d.id for d in store.group_documents(gid)} if gid else set()
        except (KeyError, ValueError):
            self.group_doc_ids = set()

    def others(self) -> dict[str, _ContractRefs]:
        if self._others is None:
            self._others = {c.id: _ContractRefs.load(self.store, c.id) for c in self.store.contracts()
                            if c.id != self.own.contract_id}
        return self._others

    def foreign(self, ref: str) -> bool:
        """ref есть в другом договоре и нет в своём. Сначала дешёвый признак — префикс id «cNN.»."""
        if self.own.has(ref):
            return False
        head = ref.split(".", 1)[0]
        others = self.others()
        if head in others:
            return True
        return any(o.has(ref) for o in others.values())


# ------------------------------------------------------------------ записи инструментов прогона

def _rec(t: Any) -> tuple[str, dict[str, Any], set[str], str | None, dict[str, Any], Any]:
    """(имя, аргументы, refs, ошибка, краткий итог, результат вычисления) записи ToolRecord (agent.py).
    Через getattr: verify не зависит от модуля агента."""
    return (str(getattr(t, "name", "")), dict(getattr(t, "args", None) or {}), set(getattr(t, "refs", None) or ()),
            getattr(t, "error", None), dict(getattr(t, "summary", None) or {}), getattr(t, "result", None))


# ------------------------------------------------------------------ проверка одной ссылки

Checked = tuple[DraftCitation, Citation] | tuple[None, str]


def _clause_citation(cl: Clause, quote: str, title: str, contract_id: str) -> Citation | None:
    """Цитата в тексте пункта (или его сносок): Citation с фрагментом текста документа по найденным позициям."""
    span = locate_quote(quote, cl.text)
    if span is not None:
        fragment = cl.text[span[0]:span[1]]
        offsets: tuple[int, int] | None = span
    else:
        notes = "\n".join(cl.footnotes)
        span = locate_quote(quote, notes) if notes else None
        if span is None:
            return None
        fragment, offsets = notes[span[0]:span[1]], None
    return Citation(doc_id=cl.doc_id, doc_title=title, contract_id=contract_id, clause_number=cl.number,
                    table_ref=cl.id if cl.number is None and "#t" in cl.id else None, page=cl.page,
                    quote=_short(fragment), verified=True, offsets=offsets)


def _calc_citation(text: str, doc_id: str, title: str, own: _ContractRefs, calcs: list[Any]) -> Citation | None:
    """Утверждение по вычислению инструмента (payments_next / date_calc) со ссылкой на документ «договор».

    Подтверждено, если: в утверждении есть числа/даты; хотя бы одно из них — в результате вычисления; остальные —
    в результате или в строке источника; источник — пункт/строка таблицы документа с числами утверждения (график
    взносов — строки таблицы договора). На экран — эта строка документа.
    """
    want = numbers(text)
    if not want:
        return None
    best: tuple[int, Clause] | None = None
    calc_nums = known_numbers(calcs)
    if not want & calc_nums:
        return None
    for cl in own.doc_clauses(doc_id):
        have = known_numbers([cl.text])
        hit = len(want & have)
        if hit and want <= (calc_nums | have) and (best is None or hit > best[0]):
            best = (hit, cl)
    if best is None:
        return None
    cl = best[1]
    return Citation(doc_id=doc_id, doc_title=title, contract_id=own.contract_id, clause_number=cl.number,
                    table_ref=cl.id if cl.number is None else None, page=cl.page, quote=_short(cl.text),
                    verified=True, offsets=(0, len(cl.text)))


def _check_ref(dc: DraftCitation, idx: _Index, run_refs: set[str], statement: str | None,
               calcs: dict[str, list[Any]]) -> Checked:
    """(живая ссылка с цитатой документа, Citation) или (None, причина). Порядок проверок важен для метрик:
    «чужой договор» считается раньше «не было в прогоне», чтобы попытка переноса не пряталась за другой причиной.
    statement — текст утверждения (None для пары противоречия: там связь не проверяется)."""
    ref = dc.ref.strip()
    own = idx.own
    if not ref:
        return None, R_UNKNOWN
    if idx.foreign(ref):
        return None, R_FOREIGN
    if ref.split("#", 1)[0] in idx.group_doc_ids:
        return None, R_NOT_TERMS                      # общие правила СК / каталог ГК — не условия договора
    if not own.has(ref):
        return None, R_UNKNOWN
    doc_type = own.doc_type_of(ref)
    if doc_type is not None and not is_contract_terms(doc_type):
        return None, R_NOT_TERMS
    table = own.tables.get(ref) if ref not in own.docs and ref not in own.rows else None
    if table is not None:
        # таблица перечня (w7-fix): в прогоне — хотя бы одна её строка; цитата — заголовок таблицы (программа её
        # строк) или название документа
        if not any(r.id in run_refs or r.source_ref in run_refs for r in table):
            return None, R_NOT_IN_RUN
        doc = own.docs.get(table[0].doc_id)
        title = doc.title if doc else ""
        heads = {normalize(x): x for x in [*(r.program for r in table), title] if x}
        quote = heads.get(normalize(dc.quote or ""))
        if quote is None:
            return None, R_QUOTE_NOT_FOUND
        if statement is not None and not related(statement, [quote, title, *{r.program for r in table}]):
            return None, R_UNRELATED
        cit = Citation(doc_id=table[0].doc_id, doc_title=title, contract_id=own.contract_id, table_ref=ref,
                       quote=_short(quote), verified=True)
        return dc.model_copy(update={"ref": ref, "quote": cit.quote}), cit
    if ref not in run_refs:
        return None, R_NOT_IN_RUN

    doc_id = own.doc_id_of(ref) or ""
    doc = own.docs.get(doc_id)
    title = doc.title if doc else ""

    def done(cit: Citation, evidence: list[str]) -> Checked:
        if statement is not None and not related(statement, evidence):
            return None, R_UNRELATED
        return dc.model_copy(update={"ref": ref, "quote": cit.quote}), cit

    if ref in own.clauses:
        # пункт: цитата обязательна и должна найтись в тексте пункта (вместе со сносками)
        cl = own.clauses[ref]
        if not quote_long_enough(dc.quote, "\n".join([cl.text, *cl.footnotes])):
            return None, R_QUOTE_SHORT
        cit = _clause_citation(cl, dc.quote, title, own.contract_id)
        if cit is None:
            return None, R_QUOTE_NOT_FOUND
        return done(cit, [cit.quote, cl.path])

    if ref in own.docs:
        # документ целиком (карточка договора, payments_next, date_calc): цитата — по пунктам и строкам документа
        if dc.quote.strip():
            short = True
            for cl in own.doc_clauses(ref):
                if not quote_long_enough(dc.quote, cl.text):
                    continue
                short = False
                cit = _clause_citation(cl, dc.quote, title, own.contract_id)
                if cit is not None:
                    return done(cit, [cit.quote, cl.path])
            return None, R_QUOTE_SHORT if short else R_QUOTE_NOT_FOUND
        if statement is None:
            return None, R_QUOTE_SHORT
        cit = _calc_citation(statement, ref, title, own, calcs.get(ref, []))
        if cit is None:
            return None, R_CALC
        return dc.model_copy(update={"ref": ref, "quote": cit.quote}), cit

    row = own.rows.get(ref) or (None if ref in own.care else own.rows_by_source.get(ref))
    if row is not None:
        # строка клиник: решает id; цитата, если модель её дала, — название клиники, юрлицо или адрес строки
        if dc.quote.strip():
            q = normalize_name(dc.quote)
            cands = [normalize_name(x) for x in (row.clinic_name, row.legal_name or "", row.address or "") if x]
            if not q or not any(q in c or fuzz.partial_ratio(q, c) >= NAME_THRESHOLD for c in cands if c):
                return None, R_NAME_MISMATCH
        cit = Citation(doc_id=row.doc_id, doc_title=title, contract_id=own.contract_id, table_ref=row.source_ref,
                       quote=_short(row.clinic_name), verified=True)
        return done(cit, [row.clinic_name, row.legal_name or "", row.address or "", row.city or "", row.care_type,
                          row.program])

    # матрица «программа × вид помощи»: ссылка — source_ref строки матрицы
    m = own.care[ref]
    mark = "входит" if m.present else "не входит"
    cit = Citation(doc_id=doc_id, doc_title=title, contract_id=own.contract_id, table_ref=ref,
                   quote=_short(f"{m.program}: {m.care_type} — {mark}"), verified=True)
    return done(cit, [m.care_type, m.program, *(own.clauses[r].text for r in m.source_refs if r in own.clauses)])


# ------------------------------------------------------------------ проверки Checks

def _parsed_types(idx: _Index) -> set[DocType]:
    """Типы документов договора, которые реально разобраны: статус не error и из документа есть пункты/строки."""
    own = idx.own
    with_content = {c.doc_id for c in own.clauses.values()} | {r.doc_id for r in own.rows.values()}
    with_content |= {d for d in (own.doc_id_of(ref) for ref in own.care) if d}
    return {d.doc_type for d in own.docs.values() if d.status.value != "error" and d.id in with_content}


def _supports(st: DraftStatement) -> set[str]:
    """Имена проверок утверждения: «care_in_program», «checks.exclusion», «conditions.franchise», «franchise»."""
    out: set[str] = set()
    for s in st.supports:
        s = s.strip().casefold()
        s = s.removeprefix("checks.")
        if s:
            out.add(s)
    return out


def _same_program(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return program_matches(a, b) or program_matches(b, a) or normalize(a) == normalize(b)


def _absent_confirmed(name: str, backers: list[DraftStatement], idx: _Index, tools: list[Any],
                      programs: set[str], looked_at: list[str]) -> bool:
    """absent подтверждён инструментом этого прогона (дефект 4 ревизии w2), а не только именем инструмента.

    care_in_program — care_matrix вернул строку этой программы с present=False, и её ref — в живой ссылке
      подтверждающего утверждения;
    clinic_in_program — clinics_query по этой программе вернул ≥ 1 строку перечня, клиника из запроса (name)
      названа, и среди строк перечня этой программы в хранилище её нет;
    exclusion — поиск/чтение пунктов был без ошибки, нашёл пункты, и looked_at не пуст.
    Программа без строк в матрице/перечне → не подтверждено (not_found).
    """
    recs = [_rec(t) for t in tools]
    own = idx.own
    if name == "care_in_program":
        returned: set[str] = set()
        for n, _a, refs, err, _s, _r in recs:
            if n in ("care_matrix", "compare_programs") and err is None:
                returned |= refs
        cited = {dc.ref for st in backers for dc in st.citations}
        for ref in cited & returned:
            m = own.care.get(ref)
            if m is None or m.present:
                continue
            if programs and any(_same_program(m.program, p) for p in programs):
                return True
            if not programs and len({x.program for x in own.care_rows}) == 1:
                return True                      # у договора одна программа — строка заведомо «этой» программы
        return False
    if name == "clinic_in_program":
        if not programs:
            return False
        names: list[str] = []
        listed = False
        for n, args, refs, err, _s, _r in recs:
            p = args.get("program")
            if n != "clinics_query" or err is not None or not p or not any(_same_program(p, x) for x in programs):
                continue
            if args.get("name"):
                names.append(str(args["name"]))
            # перечень программы получен: вызов вернул хотя бы одну строку этой программы
            listed = listed or any(r in own.rows and _same_program(own.rows[r].program, p) for r in refs)
        prog_rows = [r for r in own.rows.values() if any(_same_program(r.program, p) for p in programs)]
        if not names or not listed or not prog_rows:
            return False                         # клиника не названа / перечня этой программы нет → not_found
        # нужной клиники среди строк программы нет — проверка по хранилищу, а не по слову модели
        return not any(name_score(nm, r) >= CLINIC_QUERY_THRESHOLD for nm in names for r in prog_rows)
    if name == "exclusion":
        searched = any(n in ("search_clauses", "get_clause") and err is None and (refs or summ.get("hits"))
                       for n, _a, refs, err, summ, _r in recs)
        return searched and bool([x for x in looked_at if x.strip()])
    return False


def _fix_checks(checks: Checks, statements: list[DraftStatement], looked_at: list[str], idx: _Index,
                tools: list[Any], programs: set[str], dropped: list[tuple[str, str]]) -> Checks:
    """Проверки SC 01 только с доказательствами (дефект 1 ревизии w2, `docs/rules.md` п. 1, 3а)."""
    parsed = _parsed_types(idx)
    sources = _absent_sources()
    own = idx.own
    by_name: dict[str, list[DraftStatement]] = {}
    for st in statements:                         # statements — уже только живые утверждения
        for s in _supports(st):
            by_name.setdefault(s, []).append(st)
    updates: dict[str, Any] = {}
    for name in CHECK_FIELDS:
        value = getattr(checks, name)
        if value not in ("yes", "absent"):
            continue
        backers = by_name.get(name, [])
        if not backers:
            updates[name] = "not_found"
            dropped.append((f"checks.{name}", R_NO_SUPPORT))
            continue
        if value != "absent":
            continue
        types = sources.get(name, ())
        ok = bool([x for x in looked_at if x.strip()]) and bool(parsed & set(types))
        if name == "clinic_in_program":
            ok = ok and bool(own.rows)           # перечня клиник нет — сказать «клиники нет» нельзя
        if name == "care_in_program":
            ok = ok and (bool(own.care) or DocType.program in parsed)
        if not ok:
            updates[name] = "not_found"
            dropped.append((f"checks.{name}", R_ABSENT))
        elif not _absent_confirmed(name, backers, idx, tools, programs, looked_at):
            updates[name] = "not_found"
            dropped.append((f"checks.{name}", R_ABSENT_TOOL))
    # условия: каждое — своим утверждением («conditions.franchise» или «franchise»); общее «conditions»
    # подтверждает условие, только если оно одно
    conds = list(checks.conditions)
    kept = [c for c in conds if by_name.get(c) or by_name.get(f"conditions.{c}")
            or (len(conds) == 1 and by_name.get("conditions"))]
    for c in conds:
        if c not in kept:
            dropped.append((f"checks.conditions.{c}", R_NO_SUPPORT))
    if kept != conds:
        updates["conditions"] = kept
    return checks.model_copy(update=updates) if updates else checks


# ------------------------------------------------------------------ verify

def verify(draft: DraftAnswer, run_refs: set[str], store: KitStore, contract_id: str, *,
           program: str | None = None, tools: list[Any] | None = None) -> Verified:
    """Проверка черновика по договору contract_id (CONTRACTS §7).

    program — программа контекста (альтернативы и absent — только по ней); tools — записи инструментов прогона
    (agent.ToolRecord: имя, аргументы, refs, краткий итог, результат вычисления). Если tools не передан, берётся
    `run_refs.tools` (AgentRun.refs возвращает набор с записями); без записей absent не подтверждается, а ссылка
    на договор без цитаты — тоже.
    """
    if tools is None:
        tools = list(getattr(run_refs, "tools", None) or [])
    idx = _Index(store, contract_id)
    dropped: list[tuple[str, str]] = []
    citations: dict[str, Citation] = {}
    quoted: dict[tuple[str, str], Citation] = {}
    total = 0
    if draft.contract_id != contract_id:
        # черновик другого договора: ни одна его ссылка не может быть показана в этом договоре
        dropped.append((draft.contract_id, R_FOREIGN))

    # вычисления инструментов по документу-источнику: doc_id → результаты payments_next / date_calc
    calcs: dict[str, list[Any]] = {}
    for n, _a, refs, err, _s, result in map(_rec, tools):
        if n in ("payments_next", "date_calc") and err is None and result is not None:
            for r in refs:
                calcs.setdefault(r, []).append(result)

    def check(dc: DraftCitation, statement: str | None) -> DraftCitation | None:
        nonlocal total
        total += 1
        if draft.contract_id != contract_id:
            dropped.append((dc.ref, R_FOREIGN))
            return None
        live, cit_or_why = _check_ref(dc, idx, run_refs, statement, calcs)
        if live is None:
            dropped.append((dc.ref, str(cit_or_why) or R_UNKNOWN))
            return None
        assert isinstance(cit_or_why, Citation)
        # ref проверенной ссылки — в Citation: пункт ответа в чате связывается с источником по ref (ADR 0014)
        cit_or_why = cit_or_why.model_copy(update={"ref": live.ref})
        quoted.setdefault((live.ref, live.quote), cit_or_why)
        citations.setdefault(live.ref, cit_or_why)
        return live

    # 1–2. утверждения: оставляем живые ссылки, утверждение без них удаляем
    statements: list[DraftStatement] = []
    for i, st in enumerate(draft.statements):
        live = [x for dc in st.citations if (x := check(dc, st.text)) is not None]
        if live:
            statements.append(st.model_copy(update={"citations": live}))
        else:
            dropped.append((f"statements[{i}]", R_STATEMENT))

    # 5. противоречия: нужны обе живые цитаты (`docs/rules.md` п. 2 — показать оба фрагмента)
    contradictions: list[DraftContradiction] = []
    for i, c in enumerate(draft.contradictions):
        a, b = check(c.a, None), check(c.b, None)
        if a is not None and b is not None:
            contradictions.append(c.model_copy(update={"a": a, "b": b}))
        else:
            dropped.append((f"contradictions[{i}]", R_CONTRADICTION))

    # 4. альтернативы: строка клиник этого договора, вернувшаяся в прогоне, программы контекста; без программы
    #    контекста — программы, по которой агент вызывал clinics_query (дефект 6 ревизии w2)
    query_programs = {str(a["program"]) for n, a, _r, err, _s, _x in map(_rec, tools)
                      if n == "clinics_query" and err is None and a.get("program")}
    programs = {program} if program else query_programs
    alternatives = []
    for alt in draft.alternatives:
        rid = alt.row_id.strip()
        row = idx.own.rows.get(rid)
        if row is None or rid not in run_refs:
            dropped.append((rid, R_FOREIGN if idx.foreign(rid) else R_ALT_ROW))
            continue
        if not programs or not any(_same_program(row.program, p) or row.program == p for p in programs):
            dropped.append((rid, R_ALT_PROGRAM))
            continue
        alternatives.append(alt.model_copy(update={"row_id": rid}))

    checks = draft.checks
    if draft.contract_id != contract_id:
        checks, alternatives = None, []          # проверки чужого черновика к этому договору не относятся
    if checks is not None:
        absent_programs = programs | {str(a["program"]) for n, a, _r, err, _s, _x in map(_rec, tools)
                                      if n == "care_matrix" and err is None and a.get("program")}
        if program:
            absent_programs = {program}
        checks = _fix_checks(checks, statements, draft.looked_at, idx, tools, absent_programs, dropped)
        want = "contradictory" if contradictions else "consistent"
        if checks.documents != want:
            if checks.documents == "contradictory":
                dropped.append(("checks.documents", R_CONTRADICTION))
            checks = checks.model_copy(update={"documents": want})

    clean = draft.model_copy(update={
        "statements": statements, "contradictions": contradictions, "alternatives": alternatives, "checks": checks,
    })
    levels = {d.id: doc_level(d) for d in idx.own.docs.values()}
    return Verified(draft=clean, citations=citations, dropped=dropped, quoted=quoted, doc_levels=levels,
                    total_refs=total)
